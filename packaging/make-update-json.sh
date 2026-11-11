#!/usr/bin/env bash
# Genera dist/update.json (lo lee la app y el CLI para avisar/obligar a actualizar)
# y dist/SHA256SUMS.
source "$(dirname "$0")/common.sh"

BASE="https://github.com/CamiloFerreira/gdsync/releases/download/v$VERSION"
DEB="gdsync_${VERSION}_all.deb"
APPIMG="gdsync-${VERSION}-x86_64.AppImage"
cd "$DIST"
sha256sum "$DEB" "$APPIMG" > SHA256SUMS

python3 - "$ROOT/release.json" "$VERSION" "$BASE" "$DEB" "$APPIMG" > update.json <<'PY'
import hashlib, json, sys
rel_file, version, base, deb, appimg = sys.argv[1:]
rel = json.load(open(rel_file))
sha = lambda f: hashlib.sha256(open(f, "rb").read()).hexdigest()
parse = lambda v: tuple(int(x) for x in v.split("."))
if parse(rel["min_supported"]) > parse(version):
    sys.exit(f"min_supported ({rel['min_supported']}) > VERSION ({version})")
json.dump({
    "version": version,
    "min_supported": rel["min_supported"],
    "advisory": rel.get("advisory", ""),
    "url": f"https://github.com/CamiloFerreira/gdsync/releases/tag/v{version}",
    "assets": {
        "deb": {"url": f"{base}/{deb}", "sha256": sha(deb)},
        "appimage": {"url": f"{base}/{appimg}", "sha256": sha(appimg)},
    },
}, sys.stdout, indent=2)
PY
echo "$DIST/update.json"
