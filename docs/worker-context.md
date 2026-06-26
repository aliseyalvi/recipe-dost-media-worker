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
- Social media: unified resolver plus bounded in-memory streaming

## Working Paths

- YouTube: public URL -> Gemini observation -> recipe normalization -> recipe card saved. Confirmed successful with `gemini-2.5-flash`.
- Social pipeline code path exists for Instagram/TikTok/Facebook when `WORKER_MODE=social_video` and `ENABLE_SOCIAL_VIDEO=true`.
- Unified social routing M14G-4SOC1: TikTok uses TikWM; Instagram/Facebook use Cobalt; YouTube stays on the native Gemini URL path.
- TikWM/Cobalt stream URLs are temporary internal values only and are never stored or returned.

## Current Blocker / Next Work

TikTok yt-dlp and Cobalt failed in Cloud Run. TikWM debug succeeded from Cloud Run:

- `POST https://www.tikwm.com/api/`
- form data: `url=<tiktok_url>`, `hd=1`
- successful response has `code=0`, `data.title`, `data.cover`, `data.hdplay` or `data.play`
- streaming the returned video URL with `requests.get(..., stream=True)` into `BytesIO` worked in Colab

Next step is M14G-4R2 full social end-to-end testing for TikTok, Instagram, and Facebook. Keep YouTube unchanged.

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
