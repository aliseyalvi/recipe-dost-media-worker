# Recipe Dost Media Worker Context

Compact repo context for future Codex turns.

## Product

Recipe Dost turns public/authorized recipe video links into structured recipe cards.

The worker may extract safe metadata, create temporary server-side media files for analysis, call Gemini, save recipe cards, and upload source thumbnails. It must not provide user-facing downloads, back up deleted videos, store raw videos permanently, commit cookies/secrets, or log full source URLs, stream URLs, cookies, Gemini prompts/raw responses, ingredients, steps, temp paths, Gemini file references, or media bytes.

## Current Architecture

- Render web service: `recipe-dost-media-worker`
- Branch to merge: `feature/queue-ytdlp-poc`
- Production after merge: run the worker from `main`
- Primary flow: `WORKER_EXECUTION_MODE=queue`
- Wake/probes: `GET /wake`, `GET /ping`, `GET /health`
- Legacy/manual/debug: `POST /process-job`
- DB writes: `media_jobs`, `media_job_events`, `recipe_cards`, best-effort `recipe_items.thumbnail_url`
- AI: Gemini 3.1 Flash-Lite single-pass structured extraction

## Working Paths

- YouTube: queue job -> oEmbed metadata -> Gemini native URL input when `YOUTUBE_AI_INPUT_MODE=gemini_url` -> structured recipe card.
- TikTok/Instagram: queue job -> unified extractor -> verified temp file -> Gemini Files API -> structured recipe card -> temp file deleted.
- Mock fixture mode: `AI_ANALYSIS_MODE=mock_fixture` skips Gemini and saves `app/fixtures/mock_recipe_card.json` through the same recipe-card save path.

## Production Defaults

- `WORKER_EXECUTION_MODE=queue`
- `AI_ANALYSIS_MODE=gemini_video_poc`
- `GEMINI_EXTRACTION_MODE=single_pass_structured`
- `GEMINI_PRIMARY_MODEL=gemini-3.1-flash-lite`
- `YOUTUBE_AI_INPUT_MODE=gemini_url`
- `ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR=true`
- `MEDIA_EXTRACT_OUTPUT_MODE=temp_file`
- `LOCAL_BRIEF_PARSER_ENABLED=false`
- `STORE_RAW_MEDIA=false`

Suspend the old webhook Render service only after the main-branch service validates TikTok, Instagram, and YouTube end to end.

## Guardrails

- Preserve Gemini quota guards before every Gemini call.
- Use only DB-safe `media_jobs.status` values: `queued`, `resolving`, `downloading`, `analyzing`, `completed`, `failed`.
- Detailed stages belong in `media_job_events`, not new status values.
- Platform routing uses `media_jobs.platform` first, then source URL host fallback.
- Known over-limit durations fail before stream/Gemini work.
- Temporary resolver/CDN URLs are private-only and must never be written to public metadata, events, or logs.
- No job should remain stuck at `analyzing`; Gemini/file hangs should become controlled failed jobs.

## Validation

Run before finishing:

```bash
python -m compileall app
```

On this Mac, `python3 -m compileall app` has passed when `python` is unavailable.
