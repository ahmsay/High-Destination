"""ComfyUI API-format workflow for FLUX.1 [dev] (+ optional 4x model upscale)."""
from __future__ import annotations

CHECKPOINT = "flux1-dev-fp8.safetensors"
UPSCALE_MODEL = "4x-UltraSharp.pth"
SAVE_NODE = "save"


def build(
    *,
    prompt: str,
    width: int,
    height: int,
    seed: int,
    steps: int = 28,
    guidance: float = 3.5,
    upscale: bool = True,
    filename_prefix: str = "hd",
) -> dict:
    wf: dict = {
        "ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CHECKPOINT}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["ckpt", 1]}},
        "guidance": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["pos", 0], "guidance": guidance}},
        # FLUX dev is guidance-distilled: cfg stays 1.0 and the negative prompt is ignored.
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["ckpt", 1]}},
        "latent": {
            "class_type": "EmptySD3LatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "sampler": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["ckpt", 0],
                "seed": seed,
                "steps": steps,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "positive": ["guidance", 0],
                "negative": ["neg", 0],
                "latent_image": ["latent", 0],
                "denoise": 1.0,
            },
        },
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sampler", 0], "vae": ["ckpt", 2]}},
    }
    image = ["decode", 0]
    if upscale:
        wf["upscaler"] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": UPSCALE_MODEL}}
        wf["upscale"] = {
            "class_type": "ImageUpscaleWithModel",
            "inputs": {"upscale_model": ["upscaler", 0], "image": image},
        }
        image = ["upscale", 0]
    wf[SAVE_NODE] = {"class_type": "SaveImage", "inputs": {"images": image, "filename_prefix": filename_prefix}}
    return wf
