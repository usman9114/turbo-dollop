#!/usr/bin/env python3
"""Download the audio of one or more Instagram Reels (or any yt-dlp URL) and
transcribe it locally with faster-whisper.

For each URL, writes into <out>/<shortcode>/:
  transcript.txt   timestamped lines
  transcript.srt   subtitles
  meta.json        uploader, caption, url, language, segments

Usage:
  python3 transcribe_reel.py URL [URL ...] [--out DIR] [--model small] [--language en]
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def clean_url(url: str) -> str:
    """Drop query strings / tracking tokens (e.g. ?xtok=..., ?igsh=...)."""
    p = urlsplit(url.strip())
    return urlunsplit((p.scheme or "https", p.netloc, p.path, "", ""))


def shortcode(url: str) -> str:
    m = re.search(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else re.sub(r"[^A-Za-z0-9_-]+", "_", url)[-40:]


def download_audio(url: str, workdir: Path) -> tuple[Path, dict]:
    import yt_dlp

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(workdir / "audio.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
    if not path.exists():  # extension can change after post-processing
        path = next(workdir.glob("audio.*"))
    return path, info


def load_pcm(path: Path):
    """Decode to 16 kHz mono float32 via ffmpeg (avoids PyAV version issues)."""
    import numpy as np

    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
        check=True,
        capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32)


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--out", default="out/transcripts", help="output directory (default: out/transcripts)")
    ap.add_argument("--model", default="small", help="whisper model: tiny, base, small, medium, large-v3")
    ap.add_argument("--language", default=None, help="force a language code, e.g. en; default autodetect")
    args = ap.parse_args()

    from faster_whisper import WhisperModel

    model = WhisperModel(args.model, device="auto", compute_type="int8")
    failures = 0

    for raw_url in args.urls:
        url = clean_url(raw_url)
        code = shortcode(url)
        dest = Path(args.out) / code
        dest.mkdir(parents=True, exist_ok=True)
        print(f"== {url}", file=sys.stderr)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                audio_path, info = download_audio(url, Path(tmp))
                audio = load_pcm(audio_path)
        except Exception as e:  # login wall, removed post, rate limit...
            failures += 1
            print(f"   download failed: {e}", file=sys.stderr)
            continue

        segs, tinfo = model.transcribe(audio, beam_size=5, language=args.language, vad_filter=True)
        segments = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segs]

        txt = "\n".join(f"[{s['start']:6.1f}-{s['end']:6.1f}] {s['text']}" for s in segments) + "\n"
        srt = "\n".join(
            f"{i}\n{srt_time(s['start'])} --> {srt_time(s['end'])}\n{s['text']}\n" for i, s in enumerate(segments, 1)
        )
        meta = {
            "url": url,
            "shortcode": code,
            "uploader": info.get("uploader"),
            "handle": info.get("channel") or info.get("uploader_id"),
            "caption": info.get("description"),
            "timestamp": info.get("timestamp"),
            "duration_s": round(len(audio) / 16000, 1),
            "language": tinfo.language,
            "language_probability": round(tinfo.language_probability, 2),
            "model": args.model,
            "segments": segments,
        }
        (dest / "transcript.txt").write_text(txt)
        (dest / "transcript.srt").write_text(srt)
        (dest / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))

        print(f"   {meta['handle']} | {meta['duration_s']}s | lang={meta['language']} -> {dest}", file=sys.stderr)
        print(f"# {url}  (@{meta['handle']})\n# caption: {meta['caption']!r}\n{txt}")

    return 1 if failures == len(args.urls) else 0


if __name__ == "__main__":
    sys.exit(main())
