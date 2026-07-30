# Recipe Dost Media Worker Context

Compact repo context for future Codex turns.

## Product

Recipe Dost turns public/authorized recipe video links into structured recipe cards.

The worker may extract safe metadata, create temporary server-side media files for analysis, call Gemini, save recipe cards, and upload source thumbnails. It must not provide user-facing downloads, back up deleted videos, store raw videos permanently, commit cookies/secrets, or log full source URLs, stream URLs, cookies, Gemini prompts/raw responses, ingredients, steps, temp paths, Gemini file references, or media bytes.

## Current Architecture

- Render web service: `recipe-dost-media-worker`
- Development branch/service: `develop`
- Production branch/service: `main`
- Primary flow: `WORKER_EXECUTION_MODE=queue`
- Wake/probes: `GET /wake`, `GET /ping`, `GET /health`
- Legacy/manual/debug: `POST /process-job`
- DB writes: `media_jobs`, `media_job_events`, `recipe_cards`, best-effort `recipe_items.thumbnail_url`
- AI: Gemini 3.1 Flash-Lite single-pass structured extraction

## Working Paths

- YouTube: queue job -> oEmbed metadata -> Gemini native URL input when `YOUTUBE_AI_INPUT_MODE=gemini_url` -> structured recipe card.
- Development TikTok/Instagram AI: server-authorized queue job -> registered
  external provider adapter -> deterministic candidate selection -> guarded
  HTTPS download -> ffprobe -> optional A/V mux -> Gemini Files API -> unchanged
  recipe-card save -> temp files deleted.
- Metadata preview remains a separate public/no-cookie, download-free path and
  never reserves AI quota or calls an external media provider.
- External selection prefers H.264/AVC MP4 at or below 720p and 36 MiB. The
  streaming/temp-file hard limit is 50 MiB and the source-video duration limit
  defaults to 180 seconds through `MAX_SOURCE_VIDEO_DURATION_SECONDS`; `*_MB`
  names use binary MiB.
- Production is still external-disabled. Its legacy extractor is retained only
  as rollback code, while cookie use requires the separate fallback flag and is
  false by default.
- Mock fixture mode: `AI_ANALYSIS_MODE=mock_fixture` skips Gemini and saves `app/fixtures/mock_recipe_card.json` through the same recipe-card save path.

## Production Defaults

- `WORKER_EXECUTION_MODE=queue`
- `AI_ANALYSIS_MODE=gemini_video_poc`
- `GEMINI_EXTRACTION_MODE=single_pass_structured`
- `GEMINI_PRIMARY_MODEL=gemini-3.1-flash-lite`
- `YOUTUBE_AI_INPUT_MODE=gemini_url`
- `YOUTUBE_DIRECT_GEMINI_ENABLED=true`
- `MEDIA_RESOLVER_MODE=legacy`
- `EXTERNAL_MEDIA_RESOLVER_AUDIENCE=disabled`
- `INSTAGRAM_RESOLVER_CHAIN=[]`, `TIKTOK_RESOLVER_CHAIN=[]`
- `COOKIE_MEDIA_FALLBACK_ENABLED=false`
- `ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR=true`
- `MEDIA_EXTRACT_OUTPUT_MODE=temp_file`
- `PREFERRED_VIDEO_SIZE_MB=36`
- `MAX_VIDEO_SIZE_MB=50`
- `MEDIA_ADAPTIVE_MAX_ATTEMPTS=4`
- `MIN_VIDEO_HEIGHT=360`, `MAX_VIDEO_HEIGHT=720`
- `LOCAL_BRIEF_PARSER_ENABLED=false`
- `STORE_RAW_MEDIA=false`

Development uses `MEDIA_RESOLVER_MODE=external`, audience
`all_authorized_ai`, Instagram adapter `rapidapi_instagram_reels13`, and TikTok
adapter `akng_tiktok`. Provider credentials stay only in Render secrets. A
production canary must first switch to a selected-test-user audience; do not
copy development credentials to production.

Runtime config selection uses Render's existing branch metadata when `APP_ENV`
is absent: `develop` loads `config/worker.development.json`, and `main` loads
`config/worker.production.json`. Conflicts, missing selected files, malformed
JSON, unknown keys/types, and unknown adapters fail closed. Blank environment
values do not erase file values.

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
