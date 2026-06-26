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
| 11 | M14G-4R2 Social End-to-End Test | Next | Full TikTok/Instagram/Facebook job tests |
| 12 | M15 Auth + Account Linking | Later | Email/Google auth |
| 13 | M16 RevenueCat Paywall | Later | Paid AI features after free credits |

## Current Next Action

Run M14G-4R2:

- TikTok media job should complete through `tikwm_resolver_completed`, `social_stream_completed`, Gemini observation/normalization, `recipe_card_saved`, and `completed`.
- Instagram media job should complete through `cobalt_resolver_completed`, `social_stream_completed`, Gemini observation/normalization, `recipe_card_saved`, and `completed`.
- Facebook should complete if Cobalt supports the URL or fail with a Cobalt-specific controlled error.
- YouTube must continue using the unchanged native path.
