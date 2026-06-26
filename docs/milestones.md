# Recipe Dost Media Worker Milestones

| Order | Milestone | Status | Goal |
|---:|---|---|---|
| 1 | M14G-1 Media Job Foundation | Done | Supabase media_jobs + media_job_events |
| 2 | M14G-2 Cloud Worker Skeleton | Done | Cloud Run worker connected |
| 3 | M14G-3 YouTube Gemini Pipeline | Done | YouTube URL → Gemini → recipe card |
| 4 | M14G-3B Quota Guard | Done | Prevent Gemini free-tier burn |
| 5 | M14G-3C Stable JSON Pipeline | Done | Video observation → recipe normalization |
| 6 | M14G-4R0 Worker Repo Context Setup | Current | Add repo context docs for Codex |
| 7 | M14G-4R1 TikTok Format Parser Fix | Next | Parse yt-dlp formats + stream with headers |
| 8 | M14G-4R2 Social Video End-to-End Test | Later | TikTok, Instagram, Facebook tests |
| 9 | M15 Auth + Account Linking | Later | Email/Google auth |
| 10 | M16 RevenueCat Paywall | Later | Paid AI features after free credits |

## Current next action

Implement M14G-4R1:
- Use local successful yt-dlp metadata structure.
- Extract direct stream URL from `formats[]`.
- Use selected `http_headers`.
- Stream with `requests.get(stream=True)` into bounded `io.BytesIO`.
