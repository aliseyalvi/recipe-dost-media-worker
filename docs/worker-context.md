```bash
cat > docs/worker-context.md <<'EOF'
# Recipe Dost Media Worker Context

## Project

`recipe-dost-media-worker` is a Python Cloud Run worker for Recipe Dost.

The mobile app saves recipe/video links. Supabase creates a `media_jobs` row. The worker processes the job and returns a recipe card.

## Current confirmed working pipeline

### YouTube pipeline — working

Status: completed successfully.

Flow:
1. App creates media job.
2. Supabase Edge Function triggers Cloud Run `/process-job`.
3. Worker accepts job quickly.
4. YouTube is detected.
5. Thumbnail is resolved.
6. Gemini video observation runs.
7. Gemini recipe normalization runs.
8. `recipe_cards` row is inserted.
9. `media_jobs.status = completed`.
10. `media_jobs.result_recipe_card_id` is set.

Known successful job:
- `media_jobs.id = 6bb284df-49b2-47b6-97f0-694d7c8a5a61`
- `result_recipe_card_id = 28877b91-6248-4182-a984-569c5e94bd70`
- `ai_model = gemini-2.5-flash`

## Current active milestone

M14G-4 — Social Video Worker Pipeline.

Goal:
- Instagram/TikTok/Facebook URL
- yt-dlp extracts metadata and direct video stream URL
- requests streams video bytes into bounded memory
- Gemini analyzes video bytes
- source thumbnail is uploaded to Supabase Storage
- recipe card is saved
- no raw video is persisted

## Current blocker

TikTok debug endpoint initially failed on Cloud Run with:

- `yt_dlp_extract_timeout`
- later direct debug returned:
  - `Unexpected response from webpage request`

Local Mac command succeeded with TikTok cookies:

```bash
python -m yt_dlp \
  --dump-single-json \
  --no-download \
  --no-playlist \
  --no-warnings \
  --socket-timeout 15 \
  --retries 1 \
  --fragment-retries 1 \
  --force-ipv4 \
  --cookies "./app/cookies/tiktok.txt" \
  --format "best[ext=mp4][height<=720]/best[height<=720]/best" \
  "https://www.tiktok.com/@m1danial/video/7599246984318373140" > /tmp/tiktok-debug.json