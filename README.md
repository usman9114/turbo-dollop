# turbo-dollop: Claude + Wan 2.1 Reels studio

Pipeline for a **fictional, adult, AI-generated Instagram persona**:

```
persona.yaml ─▶ Claude (plan) ─▶ Wan 2.1 (render 5s 9:16 clips) ─▶ Claude vision (review) ─▶ approved/ + captions
```

1. **Plan**: Claude writes a batch of shot ideas, Wan 2.1 prompts, captions, hashtags and alt text in the persona's voice.
2. **Render**: Wan 2.1 turns each prompt into a ~5 second vertical clip (81 frames at 16 fps). It uses image-to-video when you supply reference stills, which keeps the face consistent.
3. **Review**: Claude looks at the start, middle and end keyframes of each clip. It rejects anything that looks underage, isn't Instagram-safe, lacks an AI disclosure, or has visible AI glitches. When it rejects a clip it suggests a prompt fix.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # planning + review only
pip install -r requirements-gpu.txt      # on the GPU machine, for rendering
export ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

```bash
# 1. Claude plans 6 clips
python -m studio plan --persona personas/example.yaml --count 6 --brief "summer week, pool and rooftop"

# 2. Wan 2.1 renders them (GPU)
python -m studio render out/<batch>/plan.json --t2v t2v-1.3b --i2v i2v-480p

# 3. Claude reviews: clips land in out/<batch>/approved or rejected, each with a .json caption file
python -m studio review out/<batch>/plan.json

# or all three at once
python -m studio run --persona personas/example.yaml --count 3
```

Re-running `render` skips clips that already exist. Delete a rejected clip and run `render --force` (or edit its prompt in `plan.json`) to retry.

## Choosing a Wan 2.1 model

| `--t2v` / `--i2v` | Model | Use | GPU (rough guide) |
|---|---|---|---|
| `t2v-1.3b` | Wan2.1-T2V-1.3B | Cheap tests, 480p | ~8–12 GB with `--offload` (RTX 3060/4070 and up). About 4 min per clip on a 4090 |
| `t2v-14b` | Wan2.1-T2V-14B | Best text-to-video, 720p | 48–80 GB (A100/H100/L40S), or 24 GB with `--offload` (slow) |
| `i2v-480p` | Wan2.1-I2V-14B-480P | **Recommended**: animates your persona stills | 48–80 GB, or 24 GB with `--offload` |
| `i2v-720p` | Wan2.1-I2V-14B-720P | Final-quality image-to-video | 80 GB |

Text-to-video **can't hold the same face** from clip to clip. For a consistent character:

1. Make stills of the persona with Flux or SDXL plus a character LoRA (train it with `ostris/ai-toolkit` or `kohya_ss`, or use ComfyUI with PuLID or InstantID).
2. Drop the stills in `personas/references/` and list them under `reference_images` in the persona YAML, each with a one-line description.
3. Claude then plans `i2v` shots that pick a matching still, and Wan animates it.

Timing guide on a rented A100/H100: the 14B models take roughly **5–15 minutes per 5 second clip** at 50 steps. `--steps 30` is faster with slightly lower quality.

## Cost per clip (rough)

| Item | Cost |
|---|---|
| Claude plan (per batch of ~6 clips) | ~$0.05–0.20 |
| Claude review (3 frames per clip) | ~$0.01–0.03 |
| GPU: 1.3B on a cloud RTX 4090 (~$0.35–0.70/hr) | ~$0.03–0.05 |
| GPU: 14B I2V on a cloud A100/H100 (~$1.50–3/hr) | ~$0.20–0.75 |

That works out to roughly **$15–60/month** for a daily-posting schedule, including re-renders. Check current prices on RunPod or Vast.ai.

Set `STUDIO_CLAUDE_MODEL` to change the Claude model. A cheaper model works fine for review.

## Running on RunPod or Vast.ai

1. Rent a pod: an RTX 4090 for 1.3B, or an A100 80GB / H100 for the 14B models. Use a PyTorch template.
2. Run `git clone` on this repo, then `pip install -r requirements-gpu.txt`.
3. Run `export ANTHROPIC_API_KEY=...` and `export HF_HUB_ENABLE_HF_TRANSFER=1` (the model weights are 30–70 GB).
4. Mount a network volume at `~/.cache/huggingface` so weights download only once.
5. Copy `out/<batch>/approved/` back down, add music and cover text in CapCut, then post or schedule.

## Content rules built into the prompts

Both Claude prompts and the Wan negative prompt enforce these rules. They also keep the account from getting banned:

- The persona is fictional, and the YAML enforces an age of **21+**. Prompts never use youth-coded wording.
- Content is non-explicit and Instagram-safe: glamour, fitness, swimwear and boudoir *mood*. No nudity, no sexual acts, no solicitation.
- No real people's likenesses and no brand logos.
- Every caption carries an AI disclosure. Also switch on Instagram's **"AI info"** label when you post.
