"""Recover a capture date from the filename.

Cameras and phones encode the date in the name far more reliably than they
write container metadata, and several of these files have none or have
epoch-relative rubbish from a camera with no clock.
"""
import re

PATTERNS = (
    # PXL_20220928_154721228, VID_20220602_215906, IMG-20180913-WA0005
    re.compile(r"(?:PXL|VID|IMG)[-_](?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})[-_]"),
    # 2017-03-25 20.37.10
    re.compile(r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})[ _]\d{2}[.:]\d{2}"),
    # 20220602_221347
    re.compile(r"\b(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})_\d{6}\b"),
    # bare 2018-09-13
    re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})\b"),
)


def from_name(name: str) -> str | None:
    for pattern in PATTERNS:
        match = pattern.search(name)
        if not match:
            continue
        year, month, day = match.group("y"), match.group("m"), match.group("d")
        if not ("1990" <= year <= "2099" and "01" <= month <= "12" and "01" <= day <= "31"):
            continue
        return f"{year}-{month}-{day}T00:00:00Z"
    return None
