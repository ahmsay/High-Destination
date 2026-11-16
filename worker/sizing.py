"""Aspect-ratio planning: generate ~1 MP in the target shape, upscale, crop to exact pixels."""
from __future__ import annotations

import math
from dataclasses import dataclass

MAX_GEN_RATIO = 2.5  # FLUX composes badly beyond this; wider targets get cropped (outpaint: TODO)
MULTIPLE = 16
UPSCALE_FACTOR = 4


@dataclass(frozen=True)
class Plan:
    target_w: int
    target_h: int
    gen_w: int
    gen_h: int
    upscale: bool  # run the 4x upscale model before the final resize/crop
    ratio_clamped: bool  # target is wider/taller than MAX_GEN_RATIO


def _round(x: float) -> int:
    return max(MULTIPLE, int(round(x / MULTIPLE)) * MULTIPLE)


def plan(target_w: int, target_h: int, megapixels: float = 1.0) -> Plan:
    if target_w <= 0 or target_h <= 0:
        raise ValueError("target size must be positive")
    ratio = target_w / target_h
    gen_ratio = min(max(ratio, 1 / MAX_GEN_RATIO), MAX_GEN_RATIO)
    area = megapixels * 1024 * 1024
    gen_w = _round(math.sqrt(area * gen_ratio))
    gen_h = _round(math.sqrt(area / gen_ratio))
    upscale = target_w > gen_w or target_h > gen_h
    return Plan(target_w, target_h, gen_w, gen_h, upscale, ratio_clamped=gen_ratio != ratio)


def cover_crop_box(src_w: int, src_h: int, target_w: int, target_h: int) -> tuple[int, int, tuple[int, int, int, int]]:
    """Scale (w, h) to cover the target, and the centered crop box (left, top, right, bottom)."""
    scale = max(target_w / src_w, target_h / src_h)
    w = max(target_w, math.ceil(src_w * scale))
    h = max(target_h, math.ceil(src_h * scale))
    left = (w - target_w) // 2
    top = (h - target_h) // 2
    return w, h, (left, top, left + target_w, top + target_h)


def fit(image, target_w: int, target_h: int):
    """Resize a PIL image to cover the target and center-crop to exact pixels."""
    from PIL import Image

    w, h, box = cover_crop_box(image.width, image.height, target_w, target_h)
    if (w, h) != image.size:
        image = image.resize((w, h), Image.LANCZOS)
    return image.crop(box)
