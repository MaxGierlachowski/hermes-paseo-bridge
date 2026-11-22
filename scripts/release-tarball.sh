#!/usr/bin/env bash
# Build dist/hermes-paseo-bridge-<version>.tar.gz (+ .sha256). The tarball holds one directory,
# hermes-paseo-bridge/, which is the plugin as Hermes loads it: copy it to $HERMES_HOME/plugins/ and
# run `hermes plugins enable hermes-paseo-bridge`. Pure Python, no dependencies, no network needed.
# Reproducible: sorted entries, fixed owner and mode, mtime from SOURCE_DATE_EPOCH (default: last commit).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

version="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n1)"
[ -n "$version" ] || { echo "release-tarball: no version in pyproject.toml" >&2; exit 1; }
manifest_version="$(sed -n 's/^version: *//p' plugin.yaml | head -n1)"
code_version="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' paseo_bridge/version.py)"
if [ "$manifest_version" != "$version" ] || [ "$code_version" != "$version" ]; then
  echo "release-tarball: version mismatch: pyproject $version, plugin.yaml $manifest_version, version.py $code_version" >&2
  exit 1
fi

epoch="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || echo 0)}"
name="hermes-paseo-bridge"
out="dist/$name-$version.tar.gz"
mkdir -p dist

python3 - "$out" "$name" "$epoch" <<'PY'
import gzip, io, os, sys, tarfile
out, top, epoch = sys.argv[1], sys.argv[2], int(sys.argv[3])
files = ["plugin.yaml", "__init__.py", "README.md", "LICENSE", "NOTICE", "CHANGELOG.md"]
files += sorted(os.path.join("paseo_bridge", f) for f in os.listdir("paseo_bridge") if f.endswith(".py"))
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
    def add_dir(path):
        info = tarfile.TarInfo(path)
        info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, epoch
        tar.addfile(info)
    add_dir(top)
    add_dir(f"{top}/paseo_bridge")
    for f in sorted(files):
        data = open(f, "rb").read()
        info = tarfile.TarInfo(f"{top}/{f}")
        info.size, info.mode, info.mtime = len(data), 0o644, epoch
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        tar.addfile(info, io.BytesIO(data))
with open(out, "wb") as fh, gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=epoch, compresslevel=9) as gz:
    gz.write(buf.getvalue())
PY

if command -v sha256sum >/dev/null 2>&1; then
  (cd dist && sha256sum "$(basename "$out")" > "$(basename "$out").sha256")
else
  (cd dist && shasum -a 256 "$(basename "$out")" > "$(basename "$out").sha256")
fi
echo "$out"
cat "$out.sha256"
