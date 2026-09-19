#!/usr/bin/env bash
# Builds the three Lambda layers. Needs no container runtime: a static ffmpeg
# binary plus manylinux wheels cross-installed for the Lambda arm64 runtime.
set -euo pipefail

ARCH="arm64"
PY_VERSION="3.13"
PIP_PLATFORM="manylinux2014_aarch64"
FFMPEG_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${ARCH}-static.tar.xz"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$ROOT/build"

rm -rf "$BUILD"
mkdir -p "$BUILD"

wheels() {
  local name="$1"; shift
  echo "==> layer: $name ($*)"
  local target="$BUILD/$name/python"
  mkdir -p "$target"
  pip3 install --quiet \
    --platform "$PIP_PLATFORM" \
    --implementation cp \
    --python-version "$PY_VERSION" \
    --only-binary=:all: \
    --target "$target" \
    "$@"
  # Test suites and headers are dead weight inside a 250MB unzipped budget.
  find "$target" -type d \( -name tests -o -name test -o -name '__pycache__' \) -prune -exec rm -rf {} +
  find "$target" -type d -name '*.dist-info' -exec rm -rf {}/RECORD \;
  (cd "$BUILD/$name" && zip -qr "$ROOT/$name.zip" python)
  echo "    $(du -h "$ROOT/$name.zip" | cut -f1)"
}

echo "==> layer: ffmpeg"
curl -fsSL "$FFMPEG_URL" -o "$BUILD/ffmpeg.tar.xz"
mkdir -p "$BUILD/ffmpeg-layer/bin"
tar -xJf "$BUILD/ffmpeg.tar.xz" -C "$BUILD"
for tool in ffmpeg ffprobe; do
  found="$(find "$BUILD" -maxdepth 3 -type f -name "$tool" -perm -u+x | head -1)"
  [ -n "$found" ] || { echo "!! $tool missing from archive" >&2; exit 1; }
  mv "$found" "$BUILD/ffmpeg-layer/bin/$tool"
done
chmod +x "$BUILD/ffmpeg-layer/bin/"*
(cd "$BUILD/ffmpeg-layer" && zip -qr "$ROOT/ffmpeg.zip" bin)
echo "    $(du -h "$ROOT/ffmpeg.zip" | cut -f1)"

wheels pyimaging Pillow
wheels authdeps cryptography PyJWT

echo "==> done: $(ls "$ROOT"/*.zip | xargs -n1 basename | tr '\n' ' ')"
