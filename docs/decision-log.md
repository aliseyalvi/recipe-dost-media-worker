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
- W-M14G-4SOC2B uses only DB-safe `media_jobs.status` values (`pending`, `resolving`, `downloading`, `analyzing`, `completed`, `failed`). Detailed social/Gemini stages are represented by progress and events, not status. No DB migration is required.
- Social jobs reject known over-limit durations before streaming, Gemini upload, or Gemini calls. Unknown duration is allowed to continue under the existing size and memory guards.
- W-M14G-4SOC2C makes social analysis stable for development: TikTok/TikWM uses `SOCIAL_ANALYSIS_MODE=text_first` by default and analyzes usable captions/titles with one Gemini text call. Social video Gemini is opt-in through `ENABLE_SOCIAL_VIDEO_GEMINI=true` and wrapped in a whole-call watchdog.

## Next Decision To Prove

M14G-4R2 remains blocked until deployed retests prove TikTok text-first jobs complete or fail with `insufficient_recipe_details`, and enabled Instagram/Facebook video jobs complete or fail with `gemini_video_timeout`. TikWM and Cobalt stream URLs are temporary internal values and must never be stored or returned.
