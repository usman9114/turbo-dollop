# turbo-dollop: Claude + Wan 2.1 Reels studio

> This repo also contains **`hunter/`**, a deal finder for repairing and reselling used electronics bought on Kleinanzeigen and eBay. See [hunter/README.md](hunter/README.md).

Pipeline for a **fictional, adult, AI-generated Instagram persona**:

```
persona.yaml ─▶ face + stills (fal.ai Flux) ─▶ Claude (plan) ─▶ Wan 2.1 on fal.ai (5s 9:16 clips) ─▶ Claude vision (review) ─▶ approved/ + captions
```

0. **Look**: fal.ai generates a face for the persona once, then reference stills in different outfits and settings that keep that face.
1. **Plan**: Claude writes a batch of shot ideas, Wan 2.1 prompts, captions, hashtags and alt text in the persona's voice.
2. **Render**: Wan 2.1 on fal.ai turns each prompt into a ~5 second vertical clip (81 frames at 16 fps). It animates a reference still, which keeps the face consistent.
3. **Review**: Claude looks at the start, middle and end keyframes of each clip. It rejects anything that looks underage, isn't Instagram-safe, lacks an AI disclosure, or has visible AI glitches. When it rejects a clip it suggests a prompt fix.

## Setup (no GPU needed)

All the heavy generation runs on **fal.ai** (pay per image or clip). Claude runs through the Anthropic API. Any laptop works.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...   # console.anthropic.com -> API keys
export FAL_KEY=...                     # fal.ai/dashboard/keys
```

A Claude.ai Pro or Max subscription doesn't include API usage. The API key is billed separately, but this pipeline uses only cents per batch.

## Usage

### 1. One-time: give the persona a face

```bash
python -m studio faces --persona personas/example.yaml --count 4
```
This saves candidate portraits to `personas/references/faces/`. Pick one and add it to the YAML:
```yaml
face_image: references/faces/face-2.jpg
```

### 2. Make reference stills (repeat whenever you want new outfits or settings)

```bash
python -m studio stills --persona personas/example.yaml --count 6 --brief "summer, pool and rooftop"
```
Claude plans outfits, settings and poses. fal.ai (Flux + PuLID) renders vertical photos that **keep the chosen face**. They're saved to `personas/references/` and listed in `personas/example.references.json`. Delete any you don't like from both.

The stills also work as normal photo posts.

### 3. Weekly Reels

```bash
python -m studio run --persona personas/example.yaml --count 5
```
or step by step:
```bash
python -m studio plan   --persona personas/example.yaml --count 5   # Claude: prompts + captions
python -m studio render out/<batch>/plan.json                       # fal.ai Wan 2.1
python -m studio review out/<batch>/plan.json                       # Claude: approve / reject
```
Approved clips go to `out/<batch>/approved/`, each with a `.json` holding the caption, hashtags, alt text and cover text. Rejected clips include a suggested prompt fix. Edit `plan.json` and run `render --force` to retry.

Render options: `--resolution 720p` (sharper, costs more) and `--fal-t2v fal-14b` (better text-to-video when a clip doesn't use a still).

## Cost (hosted, approximate; check fal.ai/pricing)

| Step | Approx. cost |
|---|---|
| Faces (4 portraits, one time) | ~$0.10–0.20 |
| Stills (Flux PuLID) | ~$0.03–0.06 each |
| Wan 2.1 image-to-video, 5s at 480p / 720p | ~$0.20 / ~$0.40 per clip |
| Wan 2.1 1.3B text-to-video | ~$0.05–0.10 per clip |
| Claude plan + review | ~$0.05–0.25 per batch |

Posting one Reel a day, with about a third re-rendered, works out to **roughly $10–25/month**. fal.ai's safety checker stays on, and any flagged image is dropped automatically.

## Optional: run Wan locally on your own GPU

`python -m studio render out/<batch>/plan.json --backend local` uses Hugging Face diffusers instead of fal.ai. Install `requirements-gpu.txt` on a CUDA machine first.

### Local model choice

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

### Local GPU cost per clip (rough)

| Item | Cost |
|---|---|
| Claude plan (per batch of ~6 clips) | ~$0.05–0.20 |
| Claude review (3 frames per clip) | ~$0.01–0.03 |
| GPU: 1.3B on a cloud RTX 4090 (~$0.35–0.70/hr) | ~$0.03–0.05 |
| GPU: 14B I2V on a cloud A100/H100 (~$1.50–3/hr) | ~$0.20–0.75 |

That works out to roughly **$15–60/month** for a daily-posting schedule, including re-renders. Check current prices on RunPod or Vast.ai.

Set `STUDIO_CLAUDE_MODEL` to change the Claude model. A cheaper model works fine for review.

### Running on RunPod or Vast.ai

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
