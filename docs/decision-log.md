# Decision Log

## 2026-07-27 — M23.W7C-R analyzer parity checkpoint

- Staged and sequential analysis use the same Gemini orchestration. Local media
  remains through primary extraction, repair, and fallback, then cleanup runs.
- Gemini 2.5 `response_schema` now converts nullable JSON-Schema unions to the
  SDK Schema dialect. Primary preflight failures record safe exception/category
  fields before fallback; per-job call budgets are independent.
- Only four targeted regressions and Python 3.12 compile validation were run.
  Broader W7C-R telemetry, ordering, QA, documentation, and soak work is deferred.

## 2026-07-27 — M23.W7A-W7C staged combined worker

- One Render service now has sequential compatibility and bounded staged modes;
  the Docker/Render entry point is `python -m app.workers.combined_worker`.
- Typed internal contracts omit runtime paths/job snapshots from serialization,
  and in-process queues are the only enabled transport. Standalone stage
  commands fail closed because no durable transport is configured.
- Dispatch checks ingest capacity, RSS, worst-case temp bytes, free temp space,
  ffmpeg, and upload budgets before conditional claim. All concurrency defaults
  remain one; analyzer two is offline-tested but not approved for Render.
- Local media is deleted only after Gemini Files API reports ACTIVE. Signed
  provider/CDN URLs are not persisted even in private metadata.
- The existing schema has no lease owner/expiry, heartbeat, attempt fence,
  durable stage, or result idempotency key. Unsafe `started_at` reclaim was not
  added and no migration was created; true crash recovery/separate services
  remain approval-blocked.
- Quota terminal events remain idempotent. Probe, thumbnail, and quota snapshot
  telemetry now state the lifecycle fact they actually represent.

## 2026-07-26 — M23.W6A-W6C deterministic worker reliability

- The active strategy is one single-pass video structured-output call, followed
  by at most one text-only repair and, only when recoverable, one model fallback
  that reuses the active Gemini file. `GEMINI_MAX_CALLS_PER_JOB=3` changes the
  provider recovery budget, not user entitlement or usage limits.
- `gemini-3.1-flash-lite` uses JSON MIME, JSON Schema, and
  `thinking_level=high`. `gemini-2.5-flash` uses its own schema field and omits
  the Gemini-3 thinking-level option. Configuration is validated before quota
  accounting or provider invocation.
- Fallback telemetry is emitted only after budget/configuration preflight. A
  two-call primary+repair sequence skips fallback with
  `gemini_call_budget_exhausted` and records no provider request.
- Quota remains status-derived in the Edge access code: active statuses reserve,
  failed/cancelled release, and only completed jobs with a result card and AI
  provider count as success. Worker events audit this idempotently; there is no
  reservation row or quota RPC.
- `media_job_private_metadata.raw_metadata` is non-null JSONB. External jobs
  supplied `None`, explaining `APIError`/Postgres `23502`; missing metadata now
  writes `{}`. Service-role/RLS and types match, so no migration is needed.
- Offline replay/batch QA uses production pipeline functions and writes only a
  gitignored sanitized report.

## 2026-07-23 — M23.W3I-IG2 Instagram split-AV compatibility repair

- The incident-compatible normalized candidates are one 1080x1920 video and
  one audio entry. The video has no declared codec, declares `mp4` through the
  provider extension, has no MIME/size, and has one URL. The audio declares
  `m4a`, has no codec/MIME/size/dimensions, and has one URL.
- The exact pre-download rejection was the shared
  `short_edge > max_short_edge` predicate: 1080 exceeded the 720 TikTok policy.
  Missing codec was already accepted; missing container was not. The terminal
  H.264/MP4 message therefore misclassified a resolution-policy rejection.
- Instagram now has three declared states: `compatible_declared`,
  `incompatible_declared`, and `probe_required`. Explicit H.265/non-MP4 is
  rejected; absent/unknown declarations continue to the existing guarded
  downloader and ffprobe. MIME and URL suffix are not compatibility truth.
- Instagram video selection no longer inherits TikTok's 720 hard gate. Declared
  compatible video ranks before probe-required video, candidates remain role
  separated, provider order breaks metadata ties, and URL alternatives are
  bounded by the existing downloader.
- ffprobe now proves H.264 plus an MP4-family container before acceptance.
  Embedded audio skips separate audio and mux. Missing audio selects only an
  audio-role candidate, probes it, and muxes with argv-only ffmpeg. The first
  attempt is stream copy; the bounded fallback copies video and re-encodes
  audio to AAC only. Video transcoding was removed.
- `instagram_candidate_normalization_summary` emits only allowlisted counts.
  `scripts/check_instagram_media_candidate.py` is an opt-in, develop-only,
  hidden-input owner gate using the shared worker path without Supabase, Gemini,
  or Sentry. Codex did not run it; the live candidate's actual codec remains
  unproven.
- Direct external-URL Gemini input is deferred from this repair. The current
  worker must first download and probe to enforce its DNS/redirect/content/
  signature/byte/codec/audio policy. A future opt-in experiment may compare a
  post-verification external URL handoff against Files API upload without
  weakening those controls or persisting signed URLs.

## 2026-07-22 — M23.W3D-TK3B3 Python/curl parity and live media gate

- TK1 was insufficient; TK2 proved `normalize_akng_input()` left `vt`/`vm`
  inputs unresolved and the adapter sent that short URL directly to AKNG.
- Normal processing now expands TikTok redirects under HTTPS-only, exact-host,
  public-DNS/peer-IP, no-proxy, no-cookie, timeout, redirect-count, and bounded
  HTML guards. An unresolved short URL never reaches AKNG.
- The owner proved curl short-link expansion and then proved AKNG numeric-ID
  input on the same machine. Numeric ID is therefore the primary provider input.
  A canonical full URL is attempted once only when it is available and numeric
  input returns an empty success wrapper or explicit input rejection. Both empty maps to
  `media_unavailable`; malformed nested types after `itemInfo` map to
  `resolver_contract_error`.
- Live reproduction proved Requests/urllib3 released the zero-length 3xx
  connection before peer inspection, so `connected_peer_ip()` returned `None`.
  Short-link GETs now use a TLS connection pinned to one prevalidated public DNS
  answer while retaining TikTok hostname/SNI certificate validation, exact-host
  redirects, and bounded response handling.
- AKNG's media proxy ignores Range, omits Content-Length, and uses variable-speed
  chunked streams. The 6.7 MB H.264 candidate did not complete within budget;
  the 4.7 MB H.264 candidate completed, had a valid MP4 signature, and ffprobe
  proved embedded video and audio. TikTok therefore prefers the smallest
  compatible H.264 candidate and throughput-probes its bounded URL alternatives.
- `scripts/check_tiktok_akng_end_to_end.py` is the pre-Render gate and shares the
  worker resolver/download/probe/mux path. It requires an explicit input kind,
  is development-only, opt-in, hidden-input, and secret/URL/body safe. Offline
  tests pass. Both numeric-ID and short-link modes exited zero against the
  development provider and completed signature, ffprobe A/V, and cleanup. No
  Gemini, Supabase, Sentry, Render, or deployment operation occurred.

## 2026-07-22 — M23.W3D-TK1/TK2 superseded approach

- TK1 normalized expanded TikTok links to numeric IDs but left `vt`/`vm` links
  unresolved. TK2's live failure disproved that approach; TK3 above supersedes
  it with safe expansion and a bounded alternate input.
- `INPUT_NOT_VALID` is terminal `media_unavailable`. A malformed 2xx provider
  schema is technical `resolver_contract_error`, not a configuration error, and
  retains the 2xx status plus actual attempt count.
- Candidate eligibility uses structured provider metadata, never a URL suffix.
  H.264 MP4 stays the compatibility gate; downloaded bytes remain
  SSRF/redirect/byte/signature/ffprobe guarded, with audio muxed only when the
  video probe proves it absent.
- Optional TikTok context is bounded, sanitized, memory-only, marked untrusted
  in the Gemini instruction, and observed only through counts/buckets. No live
  provider call, deploy, Render mutation, mobile/schema change, or main merge
  was performed by Codex.

## 2026-07-21 — M23.W3C-R file-based resolver configuration

- Root cause was a partial Settings source: scalar resolver fields read JSON in
  their defaults, while chains and CDN suffix lists used empty factories. The
  merged JSON object is now a Pydantic source, so every matching field loads.
- Canonical file chains are JSON arrays; non-blank env overrides may remain
  comma-separated, while blank overrides are ignored. Unknown keys/types and
  adapter IDs fail closed.
- On Render, `develop` selects development and `main` selects production through
  existing branch metadata. `APP_ENV` is optional there and must agree when set.
  Production remains external-disabled and requires no provider credentials.
- Local tests use Python 3.12 and root `requirements.txt`. No provider call,
  deployment, Render mutation, mobile/schema change, or main merge occurred.

## 2026-07-21 — M23.W3C external resolver implementation

- The active queue now receives only provider-neutral resolutions. Instagram
  uses `rapidapi_instagram_reels13`; TikTok uses structured AKNG `bitrateInfo`
  through `akng_tiktok`; YouTube remains direct to Gemini.
- Development config enables external routing for all jobs that already passed
  server access. Production remains external-disabled. No flag grants access:
  the worker requires the server-written AI-access event before provider work.
- H.264 MP4 within 36 MiB and at most 720p is preferred; the hard final/download
  limit is 50 MiB and duration remains 120 seconds. Video is probed and separate
  audio is muxed only when needed. Explicit H.265/DASH remains unsupported.
- External URL handling is HTTPS/host-policy/SSRF/redirect/byte/signature/probe
  guarded. Direct URLs never enter events/tables. Cookie metadata is removed;
  cookie media requires both a legacy/hybrid strategy and its independent flag.
- Queue claim is conditional, paid adapter attempts are ledgered/bounded, the
  circuit breaker is local, and every active Gemini call now uses the existing
  repository-backed call ceiling. No live provider/deployment/mobile/schema or
  `main` action occurred.

## 2026-07-21 — M23.W3A external media resolver architecture

- Saved-link metadata and AI generation are separate pipelines: metadata is
  public/no-cookie, download-free, quota-free, bounded, deduplicated, and able
  to retain the existing minimal Build 3 record.
- Social AI resolution will use a provider-neutral contract behind disabled,
  fail-closed flags. Instagram and TikTok use independently testable adapters;
  YouTube keeps its public URL-to-Gemini route. Cookie yt-dlp is not a default
  and any temporary emergency fallback requires a separate false-by-default
  flag.
- The first production external canary is server-authorized selected test users,
  followed by Premium and only then `all_ai`, which includes free users' three
  existing successful generations. Known-failing social routes must reject
  before job creation instead of consuming an active reservation.
- Before rollout, the active queue must gain an atomic claim, a shared
  repository-backed guard before every Gemini call, SSRF-safe bounded media
  fetching, and one sanitized authoritative Sentry capture after retries are
  exhausted. This milestone added no integration or runtime behavior.

## 2026-07-16 — M22E.2E observability QA freeze

- Worker Sentry now fails closed unless execution is on Render with the exact
  deployment branch: production runtime requires production + `main`; the one
  controlled diagnostic requires development + `develop` + `SENTRY_DEV_TEST=true`.
- The active queue flow, retry/backoff and job-state semantics, expected product
  failure exclusions, sanitizer, and legacy-flow exclusion remain unchanged.
- Focused policy/privacy/reporting/active-boundary tests pass. No deployment,
  controlled event, provider call, live Supabase call, or legacy instrumentation
  was performed.

Keep this file terse: only durable architecture decisions, not full task history.

## 2026-07-20 — M23.W2B direct Instagram candidate normalization

- The active queue extractor normalizes `formats`, `requested_formats`/
  `requested_downloads`, and top-level direct media through one deduplicating
  layer shared by normal and cookie-authenticated metadata extraction.
- Missing audio metadata is unknown, not absent. Unknown audio may be downloaded
  under the 50 MiB guard but must be confirmed after download before Gemini.
- `ffprobe`/`ffmpeg` support is optional and bounded; neither exists in the
  supported Docker image, so no dependency or unconditional transcoding was
  added. Probe-unavailable direct media fails safely and is visible as one beta
  grouped warning rather than being discarded as zero candidates.

## 2026-07-20 — M23.W2 adaptive media and terminal outcomes

- Active queue social extraction uses a 36 MiB preferred target, a 50 MiB hard
  downloaded-byte/temp-file limit, and at most four combined audio/video source
  formats across 720p, 540p, 480p, and 360p. The historical `*_MB` names are
  binary MiB. Gemini's configured Files limit is 2,000,000,000 bytes and is a
  startup invariant above the worker hard limit.
- Portrait quality is measured by short edge, fixing the old `height<=720`
  selector that excluded 720x1280 and fell through to unrestricted 1080p.
  Exact format IDs are pinned; partials are removed between attempts.
- Compression is rejected for this milestone because the Docker runtime does
  not install ffmpeg and Render Free has 0.1 CPU. Smaller source selection is
  the supported optimization.
- Terminal `media_size_exceeded` retains the existing database code/message
  contract, logs one privacy-safe WARNING, and captures one Sentry warning with
  a stable fingerprint. Intermediate attempts remain Sentry-silent. Build 3
  ignores the server message for this unmapped code, so improved app copy needs
  a future mobile release.

## Completed Decisions

- YouTube remains the stable direct Gemini path: public URL -> video observation -> recipe normalization -> `recipe_cards`.
- Gemini free-tier guardrails stay enabled for development: max 2 calls/job, low retry count, repair/fallback chains disabled unless explicitly enabled.
- Social AI media uses isolated, bounded temporary files that are removed after
  processing. Raw video is never retained, uploaded to Supabase/Drive, or
  exposed as a download.
- yt-dlp is still retained for social metadata/direct stream discovery. Use platform-specific cookies only; Facebook cookies must not be used for TikTok.
- M14G-4R1 TikTok parser: select direct non-HLS MP4 from `formats[]`, preserve selected `http_headers`, and stream with those headers.
- M14G-4TWM0 adds TikWM as a protected debug-only probe path. It does not call Gemini, process jobs, save recipe cards, store video, or expose TikWM video URLs.
- M14G-4SOC1 routes social jobs through one internal `SocialVideoInfo`: YouTube stays native, TikTok uses TikWM, Instagram/Facebook use Cobalt. Cobalt is disabled for TikTok and not used for YouTube.
- W-M14G-4SOC2A keeps TikWM/Cobalt resolution unchanged and stabilizes the Gemini social-video stage with hard worker timeouts, controlled failure updates, and `GEMINI_SOCIAL_UPLOAD_MODE=files_api` by default. Raw video remains in memory only and Gemini uploaded files are deleted best-effort.
- W-M14G-4SOC2B/W-M14R1A uses only DB-safe `media_jobs.status` values (`queued`, `resolving`, `downloading`, `analyzing`, `completed`, `failed`). Detailed social/Gemini stages are represented by progress and events, not status. No DB migration is required.
- Social jobs reject known over-limit durations before streaming, Gemini upload, or Gemini calls. Unknown duration is allowed to continue under the existing size and memory guards.
- W-M14G-4SOC2C makes social analysis stable for development: TikTok/TikWM uses `SOCIAL_ANALYSIS_MODE=text_first` by default and analyzes usable captions/titles with one Gemini text call. Social video Gemini is opt-in through `ENABLE_SOCIAL_VIDEO_GEMINI=true` and wrapped in a whole-call watchdog.
- W-M14R1 reuses the existing worker repo for Render Free Web Service testing. `/process-job` validates the job, schedules FastAPI `BackgroundTasks`, returns quickly with `202`, and lets the background task update Supabase directly. This is a POC, not a durable production queue.
- W-M14R1 keeps TikTok strictly TikWM + text-first Gemini for the Render POC. TikTok stream URLs remain internal resolver values only, are not stored, and are not streamed in this milestone. Known over-limit TikToks fail before Gemini.
- Instagram and Facebook are disabled for the Render POC by default through `ENABLE_RENDER_INSTAGRAM_RESOLVER=false` and `ENABLE_RENDER_FACEBOOK_RESOLVER=false`; they fail with controlled DB statuses until separately validated.
- W-M14R1A aligns the worker with the app database: `create-media-job` manual mode creates `queued` jobs, `/process-job` accepts `queued`, and the worker acquires them by updating to `resolving`. The status path is `queued -> resolving -> analyzing/downloading -> completed/failed`.
- W-M14R1B routes jobs by `media_jobs.platform` before URL host fallback. TikTok always enters the TikWM text-first branch before the legacy YouTube-only unsupported fallback, with no raw video storage and no TikWM CDN URL storage.
- W-M14R1C replaces the failing Gemini SDK `analyze_social_text` helper with a direct synchronous REST call to `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` using `requests.post(timeout=45)`. The TikTok text-first path now uses `_gemini_caption_to_recipe_sync` which manages its own prompt, error handling, and JSON extraction. No video streaming. No raw video storage. No CDN URL storage. Quota guards (`_start_gemini_api_call`) are preserved.
- W-M15A adds TikTok video-to-Gemini fallback behind `ENABLE_SOCIAL_VIDEO_GEMINI=true`. When text-first caption analysis returns `insufficient_recipe_details`, the worker downloads the video from the TikWM internal URL to a temp file under `TEMP_MEDIA_DIR`, uploads it to Gemini Files API via REST multipart upload, calls `generateContent` with `fileData`, and saves the recipe card. Temp files are always deleted in `finally`. Gemini uploaded files are deleted best-effort. No raw video storage. No CDN URL storage. No user-facing downloads. Instagram/Facebook remain disabled. YouTube path unchanged.
- W-M15D0 adds `AI_ANALYSIS_MODE=mock_fixture` for quota-free development testing. TikTok still uses the real TikWM resolver, duration guard, thumbnail upload, job events, and recipe-card save path, but skips Gemini API calls, Gemini quota guards, video API calls, and SDK helpers. Recipe content is loaded from `MOCK_RECIPE_FIXTURE_PATH` (`app/fixtures/mock_recipe_card.json` by default), saved with `ai_provider=mock` and `ai_model=mock-recipe-fixture`, and never stores raw video, CDN URLs, or user-facing downloads.
- W-M15D1 persists resolver metadata in two lanes. Public `media_jobs.source_metadata` stores TikTok title/description, author username, duration, thumbnail presence/upload URL, media flags, and resolved timestamp. Private `media_job_private_metadata` stores the resolver CDN URL and raw resolver metadata by `media_job_id`. Metadata writes are best-effort and emit sanitized events; CDN URLs are never written to events or public source metadata.
- W-M15D2 standardizes the mock-mode resolver adapter matrix without Cobalt: TikTok -> TikWM, YouTube -> yt-dlp metadata, Instagram -> SnapInsta experimental adapter, Facebook -> SnapSave experimental adapter. YouTube can complete mock jobs from metadata even without a direct media format. Instagram/Facebook remain disabled by default and fail controlled unless their Render flags are enabled. Gemini is not called in `AI_ANALYSIS_MODE=mock_fixture`.
- W-M16Q-POC adds an opt-in `WORKER_EXECUTION_MODE=queue` path for Render POC testing. Queue mode starts one FastAPI startup task, polls the oldest `queued` media job, claims it with a simple `queued -> resolving` update, runs a unified `yt-dlp` extractor, persists public source metadata separately from private CDN/raw metadata, saves the existing mock fixture recipe, and always ends jobs as `completed` or `failed` at progress `100`. The existing `/process-job` webhook path remains unchanged when execution mode is not `queue`.
- W-M16Q-POC post-extraction fix reuses a shared mock fixture recipe-card save helper for both webhook mock mode and queue mode. Queue failures now keep stage-specific error codes (`source_metadata_update_failed`, `private_metadata_save_failed`, `mock_fixture_failed`, `recipe_card_insert_failed`, `job_completion_update_failed`) instead of reporting post-extraction DB/API errors as `unified_extractor_failed`.
- W-M16Q-POC-C1 keeps unified yt-dlp extraction cookie-aware but non-leaky. Queue mode tries extraction without cookies first, retries with a platform cookie file only when configured, decodes platform cookie secrets into `/tmp` with `0600` permissions, and never writes cookie content or paths to events, logs, or public metadata. In `AI_ANALYSIS_MODE=mock_fixture`, OpenGraph metadata-only fallback can complete jobs without video bytes; source metadata records `resolution_mode`, `video_bytes_verified`, `output_mode`, and `cookie_mode` booleans/labels only.
- W-M16G-POC adds real AI analysis only behind `AI_ANALYSIS_MODE=gemini_video_poc`. The queue worker sends Gemini only the verified temp video file produced by the extractor, never CDN URLs or source URLs, and emits safe Gemini events without prompts, raw responses, temp paths, cookies, or bytes. Mock fixture mode remains unchanged.
- W-M16G-C1 uses the official `google-genai` Files API as the queue Gemini video input path. The worker uploads only an extractor-verified local temp file, waits until Gemini reports the file ACTIVE, calls the video model with the uploaded file object plus the fixed prompt, validates recipe JSON before insert, and deletes the Gemini file best-effort. Events expose only sanitized stage/model/platform/resolver/size/duration/input-mode metadata, never source URLs, CDN URLs, cookie paths, temp paths, prompts, raw responses, raw bytes, or Gemini file references.
- W-M16G-C2 uses a two-step queue Gemini flow: uploaded video file -> plain recipe brief, then brief text -> strict JSON. The brief step captures safe candidate/parts/finish diagnostics for empty responses, and Instagram unified extraction starts with configured cookies to avoid a noisy unauthenticated first failure.
- W-M16G-C3 treats the successful Gemini video brief as sufficient to complete a queue job. Brief-to-JSON conversion uses text-only Gemini with short retries and a flash fallback for server errors; if conversion still fails, the worker builds a minimal local recipe card from the brief and completes the job.
- M17A keeps queue mode as the primary worker flow but adds `GET /wake` so Render Free can be woken by HTTP without invoking `/process-job`. YouTube queue Gemini uses `YOUTUBE_AI_INPUT_MODE=gemini_url`: oEmbed for public metadata, then Gemini native YouTube URL input for the brief, followed by the same JSON/fallback recipe save path.
- M18 normalizes all queue Gemini recipe cards into the existing `recipe_cards` insert shape immediately before save. The normalizer strips markdown, derives ingredients/steps/grocery from JSON or brief sections, keeps tags out of missing info, and uses prompt version `m18_recipe_video_v1`. Private metadata failures are non-fatal when `ALLOW_PRIVATE_METADATA_FAILURE=true`.
- Queue Gemini token/performance optimization caps the video brief output, JSON output, and brief text passed to JSON conversion with `GEMINI_VIDEO_BRIEF_MAX_OUTPUT_TOKENS`, `GEMINI_RECIPE_JSON_MAX_OUTPUT_TOKENS`, and `GEMINI_RECIPE_BRIEF_MAX_CHARS`. Defaults are conservative and exposed on `/health`.
- M20 uses `m20_gemini31_single_pass_v1` for queue Gemini recipe cards. Default extraction is now a single Gemini structured JSON call using `gemini-3.1-flash-lite` with thinking level `high`; `gemini-2.5-flash` is only a technical fallback model. Validation/repair happens on canonical JSON, not raw briefs, and failed quality gates stop before `recipe_cards` insert.
- M20.5 marks `feature/queue-ytdlp-poc` ready to merge to `main` for production cutover. `WORKER_EXECUTION_MODE=queue` is the primary production flow; `/process-job` remains only for legacy/manual/debug use. `/wake`, `/ping`, and `/health` stay available. YouTube uses Gemini native URL input when `YOUTUBE_AI_INPUT_MODE=gemini_url`; TikTok and Instagram use verified temp files plus Gemini Files API. The old webhook Render service can be suspended only after the main-branch service validates TikTok, Instagram, and YouTube.
- M21.7A adds protected `POST /metadata-preview` for app saved-link previews before AI generation. It accepts `X-Worker-Secret` or `Authorization: Bearer`, fetches lightweight YouTube oEmbed, yt-dlp metadata-only, or OpenGraph metadata, never calls Gemini, never downloads video bytes, and returns only title/description/image thumbnail/author/duration/tags plus over-limit flags.
- M21.9B keeps thumbnails metadata-first. Platform/oEmbed thumbnails are preferred, yt-dlp `thumbnail` is used when valid, `thumbnails[]` is scored by size when needed, avatar/tiny candidates are avoided, and thumbnail events expose only `platform`, `thumbnail_source`, `thumbnail_selected`, and optional `duration_seconds`. Frame sampling is an optional fallback only when no metadata thumbnail exists and `ffmpeg` is available.
- M21.9B.1B added platform_first_v2 thumbnail strategy and no-downgrade protection so full AI jobs do not replace platform thumbnails with sampled frames.
- M21.9B.2 fixed YouTube Shorts thumbnail selection by treating hq1/hq2/hq3 oEmbed thumbnails as low-priority frame variants and validating deterministic YouTube thumbnail candidates first.
- M22B.2D added one repair/fallback path for Gemini structured extraction failures and safer failure categories. Terminal structured failures use `gemini_structured_extraction_failed` with safe metadata only; private metadata save failure remains non-fatal when configured.
- M22D.0B sets worker env source of truth to `env/development.env` and `env/production.env`, with committed example templates only, gitignored real files, masked validation, and temporary Render export files. The Render export maps `MEDIA_WORKER_SHARED_SECRET` to the worker's `WORKER_API_SECRET` key and keeps `MAX_VIDEO_SECONDS` aligned with `MAX_VIDEO_DURATION_SECONDS`.
- M22D.0C narrows clean worker env templates to canonical source keys plus current queue/Gemini path overrides. TikWM, Cobalt, RapidAPI, cookie, and other experimental resolver keys are removed from clean templates unless intentionally re-enabled and documented in the env variable map.
- M22D.0D moves non-secret worker runtime configuration into committed `config/worker*.json` files loaded by `app/runtime_config.py`. Environment variables still override config for Render backward compatibility; env templates/export now contain only secrets and environment identity.
- M22D.3 records the production worker infrastructure split: production Supabase ref `cjlvkagkcfswsycsblfw`, Render env group `recipe-dost-worker-prod`, and Render service `recipe-dost-media-worker-prod`. Production Supabase migrations and SQL smoke tests passed; production Edge Functions, function smoke tests, RevenueCat/Google Play setup, and the Render deploy/health/log smoke pass remain to complete.
- M22D.5A restores production worker extractor parity after env cleanup. Root cause: production JSON did not explicitly carry the non-secret Instagram/unified yt-dlp extractor flags, so production could fail claimed jobs with `unified_extractor_failed` or the legacy `instagram_resolver_disabled` gate. Fix: production queue/Gemini/social extraction flags live in `config/worker.production.json`; Instagram cookie values remain env-only through `INSTAGRAM_COOKIES_B64`, and missing configured cookies fail safely as `instagram_cookies_missing`.
- M22D.5B adds safe runtime config observability and fixes the TikTok TikWM NameError. YouTube production smoke passed. Instagram failures with `instagram_resolver_disabled` indicate the effective runtime config/deployed revision still has the Instagram resolver gate disabled. TikTok failures after `tikwm_resolver_started` were caused by using `resolver_started_at` before assignment. `/health.safe_config` and startup logs now expose non-secret config booleans/model names/cookie-presence flags only; TikWM unexpected resolver errors fail as `tikwm_resolver_exception` with safe exception class metadata.
- M22D.5C is audit-only. `docs/m22d-social-pipeline-audit.md` records that the intended production AI workflow is queue mode: YouTube uses oEmbed plus Gemini URL, while TikTok/Instagram use unified yt-dlp temp-file extraction plus Gemini Files API. Observed `tikwm_resolver_started`, `instagram_resolver_disabled`, and `background_worker_error` indicate the legacy `/process-job` background path, not the queue path. Metadata preview is a separate metadata-only path and does not prove AI job resolver parity.
- M22D.5D makes `/process-job` queue-mode compatible. When `WORKER_EXECUTION_MODE=queue`, the endpoint validates the job and worker secret, records a safe queue-wake event, starts the queue loop, returns `202`, and does not schedule `JobRunner.run()`. Normal production TikTok/Instagram jobs should now reach `queue_job_claimed` and `unified_extractor_started`; TikWM remains legacy/manual/debug, with the M22D.5B timing fix kept only for that path.
- M22D.5E fixes production runtime config packaging/loading. Render env was clean; the deployed Docker image copied `app/` but not `config/`, so production silently fell back to code defaults such as unified extractor disabled, Instagram cookies disabled, and max duration 90. The Docker image now copies `config/`, `runtime_config.py` resolves config from the project root or `WORKER_CONFIG_DIR`, `/health.safe_config.config_source` reports loaded/missing config files, and production fails startup if `worker.production.json` is missing.
- M22D.5F completes production worker smoke testing. Production `/health` confirmed `worker.production.json` loaded, queue mode and `/process-job` queue-wake compatibility active, unified extractor and Instagram/yt-dlp cookies enabled, max duration 120, and raw URL logging disabled. YouTube, Instagram, and TikTok AI recipe generation passed end to end in production. Final architecture: same main branch, separate Render services/env groups; Render env stores secrets only, while `config/worker.production.json` stores non-secret production runtime config. Next milestone is app repo RevenueCat/Google Play billing setup.
- M22E.0B sets the release branch contract: dev worker tracks `develop`, production worker tracks `main`, and dev/prod Render env groups remain separate. `scripts/check_environment.py` is the safe local preflight for branch, config-source, `APP_ENV`, Supabase host-only identity, and selected non-secret runtime config checks. Production checks fail outside `main` unless `ALLOW_NON_MAIN_PROD_DEPLOY=1` is set; development checks prefer `develop`.
- M22E.1R-B fixes the active queue media-job tag gap without changing the legacy generator. The existing `recipe_cards.tags_json jsonb not null default '[]'` contract is confirmed in the app migration/types, so the worker now carries structured Gemini tags through `GeminiRecipeResult` and writes them in its existing insert mapper. The active prompt/schema requires 5-8 useful tags in the same Gemini response; no tag-only AI call, quota/access, RevenueCat, package, media, or database-schema change is introduced. A successful real AI queue job must have 5-8 normalized tags; malformed/unsafe/insufficient tags fail safely before insert rather than storing an incomplete card. Existing cards with empty tags remain compatible.
- M22E.2C uses explicit `sentry-sdk` capture only for the active queue worker. Default/auto framework integrations, logging, breadcrumbs, tracing, profiling, request bodies, source context, and local variables are disabled. Production requires `APP_ENV=production` plus a DSN and Render `main`; the isolated development command requires `APP_ENV=development`, `SENTRY_DEV_TEST=true`, a DSN, and Render `develop`. Legacy/manual `JobRunner`, metadata-preview, and debug routes are excluded. Expected product/provider failures and intermediate retries are not reported; unexpected terminal queue/AI/persistence failures and terminal-failure persistence are reported once through a strict final sanitizer.

## Next Decision To Prove

M22C.3 in the app repo remains the next decision point: RevenueCat + Google Play billing setup must preserve the existing access/quota logic, `premium` entitlement, and Test Store fallback.

## [2026-07-01] Architectural Decision: Graph Indexer Removal

- **Context:** Automated AST graph generators (Graphify/CodeGraph) were causing infinite self-correction loops due to macOS sandbox permission conflicts (`Errno 1`). This was expanding the active context window with 40k+ line JSON data logs, exhausting session limits inside a single task block.
- **Decision:** Completely uninstalled Graphify, wiped `graphify-out/` and `.code-review-graph/`, and removed the MCP server configuration.
- **Consequence:** Project context tracking is now 100% shifted back to lightweight, cost-free, explicit markdown files (`agents.md`, `todo.md`, `decision_log.md`). Codex must rely purely on target file blocks provided directly in user prompts.
