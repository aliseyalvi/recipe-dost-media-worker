# Decision Log

Keep this file terse: only durable architecture decisions, not full task history.

## Completed Decisions

- YouTube remains the stable direct Gemini path: public URL -> video observation -> recipe normalization -> `recipe_cards`.
- Gemini free-tier guardrails stay enabled for development: max 2 calls/job, low retry count, repair/fallback chains disabled unless explicitly enabled.
- Social video uses resolver metadata plus bounded in-memory bytes. Raw video is never written to disk, uploaded to Supabase/Drive, or exposed as a download.
- yt-dlp is still retained for social metadata/direct stream discovery. Use platform-specific cookies only; Facebook cookies must not be used for TikTok.
- M14G-4R1 TikTok parser: select direct non-HLS MP4 from `formats[]`, preserve selected `http_headers`, and stream with those headers.
- M14G-4TWM0 adds TikWM as a protected debug-only probe path. It does not call Gemini, process jobs, save recipe cards, store video, or expose TikWM video URLs.

## Next Decision To Prove

M14G-4TWM1 must prove TikWM from Cloud Run. TikWM is a third-party service, not an official TikTok API. If Cloud Run can resolve and probe TikWM streams without exposing URLs or storing video, full job integration can be considered later.
