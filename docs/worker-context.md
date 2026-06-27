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

TikWM Cloud Run debug works for TikTok, and Cobalt Cloud Run debug works for Instagram. Full TikTok app jobs reached resolver, stream, thumbnail upload, and Gemini video observation start, then hit DB-safe status and Gemini hang issues. W-M14G-4SOC2A adds Gemini timeouts and `files_api` social upload mode; W-M14G-4SOC2B keeps `media_jobs.status` DB-safe and adds duration/cost guards before streaming or Gemini; W-M14G-4SOC2C adds text-first TikTok analysis and makes social video Gemini opt-in with a whole-call watchdog.

Earlier TikWM validation:

- `POST https://www.tikwm.com/api/`
- form data: `url=<tiktok_url>`, `hd=1`
- successful response has `code=0`, `data.title`, `data.cover`, `data.hdplay` or `data.play`
- streaming the returned video URL with `requests.get(..., stream=True)` into `BytesIO` worked in Colab

Next step is deploy and retest M14G-4R2 full social end-to-end for TikTok, Instagram, and Facebook. TikTok should complete through text-first caption/title analysis or fail with `insufficient_recipe_details`. Instagram/Facebook video Gemini should only run when enabled and should complete or fail with `gemini_video_timeout`. Keep YouTube unchanged.

## Development Guardrails

- Keep controlled failures as DB status/events or safe debug errors.
- Preserve Gemini quota guards before every Gemini call.
- Free-tier defaults: max 2 Gemini calls/job, no repair call, no fallback chains, Cloud Run concurrency 1, no raw video storage.
- Social video uses `GEMINI_SOCIAL_UPLOAD_MODE=files_api` by default; `inline` is still available for debug, with the same timeout controls.
- Jobs must never remain stuck at `analyzing` progress `60`; Gemini hangs should become failed jobs with timeout-specific error codes.
- Worker code only writes DB-safe `media_jobs.status` values: `pending`, `resolving`, `downloading`, `analyzing`, `completed`, `failed`.
- Known over-limit social durations are rejected before stream download, Gemini upload, or Gemini calls. Unknown duration continues under size/memory guards.
- Development social analysis defaults: `SOCIAL_ANALYSIS_MODE=text_first`, `ENABLE_TIKTOK_TEXT_FALLBACK=true`, `ENABLE_SOCIAL_VIDEO_GEMINI=false`.
- Jobs must never remain stuck at `analyzing` progress `50`; social video Gemini timeout should become `gemini_video_timeout`.
- For social work inspect only `app/config.py`, `app/main.py`, `app/social/*`, `requirements.txt`, `README.md`, and `docs/*.md` unless blocked.

## Validation

Run before finishing:

```bash
python -m compileall app
```

On this Mac, `python` may be missing; `python3 -m compileall app` has passed.
