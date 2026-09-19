import io
import json
import os
import subprocess
import tempfile

from PIL import Image, ImageOps

FFMPEG = "/opt/bin/ffmpeg"
FFPROBE = "/opt/bin/ffprobe"

THUMB_EDGE = 512
THUMB_QUALITY = 80
# Poster extraction streams the whole file through ffmpeg, so cap what we pull into /tmp.
MAX_TRANSCODE_BYTES = 4_500_000_000

from media_kinds import kind_for  # noqa: F401  (re-exported for the processor)


def _probe(path: str) -> dict:
    result = subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        return {}
    return json.loads(result.stdout or b"{}")


def _webp(image: Image.Image) -> bytes:
    image = ImageOps.exif_transpose(image)
    image.thumbnail((THUMB_EDGE, THUMB_EDGE))
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, "WEBP", quality=THUMB_QUALITY, method=4)
    return buffer.getvalue()


def _exif_taken(image: Image.Image):
    exif = image.getexif()
    if not exif:
        return None
    # 36867 DateTimeOriginal, 306 DateTime
    for tag in (36867, 306):
        value = exif.get(tag)
        if value:
            try:
                date, clock = str(value).split(" ")
                return f"{date.replace(':', '-')}T{clock}Z"
            except ValueError:
                continue
    return None


def from_image(path: str) -> tuple[bytes, dict]:
    with Image.open(path) as image:
        meta = {"width": image.width, "height": image.height, "taken_at": _exif_taken(image)}
        return _webp(image), meta


def from_video(path: str) -> tuple[bytes | None, dict]:
    probe = _probe(path)
    stream = next(
        (s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {}
    )
    meta = {
        "width": stream.get("width"),
        "height": stream.get("height"),
        "duration": float(probe.get("format", {}).get("duration", 0)) or None,
        "taken_at": probe.get("format", {}).get("tags", {}).get("creation_time"),
    }

    with tempfile.NamedTemporaryFile(suffix=".jpg") as frame:
        # Seek a second in so the poster is not a black lead-in frame.
        result = subprocess.run(
            [FFMPEG, "-y", "-ss", "1", "-i", path, "-frames:v", "1", "-f", "image2", frame.name],
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0 or os.path.getsize(frame.name) == 0:
            return None, meta
        with Image.open(frame.name) as image:
            return _webp(image), meta


def from_audio(path: str) -> tuple[bytes | None, dict]:
    probe = _probe(path)
    tags = probe.get("format", {}).get("tags", {})
    meta = {
        "duration": float(probe.get("format", {}).get("duration", 0)) or None,
        "taken_at": tags.get("creation_time") or tags.get("date"),
        "title": tags.get("title"),
        "artist": tags.get("artist"),
    }

    with tempfile.NamedTemporaryFile(suffix=".jpg") as art:
        result = subprocess.run(
            [FFMPEG, "-y", "-i", path, "-an", "-vcodec", "copy", art.name],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0 or os.path.getsize(art.name) == 0:
            return None, meta
        with Image.open(art.name) as image:
            return _webp(image), meta
