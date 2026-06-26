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

## Next Decision To Prove

M14G-4R2 must prove full TikTok, Instagram, and Facebook jobs end to end under quota guards. TikWM and Cobalt stream URLs are temporary internal values and must never be stored or returned.
