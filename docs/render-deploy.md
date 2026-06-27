# Render Deploy Notes

W-M14R1 reuses the existing `recipe-dost-media-worker` repo as a Render Free Web Service POC. It does not create a second worker repo and does not remove Cloud Run configs.

## Runtime

Render uses `render.yaml`:

```yaml
services:
  - type: web
    name: recipe-dost-media-worker
    runtime: python
    plan: free
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT --timeout-keep-alive 120
    healthCheckPath: /health
```

`PORT` is supplied by Render through the start command.

## Process Job Flow

- Supabase create-media-job Edge Function calls `POST /process-job` with `X-Worker-Secret`.
- The route validates the body and current `media_jobs` row.
- Completed jobs return `200` with `reason=already_completed`.
- Already-processing jobs return `202` with `accepted=false`.
- Pending jobs, and failed jobs with `force=true`, schedule FastAPI `BackgroundTasks` and return `202` immediately.
- The background task updates Supabase directly and must finish with `media_jobs.status` as `completed` or `failed`.

FastAPI BackgroundTasks are acceptable for this Render Free POC. They are not a durable production queue; add leases, retries, and a durable queue before relying on this for production workloads.

## Recommended Render Env

```env
RUNTIME_PLATFORM=render
APP_ENV=development
WORKER_MODE=social_video
MEDIA_PIPELINE_MODE=social_stream

ENABLE_SOCIAL_VIDEO=true
ENABLE_TIKWM_RESOLVER=true
ENABLE_RENDER_INSTAGRAM_RESOLVER=false
ENABLE_RENDER_FACEBOOK_RESOLVER=false
ENABLE_COBALT_RESOLVER=false

SOCIAL_ANALYSIS_MODE=text_first
ENABLE_TIKTOK_TEXT_FALLBACK=true
ENABLE_SOCIAL_VIDEO_GEMINI=false
SOCIAL_TEXT_GEMINI_TIMEOUT_SECONDS=45
MAX_VIDEO_DURATION_SECONDS=90

GEMINI_MAX_CALLS_PER_JOB=2
ENABLE_GEMINI_REPAIR_CALL=false
ENABLE_GEMINI_FALLBACKS=false
STORE_RAW_MEDIA=false
```

Set `WORKER_API_SECRET`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `GEMINI_API_KEY` as Render secrets.

## Safety Rules

- Do not store raw videos.
- Do not store TikWM, Cobalt, Snapinsta, Snapsave, or CDN download URLs.
- Do not expose user-facing downloads.
- Do not log full source URLs, stream URLs, cookies, Gemini prompts, raw Gemini responses, ingredients, or steps.
- TikTok uses TikWM metadata plus Gemini text-first analysis only in W-M14R1.
- Instagram/Facebook remain disabled until their Render resolver paths are separately validated.
