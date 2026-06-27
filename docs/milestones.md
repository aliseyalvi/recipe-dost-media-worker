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
| 14 | M14G-4R2 Social End-to-End Test | Blocked | Full TikTok/Instagram/Facebook job tests |
| 15 | M15 Auth + Account Linking | Later | Email/Google auth |
| 16 | M16 RevenueCat Paywall | Later | Paid AI features after free credits |

## Current Next Action

Deploy and rerun M14G-4R2 after W-M14G-4SOC2C:

- TikTok media job with usable title/caption should complete through `tikwm_resolver_completed`, `social_text_analysis_started`, `social_text_analysis_completed`, `recipe_card_saved`, and `completed`, or fail with `insufficient_recipe_details`.
- Instagram media job should only enter video Gemini when `ENABLE_SOCIAL_VIDEO_GEMINI=true`; enabled video jobs should complete or fail with `gemini_video_timeout`.
- Facebook should complete if Cobalt supports the URL or fail with a Cobalt-specific controlled error.
- YouTube must continue using the unchanged native path.
- No job should remain stuck at `analyzing` progress `50`; Gemini video hangs should become controlled failed jobs with `gemini_video_timeout`.
- Known videos longer than `MAX_VIDEO_DURATION_SECONDS` should fail with `video_duration_exceeded` before stream or Gemini events.
