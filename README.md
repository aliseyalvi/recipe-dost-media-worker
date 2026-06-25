# Recipe Dost Media Worker

FastAPI Cloud Run worker for Recipe Dost media jobs.

M14G-3 supports direct analysis of public YouTube videos with Gemini. The
worker sends the public YouTube URL to Gemini without downloading or storing
the video, validates the structured bilingual result, and saves a
`recipe_cards` row.

Instagram, TikTok, Facebook, and other sources continue to use the
`needs_caption` skeleton fallback.

## Local setup

Python 3.12 or newer is recommended.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set real local values in `.env`. The Supabase service role key, Gemini API
key, and worker secret are server-only. Never ship them to the Expo app or
commit them to source control.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_ENV` | `development` | Runtime environment label |
| `WORKER_MODE` | `skeleton` | `skeleton` or `youtube_direct` |
| `WORKER_API_SECRET` | required | Shared secret required by `/process-job` |
| `SUPABASE_URL` | required | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | required | Server-only Supabase service role key |
| `GEMINI_API_KEY` | required for YouTube | Server-only Gemini Developer API key |
| `GEMINI_MODEL` | `gemini-3.5-flash` | General Gemini model fallback |
| `GEMINI_VIDEO_MODEL` | `GEMINI_MODEL` | Model used for YouTube video analysis |
| `GEMINI_VIDEO_FALLBACK_MODELS` | `gemini-2.5-flash,gemini-2.5-flash-lite` | Comma-separated fallback models |
| `GEMINI_MAX_RETRIES` | `3` | Attempts per model for temporary errors |
| `GEMINI_RETRY_BASE_SECONDS` | `2` | Exponential retry base delay |
| `MAX_VIDEO_DURATION_SECONDS` | `90` | Reserved future media limit |
| `MAX_VIDEO_SIZE_MB` | `60` | Reserved future media limit |

If `GEMINI_VIDEO_MODEL` is unset, the worker uses `GEMINI_MODEL`, then
`gemini-3.5-flash`.

M14G-3 YouTube analysis requires:

```env
WORKER_MODE=youtube_direct
GEMINI_API_KEY=replace-with-your-gemini-api-key
GEMINI_VIDEO_MODEL=gemini-3.5-flash
```

Use `WORKER_MODE=skeleton` to retain caption fallback behavior without Gemini.

## Run locally

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Health check:

```bash
curl http://localhost:8080/health
```

Process an existing media job:

```bash
curl -X POST http://localhost:8080/process-job \
  -H "Content-Type: application/json" \
  -H "X-Worker-Secret: replace-with-your-worker-secret" \
  -d '{"media_job_id":"00000000-0000-0000-0000-000000000000"}'
```

Retry a failed job:

```bash
curl -X POST http://localhost:8080/process-job \
  -H "Content-Type: application/json" \
  -H "X-Worker-Secret: replace-with-your-worker-secret" \
  -d '{"media_job_id":"00000000-0000-0000-0000-000000000000","force":true}'
```

Temporary overload, rate-limit, timeout, and unknown Gemini failures retry
automatically. Change the primary model without code by setting
`GEMINI_VIDEO_MODEL`. If `gemini-3.5-flash` remains overloaded, set it to
`gemini-2.5-flash`; fallback models are then tried in configured order.

## Free-tier quota safety

Recommended development settings:

```env
APP_ENV=development
GEMINI_VIDEO_MODEL=gemini-2.5-flash
GEMINI_TEXT_MODEL=gemini-2.5-flash
GEMINI_VIDEO_FALLBACK_MODELS=
GEMINI_MAX_RETRIES=1
GEMINI_MAX_CALLS_PER_JOB=2
GEMINI_DAILY_DEV_CALL_LIMIT=5
ENABLE_GEMINI_REPAIR_CALL=false
ENABLE_GEMINI_FALLBACKS=false
```

One app action can create multiple Gemini requests, and failed requests still
consume quota. Force retries can consume more quota. The worker blocks calls
after the configured per-job or daily development limit. Increase these limits
or enable fallbacks only after billing is enabled or broader testing is ready.

```env
GEMINI_DISABLED_MODELS=gemini-3.5-flash
GEMINI_VIDEO_MODEL=gemini-2.5-flash
GEMINI_DAILY_DEV_MODEL_LIMITS=gemini-3.5-flash:0,gemini-2.5-flash:10,gemini-2.5-flash-lite:10
GEMINI_DEV_BUDGET_RESET_AT=2026-06-25T13:30:00Z
```

Set `ENABLE_GEMINI_FALLBACKS=true` to try fallback models. Google API quota
and worker-side development budgets are separate.

`POST /process-job` returns at `resolving`/10% and processing continues in the
background. Stage A extracts plain video observations; Stage B converts those
notes into validated Recipe Dost JSON.

For free-tier development, use `GEMINI_VIDEO_MODEL=gemini-2.5-flash`,
`GEMINI_TEXT_MODEL=gemini-2.5-flash`, `GEMINI_MAX_CALLS_PER_JOB=2`,
`GEMINI_MAX_RETRIES=1`, `ENABLE_GEMINI_FALLBACKS=false`, and
`ENABLE_GEMINI_REPAIR_CALL=false`.

For a manual YouTube test, first create a `media_jobs` row through the app or
existing Supabase function with:

- `platform = youtube`
- a public `youtube.com/watch`, `youtu.be`, or `youtube.com/shorts` URL
- a valid `recipe_item_id`

Then call `/process-job` with that job ID. Successful YouTube jobs progress
through:

1. `resolving` at 10%
2. `analyzing` at 60%
3. `saving_result` at 85%
4. `completed` at 100%

The worker also sets the standard YouTube thumbnail URL and attempts to update
the related `recipe_items.thumbnail_url`. A recipe-item thumbnail failure does
not fail the media job.

Unsupported platforms progress to `needs_caption` at 100% with
`worker_skeleton_no_media_provider`.

## Validation

Compile the application:

```bash
python -m compileall app
```

Start the local server:

```bash
uvicorn app.main:app --reload --port 8080
```

## Deploy to Cloud Run

```bash
gcloud run deploy recipe-dost-media-worker \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars APP_ENV=production,WORKER_MODE=youtube_direct,GEMINI_MODEL=gemini-3.5-flash,GEMINI_VIDEO_MODEL=gemini-3.5-flash,MAX_VIDEO_DURATION_SECONDS=90,MAX_VIDEO_SIZE_MB=60 \
  --set-secrets WORKER_API_SECRET=recipe-dost-worker-api-secret:latest,SUPABASE_URL=recipe-dost-supabase-url:latest,SUPABASE_SERVICE_ROLE_KEY=recipe-dost-supabase-service-role-key:latest,GEMINI_API_KEY=recipe-dost-gemini-api-key:latest
```

`--allow-unauthenticated` allows requests to reach the container.
`POST /process-job` still requires `X-Worker-Secret`. Cloud Run IAM can be
enabled as an additional infrastructure-level control.

## Scope and privacy

- Only public YouTube video URLs are analyzed automatically.
- YouTube video bytes are not downloaded or stored by this worker.
- Private, unlisted, protected, Instagram, TikTok, and Facebook videos are not
  analyzed.
- Raw Gemini responses and full recipe content are not written to job-event
  metadata or logs.
- yt-dlp, Apify, RapidAPI, FFmpeg, OpenCV, raw-media storage, and thumbnail
  frame extraction remain deferred.
