# Decision Log

## M14G-3 — YouTube Gemini pipeline completed

Decision:
- Keep YouTube direct Gemini path.
- Use two-stage Gemini:
  1. video observation
  2. recipe normalization
- Store recipe card in Supabase.
- Store thumbnail URL when available.

Reason:
- This path has been verified end-to-end.

## M14G-3B — Quota guard

Decision:
- Add Gemini per-job and daily dev limits.
- Use Gemini 2.5 Flash for development.
- Disable repair calls and fallback chains by default.

Reason:
- Free-tier Gemini quota was exhausted quickly by retries/fallbacks.

## M14G-4 — Social video resolver

Decision:
- Use yt-dlp first because it is free.
- Use platform-specific cookies.
- Facebook cookies must not be used for TikTok.
- TikTok requires TikTok cookies if anonymous extraction fails.
- Add debug endpoint before full app processing.

Reason:
- Public browser access does not guarantee Cloud Run + yt-dlp access.

## M14G-4 streaming strategy

Decision:
- Do not store raw videos.
- Do not upload raw videos to Supabase.
- Do not use Google Drive.
- Use bounded memory buffer for short videos.
- Use source/yt-dlp thumbnail and upload only thumbnail to Supabase.

Reason:
- Free-tier friendly and avoids turning app into a video downloader.
