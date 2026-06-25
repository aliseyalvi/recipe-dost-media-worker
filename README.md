# Recipe Dost Media Worker

Minimal FastAPI worker skeleton for M14G-2. It receives a media job ID,
reads and updates the job with the Supabase service role, and records job
events. It does not fetch, download, or analyze media.

## Local setup

Python 3.12 is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set real local values in `.env`. The service role key is server-only and must
never be shipped to the Expo app or committed to source control.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_ENV` | `development` | Runtime environment label |
| `WORKER_MODE` | `skeleton` | Worker implementation mode |
| `WORKER_API_SECRET` | required | Shared secret required by `/process-job` |
| `SUPABASE_URL` | required | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | required | Server-only Supabase service role key |
| `MAX_VIDEO_DURATION_SECONDS` | `90` | Reserved limit for a future media pipeline |
| `MAX_VIDEO_SIZE_MB` | `60` | Reserved limit for a future media pipeline |

## Run locally

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Health check:

```bash
curl http://localhost:8080/health
```

Process a job:

```bash
curl -X POST http://localhost:8080/process-job \
  -H "Content-Type: application/json" \
  -H "X-Worker-Secret: replace-with-your-worker-secret" \
  -d '{"media_job_id":"00000000-0000-0000-0000-000000000000"}'
```

The skeleton advances a queued or failed job through:

1. `resolving` at 10%
2. `validating` at 30%
3. `needs_caption` at 100%

Each stage writes a `media_job_events` row. The terminal skeleton state uses
`worker_skeleton_no_media_provider` and leaves `result_recipe_card_id`
untouched.

Completed, active, and `needs_caption` jobs are returned as-is so repeated
requests do not restart work.

## Deploy to Cloud Run

Build and deploy from this directory:

```bash
gcloud run deploy recipe-dost-media-worker \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars APP_ENV=production,WORKER_MODE=skeleton,MAX_VIDEO_DURATION_SECONDS=90,MAX_VIDEO_SIZE_MB=60 \
  --set-secrets WORKER_API_SECRET=recipe-dost-worker-api-secret:latest,SUPABASE_URL=recipe-dost-supabase-url:latest,SUPABASE_SERVICE_ROLE_KEY=recipe-dost-supabase-service-role-key:latest
```

`--allow-unauthenticated` permits the HTTP request to reach the container;
`POST /process-job` still requires the `X-Worker-Secret` application header.
For stricter infrastructure-level access, deploy without that flag and use
Cloud Run IAM in addition to the worker secret.

Create the referenced Google Secret Manager secrets before deploying, or
replace the secret names with those used by your project.

## Deferred work

Downloader providers, yt-dlp, Apify, RapidAPI, FFmpeg, OpenCV, Gemini Files
API, thumbnail extraction, and recipe card generation are intentionally
deferred.
