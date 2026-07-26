# M22D Worker Env Variable Map

This map is based on the M22D.0D audit of code references, local env-file key names, templates, and scripts. Real env values are intentionally omitted.

## Env-Only Variables

These remain in Render env groups and local `env/*.env` files.

| Variable name | Source | Used by file | Required? | Secret? | Current worker path? | Status | Notes |
|---|---|---|---:|---:|---:|---|---|
| `APP_ENV` | env | `app/config.py`, `app/runtime_config.py`, `scripts/validate_env.py` | optional on Render | no | yes | keep if already present | Selects `config/worker.<env>.json`; when absent, Render branch metadata selects `develop` -> development and `main` -> production. If present, it must agree with the branch. |
| `LOG_LEVEL` | env/config | `scripts/validate_env.py`, `config/worker.*.json` | yes | no | deploy convention | keep | Kept in env as environment identity; mirrored in config docs only. |
| `SUPABASE_URL` | env | `app/config.py`, `app/supabase_client.py` | yes | no | yes | keep | Environment-specific Supabase URL. |
| `SUPABASE_SERVICE_ROLE_KEY` | env | `app/config.py`, `app/supabase_client.py` | yes | yes | yes | keep | Server-only key; never reuse across dev/prod. |
| `GEMINI_API_KEY` | env | `app/config.py`, `app/main.py`, `app/job_runner.py`, `app/queue_worker.py` | yes | yes | yes | keep | Required for real Gemini calls. |
| `MEDIA_WORKER_SHARED_SECRET` | env | `app/config.py`, `scripts/export_render_env.py` | yes | yes | yes | legacy alias / source | Exported as runtime `WORKER_API_SECRET`; still supported by `Settings`. |
| `WORKER_API_SECRET` | env | `app/config.py`, `app/main.py` | runtime yes | yes | yes | keep | Runtime auth secret. Export derives it from `MEDIA_WORKER_SHARED_SECRET`. |
| `INSTAGRAM_COOKIES_B64` | env | `app/config.py`, `app/main.py`, `app/social/stream_download.py` | no | yes | optional | keep if needed | Cookie/private auth value; never commit. |
| `YOUTUBE_COOKIES_B64` | env | `app/config.py`, `app/main.py`, `app/social/stream_download.py` | no | yes | optional | keep if needed | Cookie/private auth value; never commit. |
| `FACEBOOK_COOKIES_B64` | env | `app/config.py`, `app/main.py`, `app/social/stream_download.py` | no | yes | optional | keep if needed | Cookie/private auth value; never commit. |
| `TIKTOK_COOKIES_B64` | env | `app/config.py`, `app/main.py`, `app/social/stream_download.py` | no | yes | optional | keep if needed | Cookie/private auth value; never commit. |
| `SENTRY_DSN` | env | `app/observability/sentry.py` | production monitoring | yes | active queue observability | keep | Unset disables Sentry; never print or commit a real value. |
| `SENTRY_DEV_TEST` | env | `app/observability/sentry.py`, `scripts/validate_env.py` | no | no | controlled verification only | keep false normally | May be `true` only for the one development verification command; production validation requires false. |
| `INSTAGRAM_RAPIDAPI_KEY` | env | `app/config.py`, `app/media_resolvers/registry.py` | active development Instagram adapter | yes | external AI resolver | keep server-only | Required only when `rapidapi_instagram_reels13` can receive traffic. |
| `TIKTOK_AKNG_ACCESS_TOKEN` | env | `app/config.py`, `app/media_resolvers/registry.py` | active development TikTok adapter | yes | external AI resolver | keep server-only | Required only when `akng_tiktok` can receive traffic. |

The resolver mode, audience, both adapter chains, cookie fallback, and YouTube
direct flag are config-only policy. Do not duplicate them in Render.

## Config Variables

These moved to committed JSON config. Existing Render env values with the same names still override config for backward compatibility until cleanup.

| Variable name | Source | Used by file | Required? | Secret? | Current worker path? | Status | Notes |
|---|---|---|---:|---:|---:|---|---|
| `AI_ANALYSIS_MODE` | config/default/env override | `app/config.py`, `app/job_runner.py`, `app/queue_worker.py` | no | no | yes | moved | Defaults to `gemini_video_poc`. |
| `ALLOW_PRIVATE_METADATA_FAILURE` | config/default/env override | `app/config.py`, `app/queue_worker.py` | no | no | yes | moved | Keeps private metadata save failures non-fatal by default. |
| `ALLOW_RAW_URL_LOGS` | config/default | `scripts/validate_env.py` | no | no | safety guard | moved | Must remain `false`. |
| `ENABLE_INSTAGRAM_COOKIES` | config/default/env override | `app/config.py`, `app/main.py`, `app/social/stream_download.py` | no | no | optional | moved | Default false. |
| `ENABLE_METADATA_ONLY_MOCK_FALLBACK` | config/default/env override | `app/config.py`, `app/main.py`, `app/social/stream_download.py` | no | no | mock/dev fallback | moved | Default true for mock/dev fallback behavior. |
| `ENABLE_QUEUE_WAKE_ENDPOINT` | config/default/env override | `app/config.py`, `app/main.py` | no | no | yes | moved | Default true. |
| `ENABLE_TIKWM_RESOLVER` | config/default/env override | `app/config.py`, `app/social/tikwm_resolver.py` | no | no | TikTok resolver | moved | Default true; production validation requires it. |
| `ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR` | config/default/env override | `app/config.py`, `app/queue_worker.py` | no | no | yes | moved | Default true for queue social extraction. |
| `ENABLE_YTDLP_COOKIES` | config/default/env override | `app/config.py`, `app/main.py`, `app/social/stream_download.py` | no | no | optional | moved | Default false. |
| `GEMINI_EXTRACTION_MODE` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | yes | moved | Default `single_pass_structured`. |
| `GEMINI_FALLBACK_MODEL` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | yes | moved | Default `gemini-2.5-flash`. |
| `GEMINI_FILE_MAX_SIZE_BYTES` | config/default/env override | `app/config.py`, `app/main.py`, `app/social/stream_download.py` | no | no | provider guard | moved | Default 2,000,000,000 bytes; worker hard limit cannot exceed it. |
| `GEMINI_FILE_MAX_WAIT_SECONDS` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | yes | moved | Default 120. |
| `GEMINI_FILE_POLL_INTERVAL_SECONDS` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | yes | moved | Default 5. |
| `GEMINI_FILE_UPLOAD_TIMEOUT_SECONDS` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | yes | moved | Default 120. |
| `GEMINI_JSON_FALLBACK_ENABLED` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | legacy two-step | moved | Default true. |
| `GEMINI_JSON_RETRY_ENABLED` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | legacy two-step | moved | Default true. |
| `GEMINI_MAX_CALLS_PER_JOB` | config/default/env override | `app/config.py`, `app/main.py` | no | no | quota guard | moved | Default 3: primary, text-only repair, bounded compatible-model fallback; entitlement quota unchanged. |
| `GEMINI_MAX_OUTPUT_TOKENS` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | yes | moved | Default 12000. |
| `GEMINI_PRIMARY_MODEL` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | yes | moved | Default `gemini-3.1-flash-lite`. |
| `GEMINI_RECIPE_REPAIR_MAX_RETRIES` | config/default/env override | `app/config.py`, `app/queue_worker.py` | no | no | yes | moved | Default 1. |
| `GEMINI_SAVE_MODE` | config/default/env override | `app/config.py`, `app/main.py` | no | no | yes | moved | Default `structured_json_with_brief_fallback`. |
| `GEMINI_STRUCTURED_OUTPUT_ENABLED` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | yes | moved | Default true. |
| `GEMINI_TEXT_MODEL` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | optional | moved | Default null; effective model falls back to primary. |
| `GEMINI_THINKING_LEVEL` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | yes | moved | Default `high`. |
| `GEMINI_VIDEO_ANALYSIS_MODE` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | yes | moved | Default `two_step_brief_then_json`; single-pass mode controls current extraction. |
| `GEMINI_VIDEO_BRIEF_MAX_RETRIES` | config/default/env override | `app/config.py`, `app/queue_worker.py` | no | no | legacy two-step | moved | Default 1. |
| `GEMINI_VIDEO_BRIEF_RETRY_ENABLED` | config/default/env override | `app/config.py`, `app/queue_worker.py` | no | no | legacy two-step | moved | Default true. |
| `GEMINI_VIDEO_INPUT_MODE` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | yes | moved | Default `file_api`. |
| `GEMINI_VIDEO_MODEL` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | optional | moved | Default null; effective model falls back to primary. |
| `LOCAL_BRIEF_PARSER_ENABLED` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | fallback | moved | Default false. |
| `MAX_VIDEO_DURATION_SECONDS` | config/default/env override | `app/config.py`, multiple worker paths | no | no | yes | moved | Default 120; validator enforces `<= 120`. |
| `MAX_VIDEO_HEIGHT` | config/default/env override | `app/config.py`, `app/social/stream_download.py` | no | no | yes | moved | Default 720 short-edge pixels. |
| `MAX_VIDEO_SECONDS` | env override alias | `app/config.py`, legacy env | no | no | yes | legacy alias | Still accepted as alias for `MAX_VIDEO_DURATION_SECONDS`; do not use in new env files. |
| `MAX_VIDEO_SIZE_MB` | config/default/env override | `app/config.py`, `app/main.py`, `app/social/stream_download.py` | no | no | yes | moved | Hard default 50 MiB; historical name says MB but conversion is binary MiB. |
| `MEDIA_ADAPTIVE_MAX_ATTEMPTS` | config/default/env override | `app/config.py`, `app/social/stream_download.py` | no | no | yes | moved | Default and maximum 4 bounded format attempts. |
| `MEDIA_EXTRACT_OUTPUT_MODE` | config/default/env override | `app/config.py`, `app/social/stream_download.py` | no | no | yes | moved | Default `temp_file`. |
| `MIN_VIDEO_HEIGHT` | config/default/env override | `app/config.py`, `app/social/stream_download.py` | no | no | yes | moved | Default 360 short-edge pixels. |
| `MOCK_ANALYSIS_DELAY_MS` | config/default/env override | `app/config.py`, `app/job_runner.py` | no | no | mock/dev | moved | Default 500. |
| `MOCK_RECIPE_FIXTURE_PATH` | config/default/env override | `app/config.py`, `app/job_runner.py` | no | no | mock/dev | moved | Default `app/fixtures/mock_recipe_card.json`. |
| `MOCK_VIDEO_PROCESSING_MODE` | config/default/env override | `app/config.py`, `app/job_runner.py`, `app/main.py` | no | no | mock/dev | moved | Default `skip_video_download`. |
| `QUEUE_EMPTY_SLEEP_SECONDS` | config/default/env override | `app/config.py`, `app/queue_worker.py` | no | no | yes | moved | Default 5. |
| `QUEUE_POLL_INTERVAL_SECONDS` | config/default/env override | `app/config.py`, `app/queue_worker.py` | no | no | yes | moved | Default 5. |
| `PREFERRED_VIDEO_SIZE_MB` | config/default/env override | `app/config.py`, `app/main.py`, `app/social/stream_download.py` | no | no | yes | moved | Soft default 36 MiB; historical name says MB but conversion is binary MiB. |
| `REAL_AI_FALLBACK_TO_MOCK` | config/default/env override | `app/config.py`, `app/queue_worker.py` | no | no | fallback | moved | Default false; production config must keep false. |
| `RECIPE_BILINGUAL_OUTPUT_ENABLED` | config/default/env override | `app/config.py`, `app/queue_worker.py` | no | no | yes | moved | Default true. |
| `RECIPE_CARD_NORMALIZATION_ENABLED` | config/default/env override | `app/config.py`, `app/queue_worker.py` | no | no | yes | moved | Default true. |
| `RECIPE_MIN_BRIEF_LENGTH` | config/default/env override | `app/config.py`, `app/queue_worker.py` | no | no | yes | moved | Default 500. |
| `RECIPE_MIN_INGREDIENTS` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | yes | moved | Default 2. |
| `RECIPE_MIN_STEPS` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | yes | moved | Default 3. |
| `RECIPE_QUALITY_GATE_ENABLED` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | yes | moved | Default true. |
| `RECIPE_REPAIR_ENABLED` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | yes | moved | Default true. |
| `RECIPE_URDU_FALLBACK_ENABLED` | config/default/env override | `app/config.py`, `app/queue_worker.py` | no | no | yes | moved | Default true. |
| `TEMP_MEDIA_DIR` | config/default/env override | `app/config.py`, `app/job_runner.py`, `app/social/stream_download.py` | no | no | yes | moved | Default `/tmp/recipe-dost-media`. |
| `THUMBNAIL_BUCKET` | config/default/env override | `app/config.py`, `app/main.py`, `app/social/thumbnail_upload.py` | no | no | yes | moved | Default `recipe-thumbnails`. |
| `WORKER_EXECUTION_MODE` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | yes | moved | Default `queue`. |
| `YOUTUBE_AI_INPUT_MODE` | config/default/env override | `app/config.py`, `app/main.py`, `app/queue_worker.py` | no | no | yes | moved | Default `gemini_url`. |
| `YOUTUBE_METADATA_MODE` | config/default/env override | `app/config.py`, `app/main.py` | no | no | yes | moved | Default `oembed`. |
| `YTDLP_USER_AGENT` | config/default/env override | `app/config.py`, extractor paths | no | no | optional | moved | Default null. |

## Legacy Or Deprecated Env Keys

These may remain in the current Render env group during Phase A. They should be removed only after the config-based worker deploys and validates.

| Variable name | Source | Used by file | Required? | Secret? | Current worker path? | Status | Notes |
|---|---|---|---:|---:|---:|---|---|
| `ENABLE_RENDER_INSTAGRAM_RESOLVER` | config/default/env override | `app/config.py`, `app/job_runner.py`, `app/main.py`, `app/social/simple_resolvers.py` | no | no | production Instagram gate | moved | Production JSON keeps this true for parity; do not put it back in clean env templates. |
| `ENABLE_RENDER_FACEBOOK_RESOLVER` | code/default/env override | `app/config.py`, `app/job_runner.py`, `app/main.py`, `app/social/simple_resolvers.py` | no | no | legacy/manual paths | deprecated env | Leave only if intentionally testing legacy resolver paths. |
| `ENABLE_SOCIAL_VIDEO_GEMINI` | code/default/env override | `app/config.py`, `app/job_runner.py`, `app/main.py` | no | no | legacy/manual paths | deprecated env | Current queue path uses `AI_ANALYSIS_MODE=gemini_video_poc`. |
| `SOCIAL_ANALYSIS_MODE` | code/default/env override | `app/config.py`, `app/job_runner.py`, `app/main.py` | no | no | legacy/manual paths | deprecated env | Default is `text_first`. |
| `TIKWM_API_URL` | config/default/env override | `app/config.py`, `app/social/tikwm_resolver.py` | no | no | TikTok resolver | moved | Default `https://www.tikwm.com/api/`; do not put it back in clean env templates. |
| `WORKER_MODE` | code/default/env override | `app/config.py`, `app/main.py` | no | no | health/legacy | deprecated env | Current queue config does not need it in Render env. |
| `QUEUE_RECOVER_STALE_JOBS` | env only from old file | no current source match | no | no | no | remove | No active code reference found. |
| `QUEUE_STALE_JOB_MINUTES` | env only from old file | no current source match | no | no | no | remove | No active code reference found. |
