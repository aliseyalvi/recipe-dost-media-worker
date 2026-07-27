# Recipe Dost Media Worker Milestones

| Order | Milestone | Status | Goal |
|---:|---|---|---|
| 1 | M14G-1 Media Job Foundation | Done | Supabase media job tables/events |
| 2 | M14G-2 Cloud Worker Skeleton | Done | Cloud Run worker connected |
| 3 | M14G-3 YouTube Gemini Pipeline | Done | YouTube URL -> Gemini -> recipe card |
| 4 | M14G-3B Quota Guard | Done | Free-tier Gemini protection |
| 5 | M14G-3C Stable JSON Pipeline | Done | Observation -> validated recipe JSON |
| 6 | M14G-4R0 Repo Context Setup | Done | Codex context docs |
| 7 | M14G-4R1 TikTok yt-dlp Parser | Done | Parse formats, preserve headers, stream bounded bytes |
| 8 | M14G-4TWM0 TikWM Cloud Run Debug | Done | Protected debug-only TikWM metadata/probe endpoint |
| 9 | M14G-4TWM1 TikWM Cloud Run Probe | Done | `/debug/tikwm` verified from Cloud Run |
| 10 | M14G-4SOC1 Unified Social Resolver | Done | TikTok->TikWM, Instagram/Facebook->Cobalt |
| 11 | W-M14G-4SOC2A Gemini Video Stabilization | Done | Timeouts, Files API social upload path, controlled failures |
| 12 | W-M14G-4SOC2B DB-Safe Status + Guards | Done | DB-safe statuses, duration guard, size guard error split |
| 13 | W-M14G-4SOC2C Stable Social Analysis Mode | Done | TikTok text-first, social video opt-in, whole-call video timeout |
| 14 | W-M14R1 Render Async Worker POC | Done | Existing worker runs `/process-job` through FastAPI BackgroundTasks for Render Free testing |
| 15 | W-M14R1A Queued Initial Status | Done | Render async worker accepts `queued` jobs from create-media-job manual mode |
| 16 | W-M14R1B TikTok Routing Fix | Done | Queued TikTok jobs route to TikWM text-first before unsupported fallback |
| 17 | W-M14R1C Replace Gemini text helper with sync REST call | Done | TikTok text-first uses direct sync REST call to Gemini generateContent instead of failing SDK helper |
| 18 | W-M15A TikTok video-to-Gemini fallback | Done | After text-first insufficient, fall back to video Gemini behind ENABLE_SOCIAL_VIDEO_GEMINI flag |
| 19 | W-M15D0 Mock Fixture AI Mode | Done | Development mode skips Gemini calls and saves a fixed fixture recipe through the real worker pipeline |
| 20 | W-M15D1 Public/private resolver metadata persistence | Done | Save app-visible resolver metadata separately from private CDN/raw metadata |
| 21 | W-M15D2 Resolver adapter matrix without Cobalt | Done | Mock mode supports TikTok, YouTube, Instagram, and Facebook resolver adapters without Gemini quota |
| 22 | W-M16Q-POC Serialized Queue Worker | Done | Queue execution mode polls oldest queued job and processes one at a time through unified yt-dlp extractor plus mock fixture save |
| 23 | W-M16Q-POC-C1 Cookie-aware yt-dlp extractor + metadata fallback | Done | Unified yt-dlp tries no-cookie first, retries platform cookies when configured, and can complete mock jobs from OpenGraph metadata-only fallback |
| 24 | W-M16G-POC Real Gemini video analysis from verified temp file | Done | Queue mode can send extractor-verified temp-file video bytes to Gemini and save a real recipe card |
| 25 | W-M16G-C1 Gemini Files API video analysis | Done | Queue Gemini POC uses the official google-genai Files API, waits for ACTIVE, analyzes the verified temp file, and saves the recipe card |
| 26 | W-M16G-C2 Two-step Gemini video recipe pipeline | Done | Queue Gemini POC extracts a plain video recipe brief first, then converts that brief to strict recipe JSON |
| 27 | W-M16G-C3 Robust Gemini JSON conversion with brief fallback | Done | Queue Gemini POC retries text JSON conversion and falls back to a local recipe card from the successful brief |
| 28 | M17A Queue wake endpoint + YouTube Gemini URL | Done | `/wake` starts the queue loop without processing inline, and YouTube queue Gemini uses native URL input instead of yt-dlp |
| 29 | M18 Stabilize E2E recipe pipeline | Done | Queue Gemini cards normalize into one structured recipe shape and YouTube private metadata failures no longer stop jobs |
| 30 | M20 Gemini 3.1 single-pass structured extraction | Done | Gemini 3.1 structured JSON extraction with quality gate, repair, fallback model, grocery derivation, and Urdu fields |
| 31 | M20.5 Worker repo cleanup before production cutover | Done | Queue mode documented as production flow, legacy webhook path de-emphasized, and `feature/queue-ytdlp-poc` marked ready to merge to `main` |
| 32 | M21.7A Worker metadata preview endpoint | Done | Protected `/metadata-preview` returns lightweight title/thumbnail/duration metadata without Gemini or video downloads |
| 33 | M21.9B Thumbnail selection quality | Done | Prefer platform/oEmbed and best yt-dlp thumbnails, avoid avatar/tiny candidates, and log safe thumbnail source labels |
| 34 | M21.9B.1B Platform-first no-downgrade thumbnails | Done | Use platform_first_v2 source labels and prevent sampled-frame fallbacks from replacing existing platform thumbnails |
| 35 | M21.9B.2 YouTube Shorts thumbnail variants | Done | Prefer deterministic YouTube thumbnails over numbered frame variants |
| 36 | M22B.2D Gemini structured extraction resilience | Done | Repair/fallback structured extraction failures with safe terminal metadata |
| 37 | M22D.0B Worker env source-of-truth setup | Done | Dev/prod env templates, validation, Render export, and docs |
| 38 | M22D.0C Worker env names audit and cleanup | Done | Audit env usage, prune stale template keys, and document source/runtime names |
| 39 | M22D.0D Worker env/config rationalization | Done | Move non-secret runtime settings to JSON config while preserving env overrides |
| 40 | M22D.3 Production worker setup result log | Done | Production Supabase and Render worker/env group setup logged with remaining blockers |
| 41 | M22D.5A Production worker extractor config parity fix | Done | Production JSON explicitly enables the queue/Gemini/unified yt-dlp Instagram extraction path after env cleanup |
| 42 | M22D.5B Social resolver production parity and TikTok NameError fix | Done | Add safe config summary, keep production social resolver validation strict, and fix TikWM resolver timing NameError |
| 43 | M22D.5C Social pipeline source-of-truth audit | Done | Audit-only resolver/source-of-truth map explaining TikWM, Instagram disabled gate, metadata preview split, and minimal prod parity plan |
| 44 | M22D.5D Queue orchestration compatibility fix | Done | `/process-job` wakes queue mode instead of scheduling the legacy direct runner for normal queued jobs |
| 45 | M22D.5E Production config packaging/loading fix | Done | Docker image packages `config/`, runtime config loads by absolute project path, and production fails if `worker.production.json` is missing |
| 46 | M22D.5F Production worker smoke completion log | Done | Production worker health and YouTube/Instagram/TikTok end-to-end smoke passed |
| 47 | M22E.0B Worker branch and environment safety | Done | Safe env doctor, branch guard, Make checks, and dev/prod Render branch workflow docs |
| 48 | M22E.1R-B AI-generated tags end-to-end | In Progress | Pushed to `develop`; active queue Gemini tag contract, normalization, and `recipe_cards.tags_json` persistence; development Render/database smoke awaits authenticated verification |
| 49 | M22E.2C Privacy-safe worker Sentry monitoring | Done | Explicit active-queue-only Python Sentry capture, fail-closed production/development branch policy, strict sanitizer, and controlled development verification command |
| 50 | M23.W2 Adaptive media processing and worker outcomes | Done locally | Size-aware 720p-to-360p selection, 36/50 MiB guards, Build 3-compatible failure persistence, safe Render logs, and grouped terminal Sentry warnings; staged Render verification pending |
| 51 | M23.W2B Direct Instagram media candidate regression | Done locally | Normalize formats/requested/top-level candidates, preserve unknown audio until bounded post-download probe, and add safe beta outcome telemetry; development verification pending |
| 52 | M23.W3A External media resolver architecture | Done locally | Audit-only metadata/AI separation, provider-neutral resolver contract, Build 3-compatible routing, security, observability, rollout, and rollback plan |
| 53 | M23.W3C Feature-flagged external media resolver | Committed to develop | Instagram Reels13 and TikTok AKNG adapters, guarded download/probe/mux, active-queue routing, no-cookie metadata, quota guard, fixtures, and rollback flags |
| 54 | M23.W3C-R File-based resolver config remediation | Done | File Settings source, safe source diagnostics, Python 3.12 tests, and healthy development Render startup |
| 55 | M23.W3D-IG Development Instagram resolver verification | Passed | External route, guarded media preparation, Gemini extraction, recipe save, and temp cleanup completed |
| 56 | M23.W3D-TK1 AKNG TikTok contract repair | Insufficient | Numeric-ID normalization was not validated against short-link provider behavior |
| 57 | M23.W3D-TK2 AKNG input telemetry | Failed live | Unresolved short URL reached AKNG and returned an empty success wrapper |
| 58 | M23.W3D-TK3B3 Python/curl parity and media proof | Passed locally | Numeric-ID primary, DNS-pinned short-link expansion, throughput-ranked AKNG proxy alternatives, and both live local A/V gates passed |
| 59 | M23.W3I-IG2 Instagram split-AV compatibility repair | Done locally | Preserve unknown declarations for secure probe, keep roles separate, accept verified H.264 MP4, conditionally mux audio, and add an owner-only live gate |
| 60 | M23.W6A-W6C Deterministic replay, Gemini reliability, queue finalization audit | Done locally | Offline replay/batch harness, model-specific structured extraction, closed failures, implicit quota-finalization proof, and private metadata fix |
| 61 | M23.W7A-W7C Staged combined worker architecture | Done locally; durable lease migration blocked | Typed stages, bounded in-process transport, resource admission, combined/fail-closed standalone entry points, ACTIVE-point cleanup, deterministic staged/concurrency-two QA; cross-process lease recovery needs approval |
| 61a | M23.W7C-R Staged analyzer parity repair | Targeted checkpoint passed | Shared primary invocation, safe preflight exception record, Gemini 2.5 schema conversion, retained local media, and independent per-job budget targeted tests pass |
| 61b | M23.W7C-RP2 Gemini runtime parity and readiness | Done locally; full-suite baseline mismatch remains | Pins google-genai/Pydantic, validates full production configs offline/startup, detects schema mutation, and blocks queue launch when not ready |
| 61c | M23.W7C-RP3 Gemini request boundary | Done locally; owner live canary pending | Restores primary response-schema semantics, prepares typed file contents, and separates local SDK transformation from provider rejection |
| 62 | M14G-4R2 Social End-to-End Test | Blocked | Full TikTok/Instagram/Facebook job tests |
| 63 | M15 Auth + Account Linking | Later | Email/Google auth |
| 64 | M16 RevenueCat Paywall | Later | Paid AI features after free credits |

## Current Next Action

Review M23.W7A-W7C, then owner-deploy only the `develop` revision to the
development Render service. Verify combined-worker sequential compatibility
first, then run the bounded staged Premium TikTok/Instagram soak at concurrency
one. Durable lease/schema work remains separately approval-gated.
Production stays unchanged.

- M23.W6A-W6C keeps single-pass structured extraction as the active strategy:
  one video call, one text-only repair when useful, and one compatible-model
  fallback only when recovery requires it. The exact maximum is three calls.
  The offline mixed batch completed 5/7 expected successes with all seven jobs
  terminal, FIFO, serialized, cleaned, and quota-finalized.
- `private_metadata_save_failed` was a worker payload defect: optional external
  metadata became SQL null although the deployed JSONB column is non-null.
  Missing raw metadata now persists as `{}`; no migration is required.

- M23.W3I-IG2 proved the incident-compatible 1080x1920 candidate was rejected
  by the shared 720 short-edge gate, not by its missing codec. Instagram now
  treats unknown declared codec/container as probe-required, preserves video
  and audio roles plus URL alternatives, securely downloads and ffprobes actual
  media, skips mux for embedded audio, and uses video stream copy with bounded
  audio-only AAC fallback when muxing is required. The live codec remains
  unknown until the owner diagnostic runs.

- M23.W3D-IG passed in development. `private_metadata_save_failed` remains an
  open nonblocking metadata issue outside the working Instagram media/Gemini
  path. The legacy Instagram selector remains parked.
- M23.W3D-TK1 was insufficient and TK2 failed because a `vt`/`vm` URL was sent
  directly. TK3B3 expands TikTok-only redirects and sends the extracted numeric
  ID first; a known canonical URL is the one eligible alternate. The owner
  manually verified complete AKNG contracts for both canonical URL and numeric
  ID inputs. Final numeric-ID and short-link diagnostics both exit zero using
  the shared worker path and the live-proven 4.7 MB H.264 candidate.

- M23.W3C-R root cause is repaired locally: mode/audience loaded because their
  Settings defaults queried JSON, while chains and CDN suffix lists used empty
  factories and never received the merged file. JSON is now a real Settings
  source, branch/file mismatches fail closed, and diagnostics/tests cover file
  arrays, comma env overrides, blank values, missing/malformed files, startup,
  and production isolation. Python 3.12 tests pass; Docker execution remains
  pending because this machine has no Docker executable.

- M23.W3C is complete locally. Development config selects external resolution
  for all already-authorized AI jobs; production remains legacy/disabled with
  cookie fallback false. Instagram and TikTok adapters, safe media preparation,
  Build 3-compatible errors/events, atomic claim, Gemini call guard, and tests
  are implemented without a provider call or deployment.

- M23.W3A is complete locally as an audit-only milestone. The active metadata
  preview is download-free but still cookie-capable, while active social AI
  extraction remains coupled to yt-dlp/cookies and temp-file download. The
  target separates public/no-cookie metadata from AI resolution, keeps YouTube
  direct to Gemini, and starts external social routing with selected server-side
  test users behind fail-closed flags. No runtime, provider, secret, schema,
  Render, or mobile change was made.

- M23.W2B is complete locally. The confirmed top-level Instagram candidate now
  normalizes as one `top_level_direct/audio_unknown` format and receives a
  bounded attempt. The current Docker image lacks ffprobe/ffmpeg, so unknown
  audio cannot yet be confirmed or optimized in Render; this remains an
  explicit development verification blocker, not a zero-candidate regression.

- M23.W2 is complete locally. Active queue social extraction now pins
  size-estimated combined audio/video formats through a bounded
  720p/540p/480p/360p ladder, with a 36 MiB preferred target and 50 MiB hard
  streaming/temp-file guard. Terminal `media_size_exceeded` persists the
  existing code/message fields, produces one grouped privacy-safe warning in
  production Sentry, and emits a searchable Render WARNING. Build 3 still uses
  generic copy for this code and requires a later mobile update for improved
  wording. No deployment or mobile change was performed.

- M22E.1R-B is in progress on worker `develop` and commit `d3b29b3` is pushed. The queue structured prompt/schema now requires 5-8 searchable tags and `MediaJobRepository.insert_recipe_card` writes the normalized array to the existing `recipe_cards.tags_json` JSONB field. Local normalization and persistence-mapper smoke tests pass. The Render dashboard is unauthenticated in this workspace, so deployment observation and a disposable development job/card query remain pending. Next: authenticate to the development Render dashboard, verify the `develop` revision, then create one disposable development AI media job and confirm only its linked card's 5-8 compliant `tags_json` shape/count; do not deploy production.

- M22E.2C is complete locally on worker `develop`. Production Sentry remains fail-closed until the production Render service on `main` receives a valid `SENTRY_DSN`; development remains disabled except for `python -m app.observability.sentry_test` with the explicit development-test flag and branch match. No Render deployment or live event was performed.

- M22D.0B added `env/development.env` and `env/production.env` as untracked source files with committed examples, masked validation, temporary Render export, and `docs/m22d-worker-environments.md`. Existing root `.env` files should be migrated manually if they point to dev Supabase.
- M22D.0C added `docs/m22d-worker-env-variable-map.md`, pruned clean env examples to canonical source keys plus current queue/Gemini path overrides, and removed stale TikWM/Cobalt/RapidAPI-style resolver keys from templates.
- M22D.0D added committed runtime config JSON files plus `app/runtime_config.py`; old Render env variables still override config until the phased env group cleanup is verified.
- M22D.3 logged production Supabase ref `cjlvkagkcfswsycsblfw`, Render production env group `recipe-dost-worker-prod`, and Render production service `recipe-dost-media-worker-prod`. Production Supabase migrations and SQL smoke tests passed. Render deploy/health/log smoke is pending, and remaining blockers are production Supabase secret sync, production Edge Function deploy, production function smoke tests, and RevenueCat/Google Play setup.
- M22D.5A fixes production worker extractor config parity. Root cause: production JSON did not explicitly carry the non-secret Instagram/unified yt-dlp extractor flags after env cleanup. Fix: keep `WORKER_EXECUTION_MODE=queue`, `AI_ANALYSIS_MODE=gemini_video_poc`, `ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR=true`, `MEDIA_EXTRACT_OUTPUT_MODE=temp_file`, Instagram resolver/cookie booleans, and Gemini single-pass model/mode settings in `config/worker.production.json`; keep cookie values only in env. Next step: redeploy the production worker and rerun the production smoke test.
- M22D.5B records that YouTube production smoke passed, Instagram failed with `instagram_resolver_disabled`, and TikTok failed after `tikwm_resolver_started` with a `NameError`. Fix: `/health.safe_config` and startup logs expose only non-secret runtime config summary fields, and the TikWM path initializes resolver timing before use. M22D.5D replaces any production-primary TikWM implication with queue/unified-extractor compatibility checks.
- M22D.5C adds `docs/m22d-social-pipeline-audit.md` as an audit-only source-of-truth review. Finding: observed TikWM/disabled-Instagram failures are from the legacy `/process-job` background path, while the intended production social AI workflow is the queue path with unified yt-dlp extraction for TikTok/Instagram. Metadata preview uses a separate metadata-only resolver flow.
- M22D.5D fixes worker-side orchestration compatibility. In queue mode, `/process-job` now validates and wakes the queue loop without scheduling legacy `JobRunner.run()`, so normal queued TikTok/Instagram jobs should be claimed by the queue worker and use unified yt-dlp extraction. TikWM remains legacy/manual/debug only; the M22D.5B TikWM NameError fix is kept for that legacy path.
- M22D.5E fixes the production config load gap observed in `/health`: Render env was clean, but the Docker image did not copy `config/`, so runtime config fell back to code defaults. Docker now copies `config/`, the loader reports loaded/missing config files, and production fails startup if `worker.production.json` is absent.
- M22D.5F records the completed production worker smoke. `/health` confirmed `worker.production.json` was loaded, queue mode and `/process-job` queue-wake compatibility were active, unified yt-dlp extraction and Instagram/yt-dlp cookies were enabled, max duration was 120, and raw URL logging was false. Production YouTube, Instagram, and TikTok AI recipe generation passed end to end through the production app/backend/worker/Gemini path.
- Final production worker architecture: same main branch, separate Render services and env groups for dev/prod. Render env contains secrets/environment identity only; `config/worker.production.json` contains non-secret production runtime config.
- M22E.0B adds `scripts/check_environment.py`, `make env-doctor-dev`, `make env-doctor-prod`, `make check-dev`, and `make check-prod`. Dev deploy checks prefer `develop`; production deploy checks require `main` unless `ALLOW_NON_MAIN_PROD_DEPLOY=1`. Release flow is `develop` -> dev Render smoke -> merge `main` -> prod Render deploy -> health and platform smoke.
- `/metadata-preview` is available for app-side saved-link previews before AI generation. It requires the worker secret via `X-Worker-Secret` or `Authorization: Bearer`, uses oEmbed/yt-dlp/OpenGraph metadata only, does not call Gemini, and does not download videos.
- `WORKER_EXECUTION_MODE=queue` is the primary production mode; FastAPI startup and `GET /wake` keep one serialized queue loop active.
- `/process-job` is retained for legacy/manual/debug kicks only and is not the primary production flow.
- create-media-job creates `queued` jobs; the queue loop claims the oldest job as `resolving`.
- Queued TikTok and Instagram jobs must route by `media_jobs.platform` into the unified extractor/temp-file Gemini path, not the old YouTube-only unsupported branch.
- Gemini 3.1 Flash-Lite single-pass structured extraction is the default documented AI path.
- YouTube queue jobs use Gemini native URL input when `YOUTUBE_AI_INPUT_MODE=gemini_url`, not yt-dlp.
- TikTok and Instagram queue jobs use verified temp-file extraction plus Gemini Files API.
- Local deep parser fallback stays disabled by default with `LOCAL_BRIEF_PARSER_ENABLED=false`.
- Facebook should fail with `facebook_resolver_disabled` until `ENABLE_RENDER_FACEBOOK_RESOLVER=true` after validation.
- No job should remain stuck at `analyzing` progress `50` or `60`; Gemini hangs should become controlled failed jobs.
- Known videos longer than `MAX_VIDEO_DURATION_SECONDS` should fail with `video_duration_exceeded` before stream or Gemini events.
- For quota-free app/backend testing, set `AI_ANALYSIS_MODE=mock_fixture`; TikTok still resolves through TikWM and uploads the real thumbnail, then emits `mock_ai_analysis_started`/`mock_ai_analysis_completed` and saves `app/fixtures/mock_recipe_card.json` without Gemini quota usage.
- Resolver metadata is now split: `media_jobs.source_metadata` gets public app-visible fields and media flags, while CDN/raw resolver data is saved only in `media_job_private_metadata`.
- Mock mode resolver matrix: TikTok uses TikWM, YouTube uses yt-dlp metadata, Instagram uses SnapInsta behind `ENABLE_RENDER_INSTAGRAM_RESOLVER`, and Facebook uses SnapSave behind `ENABLE_RENDER_FACEBOOK_RESOLVER`.
- W-M16Q-POC adds `WORKER_EXECUTION_MODE=queue`: FastAPI starts one in-process queue loop, selects the oldest `queued` job, claims it as `resolving`, extracts metadata/temp media with unified yt-dlp, saves public/private metadata, uses the mock fixture recipe save path, completes or fails at progress `100`, then deletes temp media before polling again.
- W-M16Q-POC post-extraction fix: queue mode now shares the webhook mock fixture recipe-card save helper and reports post-extraction DB/API failures with stage-specific error codes instead of `unified_extractor_failed`.
- W-M16Q-POC-C1 makes the unified extractor cookie-aware and metadata tolerant: first try no cookies, retry platform cookies when configured, emit video-byte verification for real downloads, and in mock mode complete from OpenGraph metadata when video bytes are inaccessible.
- W-M16G-POC adds `AI_ANALYSIS_MODE=gemini_video_poc`: queue mode requires `video_bytes_verified=true` plus an existing temp file, uploads that temp file to Gemini, validates strict recipe JSON, saves a real recipe card with `ai_provider=gemini`, and deletes temp media in `finally`.
- W-M16G-C1 switches queue Gemini POC upload/read to the official `google-genai` Files API with `GEMINI_VIDEO_INPUT_MODE=file_api`, sanitized file upload/processing/analysis events, ACTIVE polling, specific Gemini/file error codes, and no CDN URL/source URL/cookie path/temp path/prompt/raw response/file reference in events or public metadata.
- W-M16G-C2 changes queue Gemini POC from direct video-to-JSON to two steps: video file to plain recipe brief, then brief to strict JSON. Empty brief/JSON responses now fail with specific codes and safe diagnostics.
- W-M16G-C3 makes brief-to-card robust: JSON conversion retries transient Gemini server failures, falls back from flash-lite to flash, and if conversion still fails builds a local recipe card from the successful brief so the job can complete.
- M17A adds `GET /wake` for Render Free wakeups and routes YouTube queue Gemini jobs through oEmbed metadata plus native Gemini YouTube URL input, skipping yt-dlp downloads and preserving the existing TikTok/Instagram temp-file path.
- M18 adds recipe-card normalization before Gemini queue inserts so TikTok, Instagram, and YouTube share one structured ingredient/step/grocery shape. Private metadata save failures are event-only by default and no longer block YouTube Gemini URL jobs.
- Queue Gemini optimization caps video brief output, JSON output, and oversized brief text sent into JSON conversion to reduce token usage and latency while keeping the two-step/fallback flow.
- M20 replaces the default queue Gemini brief-to-JSON flow with `GEMINI_EXTRACTION_MODE=single_pass_structured`: Gemini 3.1 Flash Lite extracts canonical recipe JSON directly from YouTube URL or uploaded temp video file, repair uses invalid JSON/errors only, and unrecoverable weak output fails with `recipe_quality_gate_failed`. The old two-step mode remains available only through `GEMINI_EXTRACTION_MODE=two_step_brief_then_json`.
- M20.5 confirms `feature/queue-ytdlp-poc` is ready to merge to `main`; after merge, production should run from `main`, and the old webhook Render service should be suspended only after the main service validates TikTok, Instagram, and YouTube.
- M21.7A adds a worker-powered metadata preview route for saved recipe links. The route is protected by the existing worker secret, returns only safe app metadata, flags over-limit durations without failing, exposes metadata-preview support on `/health`, and leaves the queue/Gemini analysis pipeline unchanged.
- M21.9B centralizes thumbnail choice for preview and jobs: platform/oEmbed thumbnails stay first, yt-dlp `thumbnail` is used when valid, `thumbnails[]` is scored by dimensions when needed, avatar/tiny candidates are avoided, and safe `thumbnail_source` labels are stored in events/source metadata.
- M21.9B.1B tightens the thumbnail contract to `platform_first_v2`, returns source/provider/strategy/candidate fields from metadata preview, and checks existing recipe thumbnails before updating so platform image URLs are not downgraded to early-frame fallbacks.
- M21.9B.2 fixes YouTube Shorts thumbnail selection by treating numbered oEmbed frame variants as low priority, validating deterministic YouTube image candidates first, and recording the updated `platform_first_v3` strategy in safe metadata/events.
