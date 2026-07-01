# Recipe Dost Worker Context for OpenCode

Repo:
recipe-dost-media-worker

Current milestone:
M20.5 — worker repo cleanup before production cutover.

Current branch:
feature/queue-ytdlp-poc

Cutover intent:
- Merge `feature/queue-ytdlp-poc` into `main`.
- Deploy the production Render worker from `main` after merge.
- Suspend the old webhook Render service only after main validates TikTok, Instagram, and YouTube.

Current architecture:
- Python FastAPI worker.
- Primary production mode is `WORKER_EXECUTION_MODE=queue`.
- FastAPI startup starts one queue loop when queue mode is enabled.
- `GET /wake` safely starts/keeps the queue loop active for Render Free.
- `GET /ping` and `GET /health` remain probe endpoints.
- `POST /process-job` is legacy/manual/debug only.
- Queue loop claims the oldest `queued` job and processes one job at a time.

Current working paths:
- YouTube uses Gemini native URL input when `YOUTUBE_AI_INPUT_MODE=gemini_url`; it must not use yt-dlp in that mode.
- TikTok and Instagram use unified extraction to create a verified temp file, then Gemini Files API.
- Gemini 3.1 Flash-Lite single-pass structured extraction is the default path.
- Local deep parser fallback is disabled by default.

Required production settings:
- `WORKER_EXECUTION_MODE=queue`
- `AI_ANALYSIS_MODE=gemini_video_poc`
- `GEMINI_EXTRACTION_MODE=single_pass_structured`
- `GEMINI_PRIMARY_MODEL=gemini-3.1-flash-lite`
- `YOUTUBE_AI_INPUT_MODE=gemini_url`
- `ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR=true`
- `MEDIA_EXTRACT_OUTPUT_MODE=temp_file`
- `LOCAL_BRIEF_PARSER_ENABLED=false`
- `STORE_RAW_MEDIA=false`

Status lifecycle:
queued -> resolving -> downloading/analyzing -> completed/failed

Allowed `media_jobs.status` values:
- queued
- resolving
- downloading
- analyzing
- completed
- failed

Do not expose or store:
- source URLs in logs/events
- CDN or stream URLs in public metadata/events
- cookies or cookie paths
- temp paths
- Gemini prompts or raw responses
- Gemini file references
- video bytes
- ingredients or steps in logs/events

Validation:

```bash
python -m compileall app
```
