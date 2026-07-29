# Recipe Dost Media Worker

FastAPI worker for Recipe Dost media jobs. The production cutover path is the serialized queue worker on Render/main.

## Scope

- Queue mode is the only active worker architecture. Render starts
  `app.main:app`; Docker's historical
  `python -m app.workers.combined_worker` name is a thin façade for the same
  sequential durable queue loop.
- `/process-job` remains available only for legacy/manual/debug job kicks.
- YouTube uses Gemini native URL input when `YOUTUBE_AI_INPUT_MODE=gemini_url`; it does not use yt-dlp in that mode.
- TikTok and Instagram can use the provider-neutral external resolver pipeline;
  production remains external-disabled until canary approval. Legacy unified
  extraction is rollback-only and platform cookies require an independent flag.
- Gemini 3.1 Flash-Lite single-pass structured extraction is the default recipe path.
- Raw video is written only to isolated, bounded temporary files for active AI
  processing; it is never retained, uploaded to Supabase/Drive, exposed as a
  download, or logged.
- Secrets, cookies, source URLs, stream URLs, Gemini prompts/raw responses, ingredients, and steps must not appear in logs or job events.

## Local Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp env/development.example.env env/development.env
```

Set real development-only server values in `env/development.env`:
`MEDIA_WORKER_SHARED_SECRET`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
`GEMINI_API_KEY` when Gemini is needed, and the two provider credentials when
external resolution is enabled. See
[docs/m22d-worker-environments.md](docs/m22d-worker-environments.md) for dev/prod
env source-of-truth, validation, and Render export steps.

## Key Environment

```env
APP_ENV=development
LOG_LEVEL=debug
SUPABASE_URL=https://your-dev-project-ref.supabase.co
SUPABASE_SERVICE_ROLE_KEY=replace-with-development-service-role-key
MEDIA_WORKER_SHARED_SECRET=replace-with-development-worker-shared-secret
GEMINI_API_KEY=replace-with-development-gemini-api-key
INSTAGRAM_RAPIDAPI_KEY=replace-with-development-instagram-rapidapi-key
TIKTOK_AKNG_ACCESS_TOKEN=replace-with-rotated-development-token
```

Non-secret runtime settings live in `config/worker.defaults.json` with environment overrides in `config/worker.development.json` and `config/worker.production.json`. Existing Render env vars still override config during the migration window.

Render can select the file from its existing branch metadata (`develop` ->
development, `main` -> production), so `APP_ENV` is optional there. If retained,
it must agree with the branch. Blank environment values are ignored rather than
erasing file values.

Render export maps `MEDIA_WORKER_SHARED_SECRET` to runtime `WORKER_API_SECRET`. See [docs/m22d-worker-env-variable-map.md](docs/m22d-worker-env-variable-map.md) before removing old Render env keys.

## Privacy-Safe Worker Sentry

Sentry covers only the active production queue flow: `app.main:app` startup/`GET /wake` -> `run_queue_loop` -> `process_queue_job` -> active YouTube oEmbed or unified social extraction -> Gemini structured extraction -> recipe/job persistence. The legacy direct `JobRunner` branch behind non-queue `/process-job`, `/metadata-preview`, and debug resolver routes are excluded. Default and auto-enabled Sentry integrations are disabled, so FastAPI and Python logging cannot report those routes implicitly.

Production reporting requires `APP_ENV=production`, a valid `SENTRY_DSN`, and,
on Render, `RENDER_GIT_BRANCH=main`. Ordinary development is disabled. A bounded
external-resolver canary may set `SENTRY_DEV_CANARY=true` only on the development
Render service and `develop`; unset it after verification. The one controlled
diagnostic event still requires `SENTRY_DEV_TEST=true`:

```bash
python -m app.observability.sentry_test
```

Render supplies `RENDER`, `RENDER_GIT_BRANCH`, and `RENDER_GIT_COMMIT`; the commit becomes safe release metadata. Events contain only allowlisted current-flow tags, exception class/stack locations without source context or locals, and bounded retry/status booleans. Requests, headers, bodies, URLs, users, arbitrary extras, breadcrumbs, tokens, prompts/responses, and recipe content are dropped. Unset `SENTRY_DSN` to roll monitoring back without affecting worker processing.

## Daily Branch Workflow

Branch mapping:

- `develop` -> `recipe-dost-media-worker-dev`
- `main` -> `recipe-dost-media-worker-prod`
- Dev env group: `recipe-dost-worker-dev`
- Prod env group: `recipe-dost-worker-prod`

Daily development:

```bash
cd /Users/sadaqatali/Documents/AiAppBuilder/recipe-dost/recipe-dost-media-worker
git checkout develop
git pull origin develop
make check-dev
git push origin develop
```

After Render deploys the dev service from `develop`, smoke `/health`, `/wake`, and the platform path affected by the change.

Production release:

```bash
cd /Users/sadaqatali/Documents/AiAppBuilder/recipe-dost/recipe-dost-media-worker
git checkout main
git pull origin main
git merge --ff-only develop
make check-prod
git push origin main
```

After Render deploys the prod service from `main`, smoke `/health`, `/wake`, then YouTube, Instagram, and TikTok jobs. `make check-prod` refuses to pass from a non-`main` branch unless `ALLOW_NON_MAIN_PROD_DEPLOY=1` is set for an intentional one-off check.

Useful focused checks:

```bash
make env-doctor-dev
make env-doctor-prod
python3 scripts/check_runtime_config.py development
python3 scripts/check_runtime_config.py production
```

The environment doctor prints only safe release context: branch, requested environment, expected branch, config filenames, expected `APP_ENV`, Supabase host, and selected non-secret runtime config checks. It must not print service-role keys, worker secrets, Gemini keys, cookies, raw URLs, stream URLs, prompts, or payloads.

For production, Gemini 3.1 single-pass structured extraction is the default in code. The old two-step brief-to-JSON path remains available only by setting `GEMINI_EXTRACTION_MODE=two_step_brief_then_json`. The local deep parser is disabled by default with `LOCAL_BRIEF_PARSER_ENABLED=false`.

Suspend the old webhook Render service only after the main-branch service validates TikTok, Instagram, and YouTube end to end.

Optional platform cookies:

- TikTok: `/app/cookies/tiktok.txt`
- Instagram: `/app/cookies/instagram.txt`
- Facebook: `/app/cookies/facebook.txt`

Cookies are platform-specific. Do not use Facebook cookies for TikTok. `COOKIE_FILE_PATH=/app/cookies.txt` is legacy and only used if `ENABLE_GENERIC_COOKIE_FALLBACK=true`.

## Run

```bash
PORT=8080 python -m app.workers.combined_worker
curl http://localhost:8080/health
```

Sequential durable queue execution is the only supported runtime.
`combined_worker` is only the Docker uvicorn façade for `app.main:app`. The W7
staged implementation was reverted and removed from active source; its design
remains in Git history. Any future concurrency design requires durable leases,
heartbeats, fencing, and idempotent stage transitions.

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

Metadata preview for a saved recipe link before AI generation:

```bash
curl -X POST http://localhost:8080/metadata-preview \
  -H "Content-Type: application/json" \
  -H "X-Worker-Secret: replace-with-your-worker-secret" \
  -d '{"url":"https://www.youtube.com/watch?v=00000000000","platform":"youtube"}'
```

`/metadata-preview` also accepts `Authorization: Bearer <WORKER_API_SECRET>`. It returns safe metadata only:

```json
{
  "platform": "youtube",
  "resolver": "youtube_oembed",
  "title": "Recipe title",
  "description": "Recipe title",
  "thumbnail_url": "https://...",
  "author_username": "creator",
  "duration_seconds": null,
  "tags": [],
  "metadata_status": "completed",
  "is_too_long": false,
  "max_duration_seconds": 90
}
```

This endpoint does not call Gemini, does not create media jobs, does not download videos, and must not return temporary video/CDN URLs or secret data. YouTube uses oEmbed first; TikTok, Instagram, and Facebook use yt-dlp metadata-only extraction when available, then safe OpenGraph fallback. Other URLs use OpenGraph only.

## Production Queue

`WORKER_EXECUTION_MODE=queue` is the primary production mode. FastAPI starts one in-process queue loop on startup; `GET /wake` safely starts it again if Render Free has idled the service. The queue loop claims the oldest `queued` job and must finish each job as `completed` or `failed`.

- YouTube uses oEmbed metadata plus Gemini native URL input when `YOUTUBE_AI_INPUT_MODE=gemini_url`.
- With external routing enabled, TikTok and Instagram use registered provider
  adapters, guarded download/probe/optional A/V mux, then the unchanged Gemini
  Files API path. Development adapter IDs are `akng_tiktok` and
  `rapidapi_instagram_unified_v1`.
- Social extraction chooses combined audio/video formats through a bounded
  720p -> 540p -> 480p -> 360p ladder, targets 36 MiB, and enforces a 50 MiB
  streaming/temp-file hard limit. The historical `*_MB` config names are binary
  MiB.
- Gemini 3.1 Flash-Lite single-pass structured extraction is the documented default.
- Temporary stream URLs are never stored or returned.
- Raw video is not stored. Gemini prompts, raw responses, source URLs, stream URLs, recipe ingredients, and recipe steps must not be logged or written to events.
- Detailed worker stages are tracked with `media_job_events` and `progress`; `media_jobs.status` stays one of `queued`, `resolving`, `downloading`, `analyzing`, `completed`, or `failed`.
- `/health` reports `metadata_preview_enabled`, `max_video_duration_seconds`, and `supported_metadata_platforms` for app/backend verification.

See [docs/worker-context.md](docs/worker-context.md) for the compact current context.
See [docs/render-deploy.md](docs/render-deploy.md) for Render setup notes.
See [docs/m22d-worker-environments.md](docs/m22d-worker-environments.md) for worker environment source-of-truth setup.
See [docs/m22d-worker-env-variable-map.md](docs/m22d-worker-env-variable-map.md) for env/config ownership.
See [docs/m23-adaptive-media.md](docs/m23-adaptive-media.md) for the audited media policy, failure/Sentry contract, and staged deployment/rollback checklist.
See [docs/m23-w3-external-media-routing-plan.md](docs/m23-w3-external-media-routing-plan.md) for external resolver flags, security, fixtures, canary, and rollback.

## Validation

```bash
python3.12 -m compileall app scripts
python3.12 scripts/check_runtime_config.py development
INSTAGRAM_RAPIDAPI_KEY=dummy-development-key \
TIKTOK_AKNG_ACCESS_TOKEN=dummy-development-token \
python3.12 scripts/check_external_resolver_config.py development
python3.12 scripts/check_environment.py development
```

Owner-only Instagram media gate (never run from Codex or production):

```bash
APP_ENV=development \
ALLOW_LIVE_INSTAGRAM_DIAGNOSTIC=true \
python scripts/check_instagram_media_candidate.py
```

The command requires `INSTAGRAM_RAPIDAPI_KEY` in the environment, reads the
Instagram URL through a hidden prompt, uses the worker adapter/selection/
download/ffprobe/mux path, and prints counts/categories only. It makes no
Supabase, Gemini, or Sentry call and exits zero only when final H.264 MP4 video
with audio is ready for Gemini.

Production preflight from `main`:

```bash
python3.12 scripts/check_runtime_config.py production
python3.12 scripts/check_external_resolver_config.py production
python3.12 scripts/check_environment.py production
```

Use `make resolver-config-docker` for the Render-like image check. It passes no
resolver mode, audience, chain, cookie-fallback, or YouTube-direct environment
overrides.

## Deploy Note

Cloud Run can be unauthenticated at the infrastructure layer if needed, but worker endpoints that mutate or debug protected data must require `X-Worker-Secret`.

## What is the process for future deployments?
A. If you add a new worker config value

Example: queue interval, model name, thumbnail bucket, feature flag.

Do not add it to Render env unless it is secret.

Process:

Step	Action
1	Add value to config/worker.defaults.json or env-specific JSON
2	Update docs/env map if needed
3	Run worker validation
4	Commit and push
5	Render redeploys worker
6	No env group update needed

Commands:
```bash
cd /Users/sadaqatali/Documents/AiAppBuilder/recipe-dost/recipe-dost-media-worker

python3 -m compileall app scripts
make check-dev
git add .
git commit -m "chore: update worker runtime config"
git push origin develop
```
# If you add a new worker secret/API key

Example: new API key, cookies, private token.

Process:

Step	Action
1	Add key to env/development.env locally
2	Add placeholder to env/development.example.env
3	Update scripts/validate_env.py if required
4	Export Render env file
5	Upload to Render env group
6	Restart/redeploy Render worker
7	Commit only examples/scripts/docs, not real env

Commands:
```bash
cd /Users/sadaqatali/Documents/AiAppBuilder/recipe-dost/recipe-dost-media-worker

python3 scripts/validate_env.py development
python3 scripts/export_render_env.py development --output /tmp/recipe-dost-worker-development.env
```

Upload /tmp/recipe-dost-worker-development.env to Render env group, then:
```bash
rm /tmp/recipe-dost-worker-development.env
```

# If you add a new app public env value

Example: RevenueCat key, Supabase URL, future PostHog key, Sentry DSN.

Process:

Step	Action
1	Add to env/development.env
2	Add placeholder to example env
3	Update validation and EAS sync allowlist
4	Run dry-run
5	Apply EAS env sync
6	Rebuild app if required

Commands:
```bash
cd /Users/sadaqatali/Documents/AiAppBuilder/recipe-dost/recipe-dost

node scripts/env/validate-env.mjs development
npm run env:eas:dev:dry
node scripts/env/sync-eas-env.mjs development --apply
eas build --platform android --profile development
```

# If you add a new Supabase Edge Function secret

Example: REVENUECAT_API_KEY, GEMINI_API_KEY, MEDIA_WORKER_URL.

Process:

Step	Action
1	Add to env/development.env
2	Add placeholder to example env
3	Update Supabase sync allowlist if needed
4	Dry-run
5	Apply Supabase secret sync
6	Test function
7	Deploy function only if code changed

Commands:
```bash
cd /Users/sadaqatali/Documents/AiAppBuilder/recipe-dost/recipe-dost

npm run env:supabase:dev:dry
node scripts/env/sync-supabase-secrets.mjs development --apply
```

## Deterministic worker QA

Offline QA calls production normalization, selection, request construction,
parsing, validation, recovery, terminal-state, cleanup, and quota-finalization
functions without network calls or secrets:

```bash
python scripts/worker_qa.py replay --case primary_malformed_repaired
python scripts/worker_qa.py batch \
  --manifest tests/fixtures/worker_qa/mixed_batch_manifest.json
```

The sanitized report is `.worker-qa/latest/summary.json` and is gitignored.
Owner-only live development mode uses an in-memory repository, so it performs no
Supabase writes or entitlement charge:

```bash
APP_ENV=development ALLOW_LIVE_WORKER_QA=true \
python scripts/worker_qa.py live \
  --manifest .worker-qa/private/live-manifest.json \
  --max-total-calls 8
```
