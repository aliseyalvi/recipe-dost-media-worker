# Decision Log

Keep this file terse: only durable architecture decisions, not full task history.

## Completed Decisions

- YouTube remains the stable direct Gemini path: public URL -> video observation -> recipe normalization -> `recipe_cards`.
- Gemini free-tier guardrails stay enabled for development: max 2 calls/job, low retry count, repair/fallback chains disabled unless explicitly enabled.
- Social video uses resolver metadata plus bounded in-memory bytes. Raw video is never written to disk, uploaded to Supabase/Drive, or exposed as a download.
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

## Next Decision To Prove

M14G-4R2 remains blocked until deployed Render retests prove `/process-job` returns quickly while background jobs reach `completed` or `failed`, TikTok text-first jobs produce `tikwm_resolver_started` and complete or fail cleanly, and Instagram/Facebook can be enabled only after resolver validation. TikWM and Cobalt stream URLs are temporary internal values and must never be stored or returned.
