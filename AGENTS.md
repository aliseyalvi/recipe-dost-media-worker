# Recipe Dost Media Worker — OpenCode Instructions

You are helping build `recipe-dost-media-worker`, a Python FastAPI media worker for Recipe Dost. Current platform target is **Render Free Web Service POC**.

## Product scope

Recipe Dost turns public/authorized recipe video links into structured recipe cards.

The worker may:
- Process public/authorized YouTube, TikTok, Instagram, and Facebook recipe links.
- Extract metadata and temporary in-memory video bytes server-side.
- Send content to Gemini for recipe analysis.
- Save recipe cards and thumbnail URLs to Supabase.

The worker must NOT:
- Provide user-facing video downloads.
- Back up deleted videos.
- Store raw videos permanently.
- Store TikWM/Cobalt/Snapinsta/Snapsave/CDN download URLs.
- Store cookies, secrets, or raw media in git.
- Log full source URLs, stream URLs, cookies, Gemini prompts, Gemini raw responses, ingredients, or steps.

## Current architecture

- Render Free Web Service: `recipe-dost-media-worker`
- Supabase Edge Function creates media_jobs and triggers `/process-job`
- `/process-job` validates, schedules FastAPI BackgroundTasks, returns `202`
- Background task creates fresh dependencies, runs the job, updates Supabase
- Worker updates:
  - `media_jobs`
  - `media_job_events`
  - `recipe_cards`
  - `recipe_items.thumbnail_url` when available
- AI provider: Gemini
- TikTok: TikWM resolver → text-first Gemini (video Gemini disabled for now)
- YouTube: native Gemini URL path (unchanged)
- Instagram/Facebook: disabled for Render POC (`ENABLE_RENDER_INSTAGRAM_RESOLVER=false`, `ENABLE_RENDER_FACEBOOK_RESOLVER=false`)

## Development rules

- Use Python typing where practical.
- Prefer minimal targeted patches. Avoid broad rewrites.
- Do not edit the app repo from this worker repo.
- Do not change Supabase DB schema unless explicitly requested.
- Do not add user-facing download features.
- Before every Gemini call, preserve quota guards.
- For free-tier testing:
  - Max 2 Gemini calls per job
  - No repair call by default
  - No fallback chains by default
  - Render concurrency 1
  - No raw video storage
- TikTok text-first for now. Full video Gemini is disabled for now.
- Use only DB-safe `media_jobs.status` values: `queued`, `resolving`, `downloading`, `analyzing`, `completed`, `failed`.
- Detailed stages must go to `media_job_events`.
- Platform routing uses `media_jobs.platform` first, then source URL host fallback.
- TikTok emits `tikwm_resolver_started`, not the YouTube-only unsupported message.
- Known over-limit TikTok durations are rejected before Gemini.
- Development social analysis defaults: `SOCIAL_ANALYSIS_MODE=text_first`, `ENABLE_TIKTOK_TEXT_FALLBACK=true`, `ENABLE_SOCIAL_VIDEO_GEMINI=false`.
- Render POC defaults: `ENABLE_RENDER_INSTAGRAM_RESOLVER=false`, `ENABLE_RENDER_FACEBOOK_RESOLVER=false`, no raw video storage, no CDN URL storage, no user-facing downloads.

## Required context files

Before starting a task, read:
1. `docs/opencode-worker-context.md`
2. `docs/worker-context.md`
3. `docs/milestones.md`
4. `docs/decision-log.md`
5. `docs/commands-log.md`
6. `docs/social-video-debug.md`

For social resolver work, inspect only:
- `app/config.py`
- `app/main.py`
- `app/job_runner.py`
- `app/supabase_client.py`
- `requirements.txt`
- `Dockerfile`
- `README.md`

## Validation

Run before finishing:
```bash
python -m compileall app
```

On this Mac, `python3 -m compileall app` has passed.

## Current Milestones

| Milestone | Status | Description |
|---|---|---|
| W-M14R1A Accept queued jobs | Done | Render async worker accepts `queued` jobs from create-media-job manual mode |
| W-M14R1B TikTok routing to TikWM | Done | Queued TikTok jobs route to TikWM text-first before unsupported fallback |
| W-M14R1C Replace Gemini text helper with sync REST call | Done | Replace failing TikTok text-first Gemini helper with direct synchronous REST call to Gemini generateContent |
| W-M14R2 Deploy worker to Render | Next | Deploy and retest full end-to-end on Render Free |

## After Every Task

Update:
1. `docs/decision-log.md`
2. `docs/commands-log.md`
3. `docs/milestones.md` if milestone status changed
4. `docs/social-video-debug.md`

## After Every Milestone

Update:
- `docs/milestones.md`
- `docs/decision-log.md`
- `docs/social-video-debug.md`
- `docs/commands-log.md`

## Secrets

Never commit `.env`, `cloudrun.env.yaml`, cookies, service-role keys, Gemini keys, or worker secrets.