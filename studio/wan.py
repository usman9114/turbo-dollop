"""Wan 2.1 rendering via Hugging Face diffusers (text-to-video and image-to-video).

GPU-only. torch/diffusers are imported lazily so planning and review run anywhere.
"""

from __future__ import annotations

import gc
from dataclasses import dataclass
from pathlib import Path

MODELS = {
    "t2v-1.3b": ("t2v", "Wan-AI/Wan2.1-T2V-1.3B-Diffusers", 480),
    "t2v-14b": ("t2v", "Wan-AI/Wan2.1-T2V-14B-Diffusers", 720),
    "i2v-480p": ("i2v", "Wan-AI/Wan2.1-I2V-14B-480P-Diffusers", 480),
    "i2v-720p": ("i2v", "Wan-AI/Wan2.1-I2V-14B-720P-Diffusers", 720),
}

BASE_NEGATIVE = (
    "bright tones, overexposed, static, blurred details, subtitles, text, watermark, logo, "
    "worst quality, low quality, JPEG artifacts, ugly, deformed, disfigured, extra fingers, "
    "fused fingers, poorly drawn hands, poorly drawn face, misshapen limbs, three legs, "
    "many people in the background, walking backwards, still picture, nudity, nsfw, childlike"
)

FPS = 16
NUM_FRAMES = 81  # ~5 s at 16 fps; Wan wants 4k+1 frames


@dataclass
class RenderResult:
    video: Path
    keyframes: list[Path]


class WanRenderer:
    def __init__(self, model_key: str, offload: bool = False, steps: int = 50):
        if model_key not in MODELS:
            raise ValueError(f"unknown model {model_key}; choose from {list(MODELS)}")
        self.kind, self.model_id, self.res = MODELS[model_key]
        self.offload = offload
        self.steps = steps
        self.pipe = None

    def load(self) -> None:
        import torch
        from diffusers import AutoencoderKLWan, UniPCMultistepScheduler

        vae = AutoencoderKLWan.from_pretrained(self.model_id, subfolder="vae", torch_dtype=torch.float32)
        if self.kind == "t2v":
            from diffusers import WanPipeline

            pipe = WanPipeline.from_pretrained(self.model_id, vae=vae, torch_dtype=torch.bfloat16)
        else:
            from diffusers import WanImageToVideoPipeline
            from transformers import CLIPVisionModel

            image_encoder = CLIPVisionModel.from_pretrained(
                self.model_id, subfolder="image_encoder", torch_dtype=torch.float32
            )
            pipe = WanImageToVideoPipeline.from_pretrained(
                self.model_id, vae=vae, image_encoder=image_encoder, torch_dtype=torch.bfloat16
            )
        flow_shift = 5.0 if self.res >= 720 else 3.0
        pipe.scheduler = UniPCMultistepScheduler.from_config(pipe.scheduler.config, flow_shift=flow_shift)
        if self.offload:
            pipe.enable_model_cpu_offload()  # fits smaller GPUs, slower
        else:
            pipe.to("cuda")
        self.pipe = pipe

    def unload(self) -> None:
        import torch

        self.pipe = None
        gc.collect()
        torch.cuda.empty_cache()

    def render(
        self,
        prompt: str,
        negative_extra: str,
        out_stem: Path,
        reference: Path | None = None,
        seed: int = 0,
    ) -> RenderResult:
        import numpy as np
        import torch
        from diffusers.utils import export_to_video, load_image
        from PIL import Image

        if self.pipe is None:
            self.load()
        negative = BASE_NEGATIVE + (", " + negative_extra if negative_extra.strip() else "")
        generator = torch.Generator(device="cpu").manual_seed(seed)
        kwargs = dict(
            prompt=prompt,
            negative_prompt=negative,
            num_frames=NUM_FRAMES,
            num_inference_steps=self.steps,
            guidance_scale=5.0,
            generator=generator,
        )

        if self.kind == "i2v":
            if reference is None:
                raise ValueError("image-to-video needs a reference still")
            image = load_image(str(reference))
            max_area = self.res * (832 if self.res == 480 else 1280)
            aspect = image.height / image.width
            mod = self.pipe.vae_scale_factor_spatial * self.pipe.transformer.config.patch_size[1]
            height = round(np.sqrt(max_area * aspect)) // mod * mod
            width = round(np.sqrt(max_area / aspect)) // mod * mod
            kwargs.update(image=image.resize((width, height)), height=height, width=width)
        else:
            # Vertical 9:16 for Reels.
            height, width = (832, 480) if self.res == 480 else (1280, 720)
            kwargs.update(height=height, width=width)

        frames = self.pipe(**kwargs).frames[0]

        out_stem.parent.mkdir(parents=True, exist_ok=True)
        video = out_stem.with_suffix(".mp4")
        export_to_video(frames, str(video), fps=FPS)

        keyframes = []
        for label, idx in (("start", 0), ("mid", len(frames) // 2), ("end", len(frames) - 1)):
            arr = np.asarray(frames[idx])
            if arr.dtype != np.uint8:
                arr = (np.clip(arr, 0, 1) * 255).astype(np.uint8)
            path = out_stem.parent / f"{out_stem.name}_{label}.jpg"
            Image.fromarray(arr).save(path, quality=90)
            keyframes.append(path)
        return RenderResult(video=video, keyframes=keyframes)
