"""Hosted generation on fal.ai: no local GPU needed.

- Wan 2.1 text-to-video and image-to-video for the clips
- Flux text-to-image for candidate faces
- Flux PuLID for stills that keep the chosen face

Needs FAL_KEY in the environment (https://fal.ai/dashboard/keys).
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

from .frames import extract_keyframes
from .wan import RenderResult

WAN_T2V = {
    "fal-1.3b": "fal-ai/wan/v2.1/1.3b/text-to-video",  # cheapest, 480p
    "fal-14b": "fal-ai/wan-t2v",  # better quality, 480p/720p
}
WAN_I2V = "fal-ai/wan-i2v"
FLUX_T2I = "fal-ai/flux-lora"
FLUX_PULID = "fal-ai/flux-pulid"

STILL_NEGATIVE = (
    "bad quality, worst quality, text, signature, watermark, logo, extra limbs, extra fingers, "
    "deformed hands, nudity, childlike"
)

_uploads: dict[Path, str] = {}


def _fal():
    import fal_client

    return fal_client


def _upload(path: Path) -> str:
    path = path.resolve()
    if path not in _uploads:
        _uploads[path] = _fal().upload_file(path)
    return _uploads[path]


def _download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=300) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    return dest


def _run(endpoint: str, arguments: dict) -> dict:
    def log(update):
        for entry in getattr(update, "logs", None) or []:
            print("   ", entry.get("message", ""), flush=True)

    return _fal().subscribe(endpoint, arguments=arguments, with_logs=True, on_queue_update=log)


class FalWanRenderer:
    """Same render() result as the local WanRenderer, but runs on fal.ai."""

    def __init__(self, t2v_model: str = "fal-1.3b", resolution: str = "480p"):
        if t2v_model not in WAN_T2V:
            raise ValueError(f"unknown fal t2v model {t2v_model}; choose from {list(WAN_T2V)}")
        self.t2v_model = t2v_model
        self.resolution = resolution

    def render(
        self,
        mode: str,
        prompt: str,
        negative_extra: str,
        out_stem: Path,
        reference: Path | None = None,
        seed: int = 0,
    ) -> RenderResult:
        from .wan import BASE_NEGATIVE

        common = {"prompt": prompt, "seed": seed, "enable_safety_checker": True}
        if mode == "i2v":
            if reference is None:
                raise ValueError("image-to-video needs a reference still")
            endpoint = WAN_I2V
            args = {
                **common,
                "image_url": _upload(reference),
                "resolution": self.resolution,
                "num_frames": 81,
                "frames_per_second": 16,
                "enable_prompt_expansion": False,
            }
        elif self.t2v_model == "fal-1.3b":
            endpoint = WAN_T2V["fal-1.3b"]
            negative = BASE_NEGATIVE + (", " + negative_extra if negative_extra.strip() else "")
            args = {**common, "aspect_ratio": "9:16", "negative_prompt": negative}
        else:
            endpoint = WAN_T2V["fal-14b"]
            args = {
                **common,
                "aspect_ratio": "9:16",
                "resolution": self.resolution,
                "enable_prompt_expansion": False,
            }

        result = _run(endpoint, args)
        video = _download(result["video"]["url"], out_stem.with_suffix(".mp4"))
        return RenderResult(video=video, keyframes=extract_keyframes(video, out_stem))

    def unload(self) -> None:
        pass


def _save_images(result: dict, out_dir: Path, stem: str) -> list[Path]:
    flags = result.get("has_nsfw_concepts") or []
    saved = []
    for i, image in enumerate(result.get("images", [])):
        if i < len(flags) and flags[i]:
            print(f"   dropped {stem}-{i}: flagged by fal safety checker")
            continue
        suffix = ".png" if image.get("content_type") == "image/png" else ".jpg"
        saved.append(_download(image["url"], out_dir / f"{stem}-{i}{suffix}"))
    return saved


def generate_faces(look: str, out_dir: Path, count: int, seed: int = 0) -> list[Path]:
    """Candidate face portraits from the persona's description. Pick one as face_image."""
    prompt = (
        f"Close-up studio portrait photo of a {look}. Looking straight at the camera, neutral "
        "friendly expression, soft even lighting, plain light-grey background, sharp focus on the "
        "face, natural skin texture, 85mm lens, photorealistic."
    )
    result = _run(
        FLUX_T2I,
        {
            "prompt": prompt,
            "image_size": "portrait_4_3",
            "num_images": count,
            "seed": seed,
            "enable_safety_checker": True,
            "output_format": "jpeg",
        },
    )
    return _save_images(result, out_dir, "face")


def generate_still(face: Path, prompt: str, out_dir: Path, stem: str, seed: int = 0) -> list[Path]:
    """A vertical photo of the persona in a new outfit/setting, keeping the face from `face`."""
    result = _run(
        FLUX_PULID,
        {
            "prompt": prompt,
            "reference_image_url": _upload(face),
            "negative_prompt": STILL_NEGATIVE,
            "image_size": "portrait_16_9",
            "seed": seed,
            "enable_safety_checker": True,
        },
    )
    return _save_images(result, out_dir, stem)
