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
| 9 | M14G-4TWM1 TikWM Cloud Run Probe | Next | Deploy and verify `/debug/tikwm` from Cloud Run |
| 10 | M14G-4R2 Social End-to-End Test | Later | Full TikTok/Instagram/Facebook job tests |
| 11 | M15 Auth + Account Linking | Later | Email/Google auth |
| 12 | M16 RevenueCat Paywall | Later | Paid AI features after free credits |

## Current Next Action

Run M14G-4TWM1:

- Deploy with TikWM config defaults or env overrides.
- Call protected `POST /debug/tikwm` with `test_stream=false`, then `true`.
- Confirm metadata/probe works from Cloud Run.
- Do not integrate TikWM into media jobs until debug passes.
