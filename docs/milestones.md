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
| 21 | M14G-4R2 Social End-to-End Test | Blocked | Full TikTok/Instagram/Facebook job tests |
| 22 | M15 Auth + Account Linking | Later | Email/Google auth |
| 23 | M16 RevenueCat Paywall | Later | Paid AI features after free credits |

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
