# M22D Worker Environments

M22D keeps secrets and environment identity in env files, and moves non-secret worker runtime configuration into committed JSON config files.

## Source Files

- Development source: `env/development.env`
- Production source: `env/production.env`
- Committed templates: `env/development.example.env` and `env/production.example.env`
- Runtime defaults: `config/worker.defaults.json`
- Environment overrides: `config/worker.development.json` and `config/worker.production.json`

On Render, `RENDER_GIT_BRANCH` is the existing runtime selector: `develop`
loads development config and `main` loads production config. `APP_ENV` remains
supported but is not required when branch metadata is present; if both exist,
they must agree. Local execution without either selector defaults to development.

Never commit real env values. `env/development.env`, `env/production.env`, root `.env`, and local `.env.*` files are ignored.

If an existing root `.env` is present, do not print or delete it automatically. If it points to the dev Supabase project, migrate those values manually into `env/development.env`, then stop using root `.env` as the shared source of truth.

The old `recipe-dost-media-worker-queue-poc.env` file may contain useful historical dev values, but do not copy it wholesale. Review key names against [docs/m22d-worker-env-variable-map.md](m22d-worker-env-variable-map.md), copy only env-only secrets/environment values, and leave non-secret runtime settings in `config/worker.*.json`.

Do not fill `env/production.env` until the production Supabase project and production service-role key exist.

## Safety Rules

- Never reuse Supabase service-role keys across environments.
- Dev worker must never use the production Supabase service-role key.
- Production worker must never use the development Supabase service-role key.
- Production validation rejects the known dev Supabase ref `xvsiuwjidmkdqzkswoki`.
- Keep local production source files at `APP_ENV=production` and
  `LOG_LEVEL=info`; Render derives environment from `main` and reads log level
  from production JSON.
- Keep `ALLOW_RAW_URL_LOGS=false` in config.
- Keep `MAX_SOURCE_VIDEO_DURATION_SECONDS` in committed JSON config. Its
  default is 180 and its validated range is 60–900 seconds.
- Do not store raw video, resolver CDN URLs, cookies, Gemini prompts, Gemini raw responses, or secrets in env docs or logs.
- Keep non-secret runtime config out of clean env files. Legacy Render env values can remain during transition and still override config.
- Blank environment overrides are ignored and cannot erase committed config.
- Resolver mode, audience, both chains, cookie fallback, and YouTube direct
  routing remain only in `config/worker.*.json`.
- Keep `SENTRY_DEV_TEST=false` except during the one controlled development verification; production requires it to remain false. Unsetting `SENTRY_DSN` is the monitoring rollback switch.

## Validate

```bash
python scripts/validate_env.py development
python scripts/validate_env.py production
python scripts/validate_env.py development --example
python scripts/validate_env.py production --example
```

Make targets:

```bash
make env-check-dev
make env-check-prod
make env-check-examples
make resolver-config-dev
make resolver-config-prod
```

The validator prints key names with masked values only. Missing env files fail with a migration hint. It also validates merged config JSON safety rules.

Use Python 3.12, matching Docker. Do not use an unconfigured global Python 3.14:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m unittest discover -s tests -p "test_*.py"
```

The repository already ignores `.venv/`. Docker and this workflow both install
from the root `requirements.txt`.

## Export For Render

Generate a temporary Render-compatible env file:

```bash
python scripts/export_render_env.py development --output /tmp/recipe-dost-worker-development.env
python scripts/export_render_env.py production --output /tmp/recipe-dost-worker-production.env
```

Make targets:

```bash
make render-env-dev
make render-env-prod
```

The export validates first, prints key names only, and writes env-only values. `MEDIA_WORKER_SHARED_SECRET` is exported as `WORKER_API_SECRET`. Config-only values are intentionally not exported.

The output file contains secrets. Delete it after uploading to Render.

## Render Setup

Use separate Render environment groups:

- `recipe-dost-worker-dev`
- `recipe-dost-worker-prod`

Use separate Render services:

- `recipe-dost-media-worker-dev`
- `recipe-dost-media-worker-prod`

Upload flow:

1. Copy the correct template to `env/development.env` or `env/production.env`.
2. For dev, migrate only current keys from the root `.env` or old `recipe-dost-media-worker-queue-poc.env`; do not print either file.
3. Fill values from the matching Supabase/Gemini/worker environment only.
4. Leave production unfilled until prod Supabase exists.
5. Run the matching validator.
6. Export the temporary Render env file.
7. In Render, upload it to the matching environment group.
8. Attach only the dev group to the dev service and only the prod group to the prod service.
9. Restart or redeploy the worker service after uploading the env group.
10. Delete the temporary `/tmp/recipe-dost-worker-*.env` file after upload.

Do not attach both env groups to one service. Do not paste production service-role keys into the dev group or dev service-role keys into the prod group.

## Branch And Release Safety

Current branch mapping:

- `develop` deploys to the development Render worker service.
- `main` deploys to the production Render worker service.
- `recipe-dost-media-worker-dev` must track `develop`.
- `recipe-dost-media-worker-prod` must track `main`.
- `recipe-dost-worker-dev` and `recipe-dost-worker-prod` remain separate Render env groups.

Render env groups should contain secrets and environment identity only. Non-secret runtime behavior belongs in `config/worker.defaults.json`, `config/worker.development.json`, and `config/worker.production.json`.

Effective runtime values use this precedence: explicit `Settings` constructor
input, nonblank process environment, nonblank `.env`, merged worker JSON
(`worker.defaults.json` followed by the selected environment overlay), file
secrets, then model fallbacks. Environment JSON files are sparse overlays and
must not repeat shared defaults.

The W10A queue-liveness defaults are version-controlled in
`config/worker.defaults.json`: empty-queue polling is 5 seconds, stale work is
eligible after 600 seconds, the recovery limit is 2, and stale scanning runs
every 60 seconds. Development and production inherit these values. Matching
Render variables remain supported as deliberate nonblank overrides, but are
not required and should not be added merely to duplicate the JSON defaults.
Pydantic `Settings` validates the effective values, and `/health.safe_config`
reports only the four resolved safe numeric values.

Use the safe environment doctor before deploys:

```bash
make env-doctor-dev
make env-doctor-prod
make check-dev
make check-prod
```

`make env-doctor-prod` and `make check-prod` require the current branch to be `main`. To intentionally test the production check from another branch, set `ALLOW_NON_MAIN_PROD_DEPLOY=1` for that one command; do not use the override for normal production deploys.

Worker release process:

1. Land and test worker changes on `develop`.
2. Let the dev Render worker deploy from `develop`.
3. Smoke dev with `/health`, `/wake`, and at least the relevant platform job path.
4. Merge `develop` to `main`.
5. Let the prod Render worker deploy from `main`.
6. Smoke prod with `/health`, `/wake`, and YouTube/Instagram/TikTok platform checks.

## M22D.3 Production Setup Status

Status date: 2026-07-08.

- Production Supabase ref used by the worker: `cjlvkagkcfswsycsblfw`
- Render production env group: `recipe-dost-worker-prod`
- Render production service: `recipe-dost-media-worker-prod`
- Production Supabase migrations: passed
- Production SQL smoke tests: passed
- Local compile validation: passed with `python3 -m compileall app scripts`
- Render deploy/health/log smoke test: pending, not yet recorded as passed

Remaining blockers before production app release:

- Sync production Supabase secrets.
- Deploy production Edge Functions.
- Run production function smoke tests.
- Complete RevenueCat/Google Play setup.

## Render Env Group Migration

Phase A: deploy the config-capable worker while the current Render env group still contains old non-secret runtime variables. Env vars intentionally override config, so this should not break the current deployment.

Phase B: verify queue worker behavior, `/health`, `/wake`, metadata preview, and at least one controlled queue job in the target environment.

Phase C: remove deprecated non-secret config variables from the Render env group, using [docs/m22d-worker-env-variable-map.md](m22d-worker-env-variable-map.md) as the checklist. Do not remove secrets or cookie/private auth values.

Phase D: keep only server secrets and deliberate operational switches in Render:
`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `GEMINI_API_KEY`,
`WORKER_API_SECRET`, provider credentials, optional `SENTRY_DSN`/Sentry switches,
and any explicitly retained private-auth secret. `APP_ENV` and `LOG_LEVEL` are
not exported; branch metadata and JSON provide them.

Do not remove non-secret Render env variables before Phase B succeeds.

## M22D.5A Production Extractor Config Parity

Status date: 2026-07-08.

Root cause: after production env cleanup, the production worker was relying on missing or invisible runtime defaults for the Instagram/unified extractor path. Production jobs could be claimed by the queue worker and then fail with `unified_extractor_failed` when `ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR` was off, or hit the legacy/manual Instagram gate with `instagram_resolver_disabled`.

Fix: non-secret extractor behavior belongs in `config/worker.production.json`, not the Render env group. Production config now explicitly keeps `WORKER_EXECUTION_MODE=queue`, `AI_ANALYSIS_MODE=gemini_video_poc`, `ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR=true`, `MEDIA_EXTRACT_OUTPUT_MODE=temp_file`, `ENABLE_RENDER_INSTAGRAM_RESOLVER=true`, `ENABLE_YTDLP_COOKIES=true`, and `ENABLE_INSTAGRAM_COOKIES=true`.

Private Instagram cookies remain env-only. If Instagram extraction requires authenticated cookies, set `INSTAGRAM_COOKIES_B64` in the production Render env group; never commit or print the value. When cookie mode is enabled but cookies are missing, the worker should fail safely with `instagram_cookies_missing`.

Remaining next step: redeploy the production worker and rerun the production smoke test for Instagram, TikTok, and YouTube.

## M22D.5B Social Resolver Production Parity

Status date: 2026-07-08.

Production smoke status: YouTube AI recipe generation passed end to end through the production app, Supabase, Edge Functions, worker trigger, queue loop, Gemini path, and recipe-card save.

Remaining social findings: Instagram still failed with `instagram_resolver_disabled`, which means the runtime effective config or deployed revision had the Instagram resolver gate disabled. TikTok reached `tikwm_resolver_started`, then failed with a `NameError` that escaped into `background_worker_error`.

Fix: the worker now exposes a safe config summary in `/health.safe_config` and startup logs. It includes only non-secret strings, booleans, numbers, resolver names, and cookie-presence booleans. TikTok's legacy/background TikWM path now initializes resolver timing before use, and unexpected TikWM resolver exceptions produce a safe `tikwm_resolver_exception` failure event instead of leaking raw data.

M22D.5D supersedes the social smoke interpretation below: TikWM should not be the production-primary TikTok route for normal queue jobs.

## M22D.5D Queue Orchestration Compatibility

Status date: 2026-07-08.

Root cause: production was still triggering `/process-job` for normal queued media jobs. In queue mode, that endpoint previously scheduled the legacy direct `JobRunner.run()` path, where TikTok emits `tikwm_resolver_started` and Instagram can fail at the SnapInsta `instagram_resolver_disabled` gate.

Fix: in `WORKER_EXECUTION_MODE=queue`, `/process-job` now validates the worker secret and media job, records `process_job_queue_wake_requested`, starts the queue loop, returns `202`, and does not schedule the legacy background runner. The queue loop remains responsible for claiming `queued` jobs and processing TikTok/Instagram through unified yt-dlp temp-file extraction plus Gemini Files API.

TikWM status: legacy/manual/debug only for production AI jobs. The M22D.5B TikWM timing fix is kept so the legacy path fails safely if someone deliberately runs non-queue/manual processing, but `config/worker.production.json` no longer carries explicit TikWM production overrides.

Next smoke test order after redeploy: verify `/health.safe_config` shows `worker_execution_mode=queue`, `process_job_mode=queue_wake_compatibility`, `raw_url_logging_enabled=false`, `max_source_video_duration_seconds=180`, and unified extraction enabled. Then run YouTube, Instagram, and TikTok production jobs. For normal TikTok/Instagram queue jobs, expected events include `worker_trigger_success`, `process_job_queue_wake_requested`, `queue_job_claimed`, and `unified_extractor_started`; unexpected events are `tikwm_resolver_started` and `instagram_resolver_disabled`.

## M22D.5E Production Config Packaging And Loading

Status date: 2026-07-08.

Root cause: production `/health` showed the M22D.5D code was deployed, but runtime config values still matched code fallbacks: `AI_ANALYSIS_MODE=gemini`, `ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR=false`, `ENABLE_INSTAGRAM_COOKIES=false`, `ENABLE_YTDLP_COOKIES=false`, and `MAX_VIDEO_DURATION_SECONDS=90`. The Render env group was clean and only contained env-only secrets/environment keys, so the env group was not overriding JSON. The Docker image copied `app/` but not `config/`, so `/service/config/worker.production.json` was missing at runtime.

Fix: the Dockerfile now copies `config/` into the runtime image. `app/runtime_config.py` resolves config from the project root or `WORKER_CONFIG_DIR`, loads `worker.defaults.json` then `worker.<APP_ENV>.json`, and fails production startup if `worker.production.json` is missing or empty. `/health.safe_config.config_source` reports loaded/missing filenames and booleans only; it does not expose secrets.

Expected production `/health.safe_config.config_source` after redeploy:

```json
{
  "app_env": "production",
  "config_dir_exists": true,
  "loaded_files": ["worker.defaults.json", "worker.production.json"],
  "missing_files": [],
  "defaults_config_loaded": true,
  "production_config_loaded": true
}
```

Expected production config values after redeploy: `ai_analysis_mode=gemini_video_poc`, `enable_unified_ytdlp_stream_extractor=true`, `enable_instagram_cookies=true`, `enable_ytdlp_cookies=true`, `media_extract_output_mode=temp_file`, `max_video_duration_seconds=120`, and `raw_url_logging_enabled=false`.

## M22D.5F Production Worker Smoke Completion

Status date: 2026-07-08.

Production worker smoke testing is complete.

- Production Render service: `recipe-dost-media-worker-prod`
- Production Render env group: `recipe-dost-worker-prod`
- Final architecture: the same main branch is deployed to separate Render services with separate environment groups for dev/prod.
- Config rule: Render env contains secrets and environment identity only; `config/worker.production.json` contains non-secret production runtime config.

Post-deploy `/health` confirmed:

- `safe_config.config_source.production_config_loaded=true`
- `safe_config.config_source.loaded_files` includes `worker.defaults.json` and `worker.production.json`
- `worker_execution_mode=queue`
- `safe_config.process_job_mode=queue_wake_compatibility`
- `enable_unified_ytdlp_stream_extractor=true`
- `enable_instagram_cookies=true`
- `enable_ytdlp_cookies=true`
- `max_video_duration_seconds=120`
- `raw_url_logging_enabled=false`

Production end-to-end smoke passed:

- YouTube AI recipe generation works.
- Instagram AI recipe generation works.
- TikTok AI recipe generation works.

Next milestone: app repo M22C.3 RevenueCat + Google Play billing setup.

## M22E.0B Worker Branch And Environment Safety

Status date: 2026-07-09.

Added `scripts/check_environment.py` as a safe release preflight. It prints only branch, requested environment, expected branch, config filenames, expected `APP_ENV`, the Supabase URL host when an env file exists, and selected non-secret runtime config checks. It must not print service-role keys, worker secrets, Gemini keys, cookies, raw URLs, stream URLs, prompts, payloads, or full env values.

Branch guard:

- Production deploy checks require `main` unless `ALLOW_NON_MAIN_PROD_DEPLOY=1` is set.
- Development checks prefer `develop` and warn when run elsewhere.

Make targets:

```bash
make env-doctor-dev
make env-doctor-prod
make check-dev
make check-prod
```
