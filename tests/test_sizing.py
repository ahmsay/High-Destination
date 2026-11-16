import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "worker"), str(ROOT / "cli")]

import sizing  # noqa: E402
import workflow  # noqa: E402
from hd import sizes  # noqa: E402


@pytest.mark.parametrize("w,h", [(3840, 2160), (3440, 1440), (1179, 2556), (4096, 4096), (3000, 2000)])
def test_plan_keeps_shape_and_about_one_megapixel(w, h):
    p = sizing.plan(w, h)
    assert p.gen_w % 16 == 0 and p.gen_h % 16 == 0
    assert 0.85e6 < p.gen_w * p.gen_h < 1.2e6
    assert abs(p.gen_w / p.gen_h - w / h) / (w / h) < 0.04
    assert p.upscale and not p.ratio_clamped


def test_extreme_ratio_is_clamped():
    p = sizing.plan(5120, 1440)
    assert p.ratio_clamped
    assert p.gen_w / p.gen_h <= 2.55


def test_small_target_skips_upscale():
    assert not sizing.plan(800, 450).upscale


@pytest.mark.parametrize("src,target", [((5440, 3072), (3840, 2160)), ((6144, 2560), (3440, 1440)),
                                        ((2688, 5888), (1179, 2556)), ((6464, 2560), (5120, 1440))])
def test_cover_crop_gives_exact_size(src, target):
    w, h, (l, t, r, b) = sizing.cover_crop_box(*src, *target)
    assert w >= target[0] and h >= target[1]
    assert (r - l, b - t) == target
    assert 0 <= l and 0 <= t and r <= w and b <= h


def test_fit_with_pillow():
    Image = pytest.importorskip("PIL.Image")
    out = sizing.fit(Image.new("RGB", (5440, 3072)), 3440, 1440)
    assert out.size == (3440, 1440)


def test_workflow_upscale_toggle():
    wf = workflow.build(prompt="x", width=1360, height=768, seed=1, upscale=False)
    assert "upscale" not in wf and wf[workflow.SAVE_NODE]["inputs"]["images"] == ["decode", 0]
    wf = workflow.build(prompt="x", width=1360, height=768, seed=1)
    assert wf[workflow.SAVE_NODE]["inputs"]["images"] == ["upscale", 0]


def test_cli_size_resolution():
    assert sizes.resolve(None, None, None) == (3840, 2160)
    assert sizes.resolve("ultrawide", None, None) == (3440, 1440)
    assert sizes.resolve(None, "9:16", None) == (2160, 3840)
    assert sizes.resolve(None, None, "2560x1080") == (2560, 1080)
    with pytest.raises(ValueError):
        sizes.resolve("4k", "16:9", None)
