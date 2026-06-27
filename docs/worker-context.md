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
- Social media: TikTok text-first for Render POC; non-TikTok video streaming remains guarded and opt-in

## Working Paths

- YouTube: public URL -> Gemini observation -> recipe normalization -> recipe card saved. Confirmed successful with `gemini-2.5-flash`.
- Render POC: Supabase Edge Function calls `/process-job`, which schedules FastAPI BackgroundTasks and returns `202` quickly while the background task updates Supabase.
- Social pipeline code path exists for Instagram/TikTok/Facebook when `WORKER_MODE=social_video` and `ENABLE_SOCIAL_VIDEO=true`, but Instagram/Facebook are disabled by default for Render with dedicated flags.
- Unified social routing M14G-4SOC1/W-M14R1: TikTok uses TikWM + Gemini text-first; Instagram/Facebook resolver paths are disabled until validated; YouTube stays on the native Gemini URL path.
- TikWM/Cobalt stream URLs are temporary internal values only and are never stored or returned.

## Current Blocker / Next Work

W-M14R1 implements the Render Async Worker POC in this existing repo. `/process-job` validates job state, schedules FastAPI BackgroundTasks, and returns `202` without doing Gemini work in the request. The background wrapper creates fresh dependencies, runs the job, and records `background_worker_error` if an unexpected exception escapes.

TikWM Cloud Run debug works for TikTok, and Cobalt Cloud Run debug works for Instagram. W-M14R1B routes TikTok jobs by `media_jobs.platform` into TikWM text-first before the old YouTube-only unsupported fallback. TikTok processing is strictly text-first for Render testing: TikWM metadata, duration guard, thumbnail upload, Gemini text call, recipe card save. TikTok video bytes are not streamed in this milestone. Instagram/Facebook fail with `instagram_resolver_disabled` or `facebook_resolver_disabled` until their Render resolver flags are enabled after validation.

Earlier TikWM validation:

- `POST https://www.tikwm.com/api/`
- form data: `url=<tiktok_url>`, `hd=1`
- successful response has `code=0`, `data.title`, `data.cover`, `data.hdplay` or `data.play`
- streaming the returned video URL with `requests.get(..., stream=True)` into `BytesIO` worked in Colab

Next step is deploy to Render Free and retest M14G-4R2. Queued jobs should receive `202` immediately, then reach `completed` or `failed` in the background. TikTok should complete through text-first caption/title analysis or fail with `social_caption_missing`, `insufficient_recipe_details`, `gemini_text_timeout`, or `video_duration_exceeded`. Keep YouTube unchanged.

## Development Guardrails

- Keep controlled failures as DB status/events or safe debug errors.
- Preserve Gemini quota guards before every Gemini call.
- Free-tier defaults: max 2 Gemini calls/job, no repair call, no fallback chains, Cloud Run concurrency 1, no raw video storage.
- Social video uses `GEMINI_SOCIAL_UPLOAD_MODE=files_api` by default; `inline` is still available for debug, with the same timeout controls.
- Jobs must never remain stuck at `analyzing` progress `50` or `60`; Gemini hangs should become failed jobs with timeout-specific error codes.
- Worker code only writes DB-safe `media_jobs.status` values: `queued`, `resolving`, `downloading`, `analyzing`, `completed`, `failed`.
- Initial media jobs created by `create-media-job` start as `queued`; Render async worker accepts `queued` and acquires them as `resolving`.
- Platform routing uses `media_jobs.platform` first, then source URL host fallback. TikTok should emit `tikwm_resolver_started`, not the YouTube-only unsupported message.
- Known over-limit TikTok durations are rejected before Gemini. Known over-limit video-mode social durations are rejected before stream download, Gemini upload, or Gemini calls.
- Development social analysis defaults: `SOCIAL_ANALYSIS_MODE=text_first`, `ENABLE_TIKTOK_TEXT_FALLBACK=true`, `ENABLE_SOCIAL_VIDEO_GEMINI=false`.
- Render POC defaults: `ENABLE_RENDER_INSTAGRAM_RESOLVER=false`, `ENABLE_RENDER_FACEBOOK_RESOLVER=false`, no raw video storage, no CDN URL storage, no user-facing downloads.
- For social work inspect only `app/config.py`, `app/main.py`, `app/social/*`, `requirements.txt`, `README.md`, and `docs/*.md` unless blocked.

## Validation

Run before finishing:

```bash
python -m compileall app
```

On this Mac, `python` may be missing; `python3 -m compileall app` has passed.
