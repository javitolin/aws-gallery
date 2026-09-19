"""Extension-to-kind mapping, kept dependency-free so the API Lambda can use it
without pulling in Pillow.
"""
import os

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".tif", ".tiff", ".bmp"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"}
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg"}


def kind_for(key: str) -> str:
    ext = os.path.splitext(key)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return "other"
