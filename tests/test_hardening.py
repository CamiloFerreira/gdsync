"""Tests de seguridad y compatibilidad CLI <-> app. Sin dependencias (no requiere GTK).

    python3 -m unittest discover -s tests -v
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="gdsync-test-"))
os.environ.update({
    "HOME": str(TMP / "home"),
    "XDG_CONFIG_HOME": str(TMP / "home/.config"),
    "XDG_STATE_HOME": str(TMP / "home/.local/state"),
})
(TMP / "home").mkdir()
# systemctl falso: los tests nunca deben tocar el gestor systemd real
FAKEBIN = TMP / "bin"
FAKEBIN.mkdir()
(FAKEBIN / "systemctl").write_text("#!/bin/sh\nexit 0\n")
(FAKEBIN / "systemctl").chmod(0o755)
os.environ["PATH"] = f"{FAKEBIN}:{os.environ['PATH']}"
sys.path.insert(0, str(ROOT))

from gdsync_app import core, updater  # noqa: E402

HOME = Path(os.environ["HOME"])
GDSYNC = str(ROOT / "gdsync")


def bash_load_config() -> dict:
    """Ejecuta el load_config real del CLI y devuelve las variables resultantes."""
    script = (
        f"source <(sed -n '/^load_config()/,/^}}/p' {GDSYNC}); "
        f"CONFIG_FILE={core.CONFIG_FILE}; REMOTE_NAME=; REMOTE_PATH=; LOCAL_PATH=; INTERVAL=; load_config; "
        "printf '%s\\0' \"$REMOTE_NAME\" \"$REMOTE_PATH\" \"$LOCAL_PATH\" \"$INTERVAL\""
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    return dict(zip(core.CONFIG_KEYS, out.split("\0")[:4]))


def cli(*args, **env) -> subprocess.CompletedProcess:
    return subprocess.run([GDSYNC, *args], capture_output=True, text=True, env={**os.environ, **env}, timeout=30)


class TestLocalPath(unittest.TestCase):
    def test_accepts_home_subdir_and_mounts(self):
        for p in (HOME / "Drive", HOME / "a/b", Path("/media/user/disk/Drive")):
            core.check_local_path(str(p))

    def test_rejects_dangerous(self):
        for p in (str(HOME), "/", "/etc", "/usr/share/x", "relativo/x", str(HOME / ".ssh"),
                  str(HOME / ".config/gdsync"), str(HOME / "a/../.gnupg/x"), "/media", "/tmp/x\nExecStart=x"):
            with self.subTest(p=p), self.assertRaises(core.CoreError):
                core.check_local_path(p)

    def test_rejects_symlink_escape(self):
        link = HOME / "trampa"
        link.symlink_to(HOME / ".ssh")
        with self.assertRaises(core.CoreError):
            core.check_local_path(str(link))

    def test_cli_and_app_agree(self):
        src = f"source <(sed -n '/^die()/p;/^check_local_path()/,/^}}/p' {GDSYNC}); check_local_path \"$1\""
        for p in (str(HOME / "Drive"), str(HOME), "/etc/x", str(HOME / ".config/z"), "/media/u/d"):
            ok_cli = subprocess.run(["bash", "-c", src, "_", p], capture_output=True).returncode == 0
            try:
                core.check_local_path(p)
                ok_py = True
            except core.CoreError:
                ok_py = False
            self.assertEqual(ok_cli, ok_py, p)


class TestConfig(unittest.TestCase):
    def test_roundtrip_python_to_bash(self):
        cfg = {"REMOTE_NAME": "gdrive", "REMOTE_PATH": "it's \"raro\" $HOME %i `x`",
               "LOCAL_PATH": str(HOME / "Drive con espacios"), "INTERVAL": "5min"}
        core.write_config(cfg)
        self.assertEqual(bash_load_config(), cfg)

    def test_roundtrip_bash_to_python(self):
        script = (f"source <(sed -n '/^info()/p;/^squote()/p;/^save_config()/,/^}}/p' {GDSYNC}); "
                  f"CONFIG_DIR={core.CONFIG_DIR}; CONFIG_FILE={core.CONFIG_FILE}; REMOTE_NAME=r1; "
                  "REMOTE_PATH=\"a'b \\$c\"; LOCAL_PATH=/media/x/y; INTERVAL=1h; save_config")
        subprocess.run(["bash", "-c", script], check=True, capture_output=True)
        cfg = core.read_config()
        self.assertEqual(cfg["REMOTE_PATH"], "a'b $c")
        self.assertEqual(cfg["INTERVAL"], "1h")

    def test_bash_never_executes_config(self):
        marker = TMP / "PWNED"
        core.CONFIG_FILE.write_text(f"REMOTE_PATH=$(touch {marker})\nEVIL=`touch {marker}`\nINTERVAL=5min;touch {marker}\n")
        bash_load_config()
        self.assertFalse(marker.exists())

    def test_config_is_private(self):
        core.write_config(dict(core.DEFAULTS))
        self.assertEqual(stat.S_IMODE(core.CONFIG_FILE.stat().st_mode), 0o600)

    def test_save_config_validation(self):
        bad = [("mal nombre", "X", str(HOME / "D"), "5min"), ("gdrive", "", str(HOME / "D"), "5min"),
               ("gdrive", "a/../b", str(HOME / "D"), "5min"), ("gdrive", "X", str(HOME / "D"), "5min\nExecStart=/bin/sh")]
        for args in bad:
            with self.subTest(args=args), self.assertRaises(core.CoreError):
                core.save_config(*args)

    def test_save_config_rejects_readonly_folder(self):
        ro = HOME / "SoloLectura"
        ro.mkdir(exist_ok=True)
        ro.chmod(0o500)
        try:
            with self.assertRaises(core.CoreError) as cm:
                core.save_config("gdrive", "X", str(ro), "5min")
            self.assertIn("permiso de escritura", str(cm.exception))
        finally:
            ro.chmod(0o700)

    def test_free_space_walks_up_to_existing_parent(self):
        self.assertIsNotNone(core.free_space(str(HOME / "no/existe/aun")))

    def test_save_config_root(self):
        core.save_config("gdrive", core.ROOT, str(HOME / "GoogleDrive"), "5min")
        self.assertEqual(core.read_config()["REMOTE_PATH"], "/")
        self.assertTrue(core.is_configured())

    def test_desktop_exec_quote(self):
        self.assertEqual(core.desktop_exec_quote("/usr/bin/gdsync-desktop"), "/usr/bin/gdsync-desktop")
        self.assertEqual(core.desktop_exec_quote('/x/a b$"%'), '"/x/a b\\$\\"%%"')


class TestBusyGuard(unittest.TestCase):
    """Con un bisync en marcha no se puede borrar su workdir (config/filtros)."""

    def setUp(self):
        self.wd = core.STATE_DIR / "bisync"
        self.wd.mkdir(parents=True, exist_ok=True)
        (self.wd / "x.path1.lst").write_text("")
        core.write_config({"REMOTE_NAME": "gdrive", "REMOTE_PATH": "X", "LOCAL_PATH": str(HOME / "D"), "INTERVAL": "5min"})

    def tearDown(self):
        for f in self.wd.glob("*"):
            f.unlink()

    def lock(self, pid):
        (self.wd / "x.lck").write_text(json.dumps({"PID": str(pid)}))

    def test_live_lock_blocks_changes(self):
        self.lock(os.getpid())
        self.assertTrue(core.sync_in_progress())
        with self.assertRaises(core.CoreError):
            core.save_filters("- *.tmp\n")
        with self.assertRaises(core.CoreError):
            core.save_config("gdrive", "Y", str(HOME / "D"), "5min")
        self.assertEqual(core.read_config()["REMOTE_PATH"], "X")
        self.assertTrue((self.wd / "x.path1.lst").exists())
        # Cambiar solo el intervalo no toca el workdir: se permite
        core.save_config("gdrive", "X", str(HOME / "D"), "10min")

    def test_stale_lock_is_ignored(self):
        p = subprocess.Popen(["true"])
        p.wait()
        self.lock(p.pid)
        self.assertFalse(core.sync_in_progress())
        core.save_filters("- *.tmp\n")
        self.assertFalse((self.wd / "x.path1.lst").exists())

    def test_bash_sync_running(self):
        src = (f"source <(sed -n '/^sync_running()/,/^}}/p' {GDSYNC}); "
               f"APP=gdsync; STATE_DIR={core.STATE_DIR}; sync_running")
        self.lock(os.getpid())
        self.assertEqual(subprocess.run(["bash", "-c", src]).returncode, 0)
        self.lock(999999999)
        self.assertNotEqual(subprocess.run(["bash", "-c", src]).returncode, 0)


class TestDiagnose(unittest.TestCase):
    LOG = [
        "2026/11/15 10:00:00 INFO  : Bisync successful",
        "2026/11/15 10:05:00 INFO  : Synching Path1",
        "2026/11/15 10:05:01 ERROR : a.txt: open /m/a.txt: permission denied",
        "2026/11/15 10:05:02 ERROR : Bisync aborted. Error is retryable without --resync due to --resilient mode.",
    ]

    def test_last_run_only(self):
        self.assertEqual(core.last_run_lines(self.LOG), self.LOG[1:])
        self.assertEqual(core.last_run_lines(self.LOG[:1]), self.LOG[:1])
        self.assertEqual(core.last_run_lines(["x"]), [])

    def test_cases(self):
        cases = {
            "permission denied": ("open-folder", self.LOG),
            "reauth": ("reauth", ["ERROR : couldn't fetch token: invalid_grant", "Bisync aborted"]),
            "resync": ("resync", ["ERROR : Bisync critical error: cannot find prior Path1 or Path2 listings"]),
            "deletes": ("resync", ["ERROR : Safety abort: too many deletes (>50%, 10 of 12)", "Bisync aborted"]),
            "quota": ("url", ["ERROR : googleapi: Error 403: Quota exceeded for quota metric", "Bisync aborted"]),
            "net": (None, ["ERROR : dial tcp: lookup www.googleapis.com: no such host", "Bisync aborted"]),
            "disk": ("open-folder", ["2026/11/15 10:00:00 gdsync ERROR: El disco de /mnt/x no está conectado"]),
        }
        for name, (action, lines) in cases.items():
            with self.subTest(name):
                self.assertEqual(core.diagnose(lines).action, action)

    def test_gdsync_error_counts_as_failed_run(self):
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        core.LOG_FILE.write_text("2026/11/15 10:00:00 INFO  : Bisync successful\n"
                                 "2026/11/15 11:00:00 gdsync ERROR: algo\n")
        try:
            self.assertIs(core.get_status().last_ok, False)
        finally:
            core.LOG_FILE.unlink()


class TestTracker(unittest.TestCase):
    LOG = [
        '2026/11/15 22:34:23 INFO  : Synching Path1 "Mio{X}:/" with Path2 "/m/GoogleDrive/"',
        "2026/11/15 22:34:23 INFO  : Copying Path2 files to Path1",
        "2026/11/15 22:34:30 INFO  : a/b.txt: Copied (new)",
        "2026/11/15 22:34:31 INFO  : a/c.burp.conflict1: Copied (new)",
        "2026/11/15 22:34:31 INFO  : a/dir: Made directory with metadata (mtime=x)",
        "Transferred:   \t    1.5 GiB / 3.651 GiB, 41%, 8.048 MiB/s, ETA 4m30s",
        "Checks:             21216 / 21216, 100%, Listed 53230",
        "Transferred:           12 / 33, 36%",
    ]

    def test_progress_and_changes(self):
        t = core.SyncTracker()
        t.feed(self.LOG)
        self.assertTrue(t.running)
        self.assertEqual((t.phase, t.direction, t.percent), ("Subiendo a Drive", "up", 41))
        self.assertEqual((t.files_done, t.files_total, t.listed), (12, 33, 53230))
        self.assertIn("1.5 GiB de 3.651 GiB (41 %)", t.summary())
        self.assertIn("quedan 4m30s", t.summary())
        self.assertEqual([(c.path, c.kind, c.direction) for c in t.changes],
                         [("a/b.txt", "new", "up"), ("a/c.burp.conflict1", "conflict", "up")])
        t.feed(["2026/11/15 22:40:00 INFO  : Bisync successful"])
        self.assertFalse(t.running)
        self.assertEqual(len(t.changes), 2)  # el historial de cambios se conserva

    def test_bisync_conflict_and_download(self):
        t = core.SyncTracker()
        t.feed(['2026/11/15 10:00:00 INFO  : Synching Path1 "r:" with Path2 "/l/"',
                "2026/11/15 10:00:01 NOTICE: - WARNING  New or changed in both paths  - doc.odt",
                "2026/11/15 10:00:02 INFO  : - Path1    Do queued copies to - Path2",
                "2026/11/15 10:00:03 INFO  : x.pdf: Copied (replaced existing)"])
        self.assertEqual([(c.path, c.kind, c.direction) for c in t.changes],
                         [("doc.odt", "conflict", None), ("x.pdf", "updated", "down")])

    def test_history_is_capped(self):
        t = core.SyncTracker()
        t.feed([f"2026/11/15 10:00:00 INFO  : f{i}: Copied (new)" for i in range(core.SyncTracker.MAX + 50)])
        self.assertEqual(len(t.changes), core.SyncTracker.MAX)


class TestFilters(unittest.TestCase):
    def test_default_filters_map_to_presets(self):
        self.assertEqual(core.parse_filters(core.DEFAULT_FILTERS), ({"temp", "system", "gdrive"}, [], []))

    def test_roundtrip_keeps_custom_rules_first(self):
        text = core.build_filters({"dev"}, ["A/B"], ["+ importante/**"])
        self.assertEqual(core.parse_filters(text), ({"dev"}, ["A/B"], ["+ importante/**"]))
        rules = [l for l in text.splitlines() if l and not l.startswith("#")]
        self.assertEqual(rules[0], "+ importante/**")
        core.save_filters(text)  # el resultado pasa la validación

    def test_partial_preset_stays_as_custom(self):
        presets, _, custom = core.parse_filters("- *.tmp\n")
        self.assertEqual((presets, custom), (set(), ["- *.tmp"]))

    def test_folder_rule_path(self):
        root = HOME / "Sync"
        (root / "a b/c").mkdir(parents=True, exist_ok=True)
        self.assertEqual(core.folder_rule_path(str(root), str(root / "a b/c")), "a b/c")
        for bad in (str(root), str(HOME), str(root / "../x")):
            with self.subTest(bad=bad), self.assertRaises(core.CoreError):
                core.folder_rule_path(str(root), bad)
        (root / "raro[1]").mkdir(exist_ok=True)
        with self.assertRaises(core.CoreError):
            core.folder_rule_path(str(root), str(root / "raro[1]"))


class TestPause(unittest.TestCase):
    def test_seconds_until_tomorrow(self):
        from datetime import datetime
        self.assertEqual(core.seconds_until_tomorrow(8, datetime(2026, 11, 15, 23, 0)), 9 * 3600)
        self.assertEqual(core.seconds_until_tomorrow(8, datetime(2026, 11, 16, 7, 0)), 3600)
        self.assertEqual(core.seconds_until_tomorrow(8, datetime(2026, 12, 31, 9, 0)), 23 * 3600)

    def test_status_reports_pause(self):
        props = {
            "gdsync.timer": {"UnitFileState": "enabled", "ActiveState": "inactive"},
            "gdsync.service": {"ActiveState": "inactive", "Result": "success"},
            "gdsync-resume.timer": {"ActiveState": "active", "NextElapseUSecRealtime": "Mon 2026-11-16 08:00:00 -03"},
        }
        orig = core.systemd_props
        core.systemd_props = lambda unit, _p: props.get(unit, {})
        try:
            core.write_config({"REMOTE_NAME": "gdrive", "REMOTE_PATH": "X", "LOCAL_PATH": str(HOME / "D"), "INTERVAL": "5min"})
            st = core.get_status()
        finally:
            core.systemd_props = orig
        self.assertEqual(st.paused_until.strftime("%H:%M"), "08:00")
        self.assertIsNone(st.next_run)

    def test_disable_cancels_pending_resume(self):
        logdir = TMP / "sclog"
        logdir.mkdir(exist_ok=True)
        log = logdir / "calls"
        fake = logdir / "systemctl"
        fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 0\n')
        fake.chmod(0o755)
        r = cli("disable", PATH=f"{logdir}:{os.environ['PATH']}")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = log.read_text().splitlines()
        self.assertIn("--user stop gdsync-resume.timer", calls)
        self.assertLess(calls.index("--user stop gdsync-resume.timer"),
                        calls.index("--user disable --now gdsync.timer"))


class TestCLI(unittest.TestCase):
    def setUp(self):
        for unit in ("gdsync.service", "gdsync.timer"):
            (core.UNIT_DIR / unit).unlink(missing_ok=True)
        core.write_config({"REMOTE_NAME": "gdrive", "REMOTE_PATH": "X", "LOCAL_PATH": str(HOME / "D"), "INTERVAL": "5min"})

    def test_security_gate_blocks_vulnerable_version(self):
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        cache = core.STATE_DIR / "update.json"
        cache.write_text(json.dumps({"version": "99.0.0", "min_supported": "99.0.0"}))
        future = time.time() + 3600  # caché «fresca»: no va a la red
        os.utime(cache, (future, future))
        try:
            r = cli("sync")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("vulnerabilidad", r.stderr)
        finally:
            cache.unlink()

    def test_gate_ignores_garbage_min_supported(self):
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        cache = core.STATE_DIR / "update.json"
        cache.write_text(json.dumps({"version": "1.0.0", "min_supported": "1\n2"}))
        future = time.time() + 3600
        os.utime(cache, (future, future))
        src = (f"source <(sed -n '/^die()/p;/^version_ge()/p;/^security_gate()/,/^}}/p' {GDSYNC}); "
               f"STATE_DIR={core.STATE_DIR}; UPDATE_CACHE={cache}; LOG_FILE=/dev/null; VERSION=1.0.0; security_gate")
        try:
            self.assertEqual(subprocess.run(["bash", "-c", src], capture_output=True).returncode, 0)
        finally:
            cache.unlink()

    def test_enable_rejects_bad_interval(self):
        core.CONFIG_FILE.write_text(f"REMOTE_NAME=gdrive\nREMOTE_PATH=X\nLOCAL_PATH={HOME}/D\nINTERVAL='5min;rm -rf ~'\n")
        r = cli("enable")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Intervalo inválido", r.stderr)
        self.assertFalse((core.UNIT_DIR / "gdsync.timer").exists())

    def test_enable_neutralizes_multiline_injection(self):
        # Comilla sin cerrar que intenta colar una directiva: la línea se descarta
        core.CONFIG_FILE.write_text("REMOTE_NAME=gdrive\nREMOTE_PATH='X %h $USER'\n"
                                    f"LOCAL_PATH={HOME}/D\nINTERVAL='5min\nExecStartPre=/bin/sh -c evil'\n")
        r = cli("enable")
        self.assertEqual(r.returncode, 0, r.stderr)
        service = (core.UNIT_DIR / "gdsync.service").read_text()
        timer = (core.UNIT_DIR / "gdsync.timer").read_text()
        self.assertNotIn("ExecStartPre", service + timer)
        self.assertIn("OnUnitInactiveSec=5min", timer)
        self.assertIn("(X %%h $$USER)", service)  # especificadores systemd escapados

    def test_enable_accepts_root(self):
        core.write_config({"REMOTE_NAME": "gdrive", "REMOTE_PATH": "/", "LOCAL_PATH": str(HOME / "D"), "INTERVAL": "5min"})
        r = cli("enable")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_validate_rejects_absolute_subpath(self):
        core.write_config({"REMOTE_NAME": "gdrive", "REMOTE_PATH": "/X", "LOCAL_PATH": str(HOME / "D"), "INTERVAL": "5min"})
        r = cli("enable")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Carpeta de Drive inválida", r.stderr)

    def test_unmounted_disk_is_refused_and_logged(self):
        local = "/mnt/gdsync-test-no-existe/Drive"
        core.write_config({"REMOTE_NAME": "gdrive", "REMOTE_PATH": "X", "LOCAL_PATH": local, "INTERVAL": "5min"})
        r = cli("sync")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no está conectado", r.stderr)
        self.assertFalse(Path(local).exists())
        self.assertIn("gdsync ERROR: El disco", core.LOG_FILE.read_text())
        core.LOG_FILE.unlink()

    def test_sd_escape(self):
        src = f"source <(sed -n '/^sd_escape()/p' {GDSYNC}); sd_escape \"$1\""
        out = subprocess.run(["bash", "-c", src, "_", "a%b$c"], capture_output=True, text=True).stdout
        self.assertEqual(out, "a%%b$$c")


class TestUpdater(unittest.TestCase):
    def info(self, latest="1.2.0", min_supported="0.0.1", assets=None):
        return updater._from_json({"version": latest, "min_supported": min_supported, "assets": assets or {}})

    def test_flags(self):
        cur = updater.__version__
        self.assertTrue(self.info("99.0.0", "99.0.0").mandatory)
        self.assertFalse(self.info("99.0.0", cur).mandatory)
        self.assertFalse(self.info(cur, "0.0.1").available)

    def test_download_rejects_foreign_or_mismatched_url(self):
        good = f"{updater.ALLOWED_PREFIX}v1.2.0/gdsync_1.2.0_all.deb"
        cases = [
            {"url": "https://evil.example/gdsync.deb", "sha256": "a" * 64},
            {"url": f"{updater.ALLOWED_PREFIX}v1.0.0/gdsync_1.0.0_all.deb", "sha256": "a" * 64},  # versión vieja
            {"url": f"{updater.ALLOWED_PREFIX}v1.2.0/../v1.0.0/x.deb", "sha256": "a" * 64},
            {"url": good, "sha256": "nope"},
            {"url": "http://" + good[len("https://"):], "sha256": "a" * 64},
        ]
        for asset in cases:
            with self.subTest(asset=asset), self.assertRaises(core.CoreError):
                updater._download(asset, TMP / "x", "1.2.0")

    def test_apply_refuses_downgrade(self):
        with self.assertRaises(core.CoreError):
            updater.apply(self.info(latest="0.0.1"))

    def test_cache_validation(self):
        updater.CACHE.parent.mkdir(parents=True, exist_ok=True)
        updater.CACHE.write_text(json.dumps({"version": "<b>x</b>", "min_supported": "1.0.0"}))
        self.assertIsNone(updater.cached())
        updater.CACHE.write_text(json.dumps({"version": "1.2.3", "min_supported": "1.0.0"}))
        self.assertEqual(updater.cached().latest, "1.2.3")

    def test_parse_version(self):
        self.assertGreater(updater.parse_version("1.10.0"), updater.parse_version("1.9.9"))


if __name__ == "__main__":
    unittest.main()
