# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

gdsync syncs **one** Google Drive folder with a local folder on Linux. It has three parts that share the same state files: a Bash CLI/sync engine (`gdsync`), a GTK4/libadwaita desktop app (`gdsync_app/`), and packaging/release tooling. User-facing text (UI, CLI, docs) is in Spanish.

## Commands

There is no test suite. CI (`.github/workflows/release.yml`) runs these lint commands:

```bash
bash -n gdsync gdsync-desktop packaging/*.sh
shellcheck -S error gdsync gdsync-desktop packaging/*.sh
python3 -m py_compile gdsync_app/*.py
```

Run from source:

```bash
./gdsync-desktop              # desktop app (same as ./gdsync ui); --background = tray only
./gdsync <cmd>                # CLI: setup|sync|dry-run|resync|status|logs|enable|disable|version
```

Build packages (output goes to `dist/`):

```bash
bash packaging/build-deb.sh
bash packaging/build-appimage.sh      # downloads appimagetool into build/ if missing
bash packaging/make-update-json.sh    # needs both artifacts; writes update.json + SHA256SUMS
```

To try the app without touching the real config, point it at temporary dirs:
`XDG_CONFIG_HOME=/tmp/x/config XDG_STATE_HOME=/tmp/x/state RCLONE_CONFIG=~/.config/rclone/rclone.conf ./gdsync-desktop`

The app is a single instance (`io.github.gdsync`). Quit a running copy from the tray menu before launching a new one, or the launch only re-activates the old process.

## Architecture

**The sync engine is the Bash script `gdsync`, not the Python app.** It wraps `rclone bisync` and writes the systemd user units (`gdsync.service` is a oneshot, `gdsync.timer` repeats it). Periodic sync therefore keeps running when the app is closed. The Python app is a front-end: it shells out to `gdsync` and `rclone`, and it reads the state the script writes.

Shared state, read and written by both sides:
- `~/.config/gdsync/config`: `KEY=value` lines written with bash `printf %q`. Bash `source`s it; Python parses it with `shlex` (`core.read_config/write_config`). The keys are `REMOTE_NAME`, `REMOTE_PATH`, `LOCAL_PATH`, `INTERVAL`.
- `~/.config/gdsync/filters.txt`: rclone filter rules.
- `~/.local/state/gdsync/bisync/`: rclone bisync workdir. If no `*.lst` file exists, the next sync runs `--resync`. Changing the folder pair or the filters **deletes this dir** on purpose (`core.reset_bisync_state`), because bisync requires a resync after either change.
- `~/.local/state/gdsync/sync.log`: rclone log. The app reads it to show live activity (`core.LogFollower`) and to find the last result, by looking for the `Bisync successful` / `Bisync aborted` lines.
- `~/.local/state/gdsync/update.json`: the cached update manifest. The CLI and the app share it.

`gdsync_app/` modules:
- `core.py`: all logic that has no UI. It handles config, rclone calls, systemd queries (`systemctl --user show`), the `Status` dataclass with its `state` property (`ok|syncing|error|paused|setup`), and `Job`, which runs a subprocess in a background thread with callbacks. "Sync now" runs `systemctl --user start gdsync.service` when the unit exists, so it never overlaps a timer run.
- `tray.py`: a hand-written StatusNotifierItem + `com.canonical.dbusmenu` server over Gio D-Bus. GTK4 has no tray API, and AppIndicator libraries are GTK3-only. Menu item ids are 1-based list indices. Call `set_menu()` to rebuild the menu; it bumps the layout revision.
- `window.py`: the Adw window with four ViewStack pages (status, folder, activity, settings). `bg(fn, done, err)` runs blocking work in a thread and returns the result on the GTK thread through `GLib.idle_add`. Never call rclone or systemctl on the GTK thread.
- `main.py`: the `Adw.Application`. It uses `hold()` so the app stays alive in the tray, polls status every 4 s, owns the current `Job`, and wires the tray, notifications, autostart and updates together. Closing the window only hides it.
- `updater.py`: update checks and self-update.

**Paths come from the file's own location.** `core.PROJECT_DIR` is the parent of the package, which holds `gdsync`, `VERSION` and `data/icons`. The same code runs from three layouts: a source checkout, the `.deb` (`/usr/lib/gdsync/`, with symlinks in `/usr/bin`), and the AppImage (`$APPDIR/usr/lib/gdsync/`). `updater.install_kind()` tells them apart by checking `$APPIMAGE` and the `/usr/lib/gdsync` prefix. The launchers `cd` into the lib dir before `python3 -m gdsync_app`, because `-m` puts the current directory first on `sys.path`.

**AppImage caveat:** the mount path is temporary. So `gdsync enable` copies the CLI to `~/.local/share/gdsync/cli/` when it runs inside an AppImage, and the systemd unit's `ExecStart` points at that copy. The app re-runs `gdsync enable` at startup whenever the timer is enabled, so the units follow the installed version after an update.

## Versioning, releases and mandatory updates

- `VERSION` is the single source of truth for the version. The Python package reads it at import time and the CLI reads it next to the script. Don't hardcode versions anywhere else.
- Every push to `main` runs CI. If the tag `v$(cat VERSION)` does not exist yet, CI builds the .deb and the AppImage, generates `update.json` and `SHA256SUMS`, test-installs the .deb, and runs `gh release create`. **Bumping `VERSION` is what publishes a release.**
- `release.json` sets `min_supported` and `advisory`. Clients below `min_supported` are blocked:
  - The app shows a dialog that cannot be closed and refuses to start sync jobs.
  - The CLI's `security_gate` (called from `sync` and `resync`) exits with an error. It checks the network at most every 6 h and falls back to the cache when offline.
  - For a security fix, set `min_supported` to the new `VERSION`. `make-update-json.sh` fails if `min_supported` is greater than `VERSION`.
- The updater only accepts asset URLs under `https://github.com/CamiloFerreira/gdsync/releases/download/` and checks SHA-256 before installing. The AppImage replaces itself atomically; the .deb is installed with `pkexec apt-get install`. After an update, the app relaunches through a detached `sh -c "sleep 1.5; exec …"` so the single-instance lock is free.

## Conventions

- Validate user input in `core` before it reaches rclone: remote names go through `valid_remote_name`, and Drive paths may not contain `..`. Never pass rclone config secrets to the UI; `list_remotes` returns only the name and type.
- rclone lives in `~/.local/bin` (installed by `gdsync install-rclone`, minimum `RCLONE_MIN=1.66.0`). Distro packages are too old for the bisync flags we use. `core.ENV` prepends `~/.local/bin` to `PATH` for every subprocess.
- Adw rows treat titles and subtitles as markup, so wrap dynamic text in `GLib.markup_escape_text`.
- Target platform is GTK 4.14 and libadwaita 1.5 (Ubuntu 24.04). Don't use APIs newer than these.
