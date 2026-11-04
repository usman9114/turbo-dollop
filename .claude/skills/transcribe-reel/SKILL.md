---
name: transcribe-reel
description: Transcribe Instagram Reels (or any public video URL yt-dlp supports, e.g. TikTok, YouTube Shorts) into timestamped text, SRT subtitles and metadata, locally with Whisper. Use when the user pastes an instagram.com/reel/... link, or asks to transcribe, get the words/message/script out of, or summarise what's said in a reel or short video.
---

# Transcribe Reel

Downloads a reel's audio with `yt-dlp` and transcribes it locally with
`faster-whisper`. No API key or Instagram login is needed for public reels.

## Steps

1. **Install deps** (only if missing; they are fast to check):
   ```bash
   python3 -c "import yt_dlp, faster_whisper" 2>/dev/null || pip install -q yt-dlp faster-whisper
   which ffmpeg || echo "ffmpeg missing: install it (apt-get install -y ffmpeg)"
   ```

2. **Run the script** with one or more URLs. Tracking params like `?xtok=` / `?igsh=` are stripped automatically.
   ```bash
   python3 .claude/skills/transcribe-reel/scripts/transcribe_reel.py "<URL>" [more URLs] --out out/transcripts
   ```
   - `--model small` (default) is a good speed/accuracy balance on CPU (~1 min for a 1-min reel).
     Use `base` for speed, `medium`/`large-v3` for accented speech, music-heavy audio or non-English.
   - `--language en` forces a language when autodetect guesses wrong.
   - Output per reel: `out/transcripts/<shortcode>/transcript.txt`, `transcript.srt`, `meta.json`
     (uploader, handle, caption, duration, language, segments). `out/` is gitignored.

3. **Present the result** to the user:
   - Who posted it (`@handle`), the length, and the caption. Captions often hold the CTA, like "comment X for the guide".
   - A **cleaned-up transcript**: merge the segments into paragraphs and fix obvious mishearings.
     Handles and brand names are often garbled, so check them against `handle` and the caption (e.g. "It's in the Antony" → `itstundeanthony`).
     Mark anything you can't resolve as *[unclear: "…"]* rather than guessing.
   - A one- or two-line gist of the message if useful.
   - Mention where the raw timestamped files were saved.

## Troubleshooting

- **Login required / rate-limited / "empty media response"**: the reel is private, age-gated, or Instagram is
  throttling. Retry once later; otherwise ask the user to download the video and give you the file, then run
  Whisper on that file directly (the script's `load_pcm` + model code works on any local audio/video file).
- **`av.open() got an unexpected keyword argument`**: PyAV version mismatch. The script avoids this by decoding with
  ffmpeg itself, so make sure you're calling the script and not `model.transcribe(path)` directly.
- **No speech / music only**: segments come back empty or as hallucinated filler ("Thank you."). Say so instead of presenting it as a transcript.

## Notes

- Only transcribe content the user can view publicly or owns. Downloading other people's reels can break
  Instagram's terms, so keep transcripts for personal use and don't redistribute them.
