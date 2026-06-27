# Recipe Dost Media Worker

FastAPI worker for Recipe Dost media jobs. The same repo supports the original Cloud Run deployment and the W-M14R1 Render Free Web Service POC.

## Scope

- YouTube path is working: public URL -> Gemini observation -> recipe normalization -> `recipe_cards`.
- Render POC path is under active testing: `/process-job` returns quickly and runs jobs with FastAPI BackgroundTasks.
- TikTok uses TikWM metadata plus Gemini text-first caption/title analysis.
- Instagram/Facebook are disabled by default on Render until their resolver paths are validated.
- Raw video is never written to disk, uploaded to Supabase/Drive, exposed as a download, or logged.
- Secrets, cookies, source URLs, stream URLs, Gemini prompts/raw responses, ingredients, and steps must not appear in logs or job events.

## Local Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set real server-only values in `.env`: `WORKER_API_SECRET`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `GEMINI_API_KEY` when Gemini is needed.

## Key Environment

```env
APP_ENV=development
RUNTIME_PLATFORM=cloud_run_compatible
WORKER_MODE=skeleton
MEDIA_PIPELINE_MODE=youtube_direct

GEMINI_VIDEO_MODEL=gemini-2.5-flash
GEMINI_TEXT_MODEL=gemini-2.5-flash
GEMINI_MAX_RETRIES=1
GEMINI_MAX_CALLS_PER_JOB=2
GEMINI_DAILY_DEV_CALL_LIMIT=5
GEMINI_VIDEO_OBSERVATION_TIMEOUT_SECONDS=120
GEMINI_TEXT_NORMALIZATION_TIMEOUT_SECONDS=60
GEMINI_FILES_UPLOAD_TIMEOUT_SECONDS=90
GEMINI_FILES_PROCESSING_TIMEOUT_SECONDS=120
GEMINI_SOCIAL_UPLOAD_MODE=files_api
MAX_JOB_RUNTIME_SECONDS=240
SOCIAL_ANALYSIS_MODE=text_first
ENABLE_TIKTOK_TEXT_FALLBACK=true
ENABLE_SOCIAL_VIDEO_GEMINI=false
SOCIAL_VIDEO_GEMINI_TIMEOUT_SECONDS=90
SOCIAL_TEXT_GEMINI_TIMEOUT_SECONDS=45
ENABLE_GEMINI_REPAIR_CALL=false
ENABLE_GEMINI_FALLBACKS=false

ENABLE_SOCIAL_VIDEO=false
ENABLE_YT_DLP=false
MAX_VIDEO_DURATION_SECONDS=90
MAX_MEMORY_BUFFER_MB=35
MAX_VIDEO_HEIGHT=720
STORE_RAW_MEDIA=false

ENABLE_TIKWM_RESOLVER=true
TIKWM_API_URL=https://www.tikwm.com/api/
TIKWM_TIMEOUT_SECONDS=15
TIKWM_HD=true
TIKWM_ENABLED_PLATFORMS=tiktok
TIKWM_MAX_VIDEO_SIZE_MB=30
TIKWM_PROBE_BYTES=262144

ENABLE_COBALT_RESOLVER=false
ENABLE_RENDER_INSTAGRAM_RESOLVER=false
ENABLE_RENDER_FACEBOOK_RESOLVER=false
COBALT_API_URL=
COBALT_TIMEOUT_SECONDS=45
COBALT_ALWAYS_PROXY=true
COBALT_VIDEO_QUALITY=720
COBALT_ENABLED_PLATFORMS=instagram,facebook
```

Use `WORKER_MODE=youtube_direct` for the stable YouTube pipeline.

Use this for Render TikTok text-first testing:

```env
RUNTIME_PLATFORM=render
WORKER_MODE=social_video
MEDIA_PIPELINE_MODE=social_stream
ENABLE_SOCIAL_VIDEO=true
ENABLE_TIKWM_RESOLVER=true
ENABLE_COBALT_RESOLVER=false
ENABLE_RENDER_INSTAGRAM_RESOLVER=false
ENABLE_RENDER_FACEBOOK_RESOLVER=false
```

For social testing, `SOCIAL_ANALYSIS_MODE=text_first` and `ENABLE_SOCIAL_VIDEO_GEMINI=false` are the development defaults. TikTok/TikWM jobs with a usable title/caption use one Gemini text call and skip stream download, video upload, and video observation. Instagram/Facebook return controlled disabled failures until `ENABLE_RENDER_INSTAGRAM_RESOLVER=true` or `ENABLE_RENDER_FACEBOOK_RESOLVER=true` after validation.

Optional platform cookies:

- TikTok: `/app/cookies/tiktok.txt`
- Instagram: `/app/cookies/instagram.txt`
- Facebook: `/app/cookies/facebook.txt`

Cookies are platform-specific. Do not use Facebook cookies for TikTok. `COOKIE_FILE_PATH=/app/cookies.txt` is legacy and only used if `ENABLE_GENERIC_COOKIE_FALLBACK=true`.

## Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
curl http://localhost:8080/health
```

Process a job:

```bash
curl -X POST http://localhost:8080/process-job \
  -H "Content-Type: application/json" \
  -H "X-Worker-Secret: replace-with-your-worker-secret" \
  -d '{"media_job_id":"00000000-0000-0000-0000-000000000000"}'
```

On W-M14R1A, queued jobs return `202` immediately:

```json
{
  "ok": true,
  "accepted": true,
  "job_id": "00000000-0000-0000-0000-000000000000",
  "message": "Job accepted for background processing."
}
```

Completed jobs return `200` with `accepted=false` and `reason=already_completed`. Already-processing jobs return `202` with `accepted=false` and `reason=already_processing`.

Debug social resolver without exposing URLs/cookies/stream URLs:

```bash
curl -X POST http://localhost:8080/debug/social-resolve \
  -H "Content-Type: application/json" \
  -H "X-Worker-Secret: replace-with-your-worker-secret" \
  -d '{"url":"https://www.tiktok.com/@example/video/0000000000000000000","resolver":"auto","test_stream":true}'
```

Debug TikWM from Cloud Run/local without calling Gemini, processing jobs, storing video, or returning the TikWM raw video URL:

```bash
curl -X POST http://localhost:8080/debug/tikwm \
  -H "Content-Type: application/json" \
  -H "X-Worker-Secret: replace-with-your-worker-secret" \
  -d '{"url":"https://www.tiktok.com/@example/video/0000000000000000000","test_stream":true}'
```

## Render POC

W-M14R1 uses FastAPI BackgroundTasks on Render Free. This is a POC, not a durable production queue. The app continues polling `media_jobs` and `media_job_events`; the worker must finish every background task as `completed` or `failed`.

- TikWM is third-party, not an official TikTok API.
- YouTube uses the existing native path.
- TikTok uses TikWM and Gemini text-first analysis.
- Instagram and Facebook are disabled until validated.
- Cobalt is not used for TikTok or YouTube.
- TikWM/Cobalt temporary stream URLs are never stored or returned.
- Raw video is not stored. Gemini prompts, raw responses, source URLs, stream URLs, recipe ingredients, and recipe steps must not be logged or written to events.
- Detailed worker stages are tracked with `media_job_events` and `progress`; `media_jobs.status` stays one of `queued`, `resolving`, `downloading`, `analyzing`, `completed`, or `failed`.

See [docs/worker-context.md](docs/worker-context.md) for the compact current context.
See [docs/render-deploy.md](docs/render-deploy.md) for Render setup notes.

## Validation

```bash
python -m compileall app
```

If local `python` is unavailable:

```bash
python3 -m compileall app
```

## Deploy Note

Cloud Run can be unauthenticated at the infrastructure layer if needed, but worker endpoints that mutate or debug protected data must require `X-Worker-Secret`.
