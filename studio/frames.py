"""Pull start / middle / end keyframes out of an mp4 for Claude to review."""

from __future__ import annotations

from pathlib import Path


def extract_keyframes(video: Path, out_stem: Path) -> list[Path]:
    import imageio.v2 as imageio
    from PIL import Image

    reader = imageio.get_reader(str(video), "ffmpeg")
    try:
        frames = [frame for frame in reader]
    finally:
        reader.close()
    if not frames:
        raise RuntimeError(f"no frames decoded from {video}")

    paths = []
    for label, idx in (("start", 0), ("mid", len(frames) // 2), ("end", len(frames) - 1)):
        path = out_stem.parent / f"{out_stem.name}_{label}.jpg"
        Image.fromarray(frames[idx]).convert("RGB").save(path, quality=90)
        paths.append(path)
    return paths
