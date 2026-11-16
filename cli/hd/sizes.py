"""Turn --preset / --ratio / --size into exact target pixels."""
from __future__ import annotations

import re

PRESETS: dict[str, tuple[int, int, str]] = {
    "4k": (3840, 2160, "16:9 desktop, 4K"),
    "1440p": (2560, 1440, "16:9 desktop, QHD"),
    "1080p": (1920, 1080, "16:9 desktop, FHD"),
    "5k": (5120, 2880, "16:9 Studio Display / 5K iMac"),
    "macbook": (3024, 1964, "14\" MacBook Pro"),
    "macbook-air": (2560, 1664, "13\" MacBook Air"),
    "ultrawide": (3440, 1440, "21:9 ultrawide"),
    "ultrawide-5k": (5120, 2160, "21:9 5K2K ultrawide"),
    "superwide": (5120, 1440, "32:9 super ultrawide (cropped, no outpaint yet)"),
    "laptop": (3000, 2000, "3:2 laptop"),
    "phone": (1290, 2796, "iPhone Pro Max"),
    "iphone": (1179, 2556, "iPhone / iPhone Pro"),
    "android": (1440, 3200, "20:9 Android"),
    "ipad": (2048, 2732, "iPad Pro 12.9\" portrait"),
    "square": (4096, 4096, "1:1"),
}

LONG_SIDE_FOR_RATIO = 3840  # --ratio picks this long side unless --size is given


def parse_ratio(text: str) -> tuple[float, float]:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[:x/]\s*(\d+(?:\.\d+)?)\s*", text)
    if not m or float(m[1]) <= 0 or float(m[2]) <= 0:
        raise ValueError(f"bad ratio {text!r}, expected e.g. 16:9")
    return float(m[1]), float(m[2])


def parse_size(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*[x×]\s*(\d+)\s*", text)
    if not m:
        raise ValueError(f"bad size {text!r}, expected e.g. 3440x1440")
    w, h = int(m[1]), int(m[2])
    if not (64 <= w <= 8192 and 64 <= h <= 8192):
        raise ValueError("width and height must be between 64 and 8192")
    return w, h


def resolve(preset: str | None, ratio: str | None, size: str | None) -> tuple[int, int]:
    if sum(x is not None for x in (preset, ratio, size)) > 1:
        raise ValueError("use only one of --preset, --ratio, --size")
    if size:
        return parse_size(size)
    if ratio:
        rw, rh = parse_ratio(ratio)
        if rw >= rh:
            return LONG_SIDE_FOR_RATIO, round(LONG_SIDE_FOR_RATIO * rh / rw)
        return round(LONG_SIDE_FOR_RATIO * rw / rh), LONG_SIDE_FOR_RATIO
    name = (preset or "4k").lower()
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name!r}; see `hd presets`")
    w, h, _ = PRESETS[name]
    return w, h
