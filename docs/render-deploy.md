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

# M23.W3C external resolver canary

Non-secret resolver mode, audience, adapter chains, provider hosts, CDN suffix
policies, size limits, and timeouts belong in `config/worker.<environment>.json`,
not Render environment variables. Render holds only credentials:

```text
INSTAGRAM_RAPIDAPI_KEY
TIKTOK_AKNG_ACCESS_TOKEN
```

Development config currently selects `external`, `all_authorized_ai`,
`rapidapi_instagram_reels13`, and `akng_tiktok`, with cookie fallback false and
YouTube direct Gemini true. Rotate the token that appeared in user-provided
input before installing it. Use separate development and production provider
applications/keys.

After an offline green test run, deploy only `develop`, confirm safe health
configuration/credential booleans, and process one authorized test-user job per
social platform. Provider URLs, bodies, credentials, source URLs, titles, user
IDs, and recipe content must be absent from Render/Sentry/events. Optional
development Sentry requires `SENTRY_DEV_CANARY=true` plus the development DSN;
turn it off after the bounded test.

The first production canary uses production config `external` plus
`test_users`, the verified provider chains/host policies, cookie fallback false,
and direct YouTube true. Production secrets are separate. Roll back by setting
the audience to `disabled`; no mobile release is required. Cookie rollback also
requires legacy/hybrid mode, `COOKIE_MEDIA_FALLBACK_ENABLED=true`, and the
existing cookie configuration.

## M23.W3C-R configuration recovery

The development service must load `/service/config/worker.defaults.json` and
`/service/config/worker.development.json`. Render's automatic branch metadata
selects `develop` -> development and `main` -> production; `APP_ENV` may remain
only when it matches that branch. A conflict or missing/unknown Render branch
fails closed.

Do not add these values to the Render environment group:

```text
MEDIA_RESOLVER_MODE
EXTERNAL_MEDIA_RESOLVER_AUDIENCE
INSTAGRAM_RESOLVER_CHAIN
TIKTOK_RESOLVER_CHAIN
COOKIE_MEDIA_FALLBACK_ENABLED
YOUTUBE_DIRECT_GEMINI_ENABLED
```

Before redeploy, run `make resolver-config-docker` on a machine with Docker.
The development image check supplies only dummy provider credentials and branch
metadata; the production check supplies no provider credentials. Expected safe
development startup event is `worker_safe_config_summary` with config path
`config/worker.development.json`, selection source `RENDER_GIT_BRANCH`, mode
`external`, audience `all_authorized_ai`, adapters
`rapidapi_instagram_reels13`/`akng_tiktok`, cookie fallback false, YouTube direct
true, and both credential-presence booleans true.

Owner recovery checklist:

1. Confirm the service tracks `develop`.
2. Keep the configured values of `INSTAGRAM_RAPIDAPI_KEY` and
   `TIKTOK_AKNG_ACCESS_TOKEN` hidden and unchanged.
3. Do not add the six resolver controls above to Render.
4. Redeploy the latest reviewed `develop` commit manually after the Docker check
   passes.
5. Confirm the one safe startup summary before running any media job.

Rollback is config-only: disable the development external audience in
`worker.development.json` and redeploy. Do not enable cookie fallback.
