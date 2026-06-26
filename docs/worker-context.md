# Recipe Dost Media Worker Context

Compact repo context for future Codex turns.

## Product

Recipe Dost turns public/authorized recipe video links into structured recipe cards.

The worker may extract metadata and temporary in-memory media bytes, call Gemini, save recipe cards, and upload source thumbnails. It must not provide user-facing downloads, back up deleted videos, store raw videos, commit cookies/secrets, or log full source URLs, stream URLs, cookies, Gemini prompts/raw responses, ingredients, or steps.

## Current Architecture

- Cloud Run service: `recipe-dost-media-worker`
- Trigger: Supabase Edge Function creates `media_jobs` and calls `/process-job`
- DB writes: `media_jobs`, `media_job_events`, `recipe_cards`, best-effort `recipe_items.thumbnail_url`
- AI: Gemini two-stage pipeline with quota guards
- Social media: yt-dlp resolver plus bounded in-memory streaming

## Working Paths

- YouTube: public URL -> Gemini observation -> recipe normalization -> recipe card saved. Confirmed successful with `gemini-2.5-flash`.
- Social pipeline code path exists for Instagram/TikTok/Facebook when `WORKER_MODE=social_video`, `ENABLE_SOCIAL_VIDEO=true`, and `ENABLE_YT_DLP=true`.
- TikTok yt-dlp parser fix M14G-4R1 is implemented: parse `formats[]`, select direct non-HLS MP4, preserve selected `http_headers`, stream via `requests` to bounded `BytesIO`.

## Current Blocker / Next Work

TikTok still needs Cloud Run proof. yt-dlp and Cobalt have failed in Cloud Run, while TikWM was validated externally in Colab:

- `POST https://www.tikwm.com/api/`
- form data: `url=<tiktok_url>`, `hd=1`
- successful response has `code=0`, `data.title`, `data.cover`, `data.hdplay` or `data.play`
- streaming the returned video URL with `requests.get(..., stream=True)` into `BytesIO` worked in Colab

M14G-4TWM0 added a protected debug-only `/debug/tikwm` endpoint. Next step is M14G-4TWM1: prove that endpoint from Cloud Run. Do not connect Gemini, media jobs, recipe saving, or raw media storage yet. Never return or log the TikWM video URL.

## Development Guardrails

- Keep controlled failures as DB status/events or safe debug errors.
- Preserve Gemini quota guards before every Gemini call.
- Free-tier defaults: max 2 Gemini calls/job, no repair call, no fallback chains, Cloud Run concurrency 1, no raw video storage.
- For social work inspect only `app/config.py`, `app/main.py`, `app/social/*`, `requirements.txt`, `README.md`, and `docs/*.md` unless blocked.

## Validation

Run before finishing:

```bash
python -m compileall app
```

On this Mac, `python` may be missing; `python3 -m compileall app` has passed.
