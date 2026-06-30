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
| 30 | M14G-4R2 Social End-to-End Test | Blocked | Full TikTok/Instagram/Facebook job tests |
| 31 | M15 Auth + Account Linking | Later | Email/Google auth |
| 32 | M16 RevenueCat Paywall | Later | Paid AI features after free credits |

## Current Next Action

Deploy and rerun M14G-4R2 after W-M14R1:

- Render Free Web Service should return `202` from `/process-job` quickly while the FastAPI background task updates Supabase to `completed` or `failed`.
- create-media-job manual mode creates `queued` jobs; `/process-job` accepts `queued` and the worker acquires them as `resolving`.
- Queued TikTok jobs must route by `media_jobs.platform` to TikWM text-first, not the old YouTube-only unsupported branch.
- TikTok media job with usable title/caption should complete through `tikwm_resolver_completed`, thumbnail upload, `social_text_analysis_started`, `social_text_analysis_completed`, `recipe_card_saved`, and `completed`, or fail with `social_caption_missing`, `insufficient_recipe_details`, `gemini_text_timeout`, or `video_duration_exceeded`.
- Instagram should fail with `instagram_resolver_disabled` until `ENABLE_RENDER_INSTAGRAM_RESOLVER=true` after validation.
- Facebook should fail with `facebook_resolver_disabled` until `ENABLE_RENDER_FACEBOOK_RESOLVER=true` after validation.
- YouTube must continue using the unchanged native path.
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
