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

    def test_desktop_exec_quote(self):
        self.assertEqual(core.desktop_exec_quote("/usr/bin/gdsync-desktop"), "/usr/bin/gdsync-desktop")
        self.assertEqual(core.desktop_exec_quote('/x/a b$"%'), '"/x/a b\\$\\"%%"')


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
