# Recipe Dost Media Worker

FastAPI worker for Recipe Dost media jobs. The production cutover path is the serialized queue worker on Render/main.

## Scope

- Queue mode is the primary production flow: startup and `GET /wake` keep one serialized queue loop active.
- `/process-job` remains available only for legacy/manual/debug job kicks.
- YouTube uses Gemini native URL input when `YOUTUBE_AI_INPUT_MODE=gemini_url`; it does not use yt-dlp in that mode.
- TikTok and Instagram use the unified extractor to create a verified temp file, then Gemini Files API analysis.
- Gemini 3.1 Flash-Lite single-pass structured extraction is the default recipe path.
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
RUNTIME_PLATFORM=render
WORKER_MODE=social_video
MEDIA_PIPELINE_MODE=social_stream
WORKER_EXECUTION_MODE=queue
ENABLE_QUEUE_WAKE_ENDPOINT=true

AI_ANALYSIS_MODE=gemini_video_poc
GEMINI_EXTRACTION_MODE=single_pass_structured
GEMINI_PRIMARY_MODEL=gemini-3.1-flash-lite
GEMINI_FALLBACK_MODEL=gemini-2.5-flash
GEMINI_THINKING_LEVEL=high
LOCAL_BRIEF_PARSER_ENABLED=false
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

YOUTUBE_AI_INPUT_MODE=gemini_url
ENABLE_SOCIAL_VIDEO=true
ENABLE_YT_DLP=true
ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR=true
MEDIA_EXTRACT_OUTPUT_MODE=temp_file
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

Production queue cutover should run from `main` after `feature/queue-ytdlp-poc` is merged:

```env
RUNTIME_PLATFORM=render
WORKER_MODE=social_video
MEDIA_PIPELINE_MODE=social_stream
WORKER_EXECUTION_MODE=queue
AI_ANALYSIS_MODE=gemini_video_poc
GEMINI_EXTRACTION_MODE=single_pass_structured
GEMINI_PRIMARY_MODEL=gemini-3.1-flash-lite
YOUTUBE_AI_INPUT_MODE=gemini_url
ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR=true
MEDIA_EXTRACT_OUTPUT_MODE=temp_file
ENABLE_SOCIAL_VIDEO=true
ENABLE_RENDER_INSTAGRAM_RESOLVER=true
ENABLE_RENDER_FACEBOOK_RESOLVER=false
```

For production, Gemini 3.1 single-pass structured extraction is the default. The old two-step brief-to-JSON path remains available only by setting `GEMINI_EXTRACTION_MODE=two_step_brief_then_json`. The local deep parser is disabled by default with `LOCAL_BRIEF_PARSER_ENABLED=false`.

Suspend the old webhook Render service only after the main-branch service validates TikTok, Instagram, and YouTube end to end.

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

Wake the queue worker:

```bash
curl http://localhost:8080/wake
```

Legacy/manual/debug process-job kick:

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

## Production Queue

`WORKER_EXECUTION_MODE=queue` is the primary production mode. FastAPI starts one in-process queue loop on startup; `GET /wake` safely starts it again if Render Free has idled the service. The queue loop claims the oldest `queued` job and must finish each job as `completed` or `failed`.

- YouTube uses oEmbed metadata plus Gemini native URL input when `YOUTUBE_AI_INPUT_MODE=gemini_url`.
- TikTok and Instagram use verified temp-file extraction plus Gemini Files API.
- Gemini 3.1 Flash-Lite single-pass structured extraction is the documented default.
- Cobalt is not used for TikTok or YouTube.
- Temporary stream URLs are never stored or returned.
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
