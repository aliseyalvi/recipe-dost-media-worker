# Recipe Dost Media Worker — Codex Instructions

You are helping build `recipe-dost-media-worker`, a Google Cloud Run Python worker for Recipe Dost.

## Product scope

Recipe Dost turns public/authorized recipe video links into structured recipe cards.

The worker may:
- Process public/authorized YouTube, TikTok, Instagram, and Facebook recipe links.
- Extract metadata and temporary video bytes server-side.
- Send video content to Gemini for recipe analysis.
- Save recipe cards and thumbnail URLs to Supabase.

The worker must NOT:
- Provide user-facing video downloads.
- Back up deleted videos.
- Store raw videos permanently.
- Store cookies, secrets, or raw media in git.
- Log full source URLs, stream URLs, cookies, Gemini prompts, Gemini raw responses, ingredients, or steps.

## Current architecture

- Cloud Run service: `recipe-dost-media-worker`
- Supabase Edge Function creates media_jobs and triggers `/process-job`
- Worker updates:
  - `media_jobs`
  - `media_job_events`
  - `recipe_cards`
  - `recipe_items.thumbnail_url` when available
- AI provider: Gemini
- Current working path:
  - YouTube URL
  - Gemini video observation
  - Gemini recipe normalization
  - recipe card saved
  - media job completed

## Development rules

- Use Python typing where practical.
- Prefer small, targeted patches.
- Keep controlled failures as DB status updates, not unhandled 500s.
- Before every Gemini call, preserve quota guards.
- For free-tier testing:
  - Max 2 Gemini calls per job
  - No repair call by default
  - No fallback chains by default
  - Cloud Run concurrency 1
  - No raw video storage

## Required context files

Before starting a task, read:
1. `docs/worker-context.md`
2. `docs/milestones.md`
3. `docs/decision-log.md`
4. `docs/commands-log.md`
5. `docs/social-video-debug.md`

After finishing a task, update:
1. `docs/decision-log.md`
2. `docs/commands-log.md`
3. `docs/milestones.md` if milestone status changed

## Tooling preference

Use CodeGraph/Graphify first if available.
If not available, inspect only the smallest relevant files.

For social resolver work, inspect only:
- `app/config.py`
- `app/main.py`
- `app/job_runner.py`
- `app/social/yt_dlp_resolver.py`
- `app/social/stream_download.py`
- `app/supabase_client.py`
- `requirements.txt`
- `Dockerfile`
- `README.md`

## Validation commands

Run before finishing:
```bash
python -m compileall app