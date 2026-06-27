# Recipe Dost Worker Context for OpenCode

Repo:
recipe-dost-media-worker

Current task:
W-M14R1C — Replace failing TikTok text-first Gemini helper with a simple synchronous Gemini REST call.

Current architecture:
- Python FastAPI worker.
- /process-job returns HTTP 202 quickly.
- FastAPI BackgroundTasks processes the job.
- App creates media_jobs in Supabase.
- Supabase create-media-job is currently in MEDIA_PROCESSOR_MODE=manual, so Cloud Run is bypassed.
- Local worker is manually triggered with curl.
- Later this same worker will be deployed to Render.

Current working parts:
- media_jobs initial status is queued.
- Worker now accepts queued jobs.
- TikTok routes to TikWM.
- TikWM resolver works.
- Thumbnail upload works.
- Worker reaches social_text_analysis_started.

Current failing part:
- After social_text_analysis_started, worker fails with:
  worker_error
  background_worker_error
  RuntimeError
  WorkerProcessingError
- No gemini_api_call_started event appears.
- So failure is before or at the current Gemini helper call.

Required fix:
- Do not broadly refactor.
- Do not touch app repo.
- Do not change DB schema.
- Do not re-enable video Gemini.
- Do not store raw video.
- Do not store CDN URLs.
- Replace TikTok text-first Gemini helper call with a direct synchronous REST call to Gemini generateContent using requests.post(timeout=45).

Status lifecycle:
queued → resolving → analyzing → completed/failed

Allowed media_jobs.status:
- queued
- resolving
- downloading
- analyzing
- completed
- failed

Important files:
- app/main.py
- app/job_runner.py
- app/config.py
- app/supabase_client.py
- requirements.txt
- docs/milestones.md
- docs/decision-log.md
- docs/social-video-debug.md
- docs/commands-log.md

Validation:
python -m compileall app

Manual test:
curl -s -X POST "http://127.0.0.1:8080/process-job" \
  -H "Content-Type: application/json" \
  -H "X-Worker-Secret: $WORKER_API_SECRET" \
  -d '{"job_id":"PASTE_QUEUED_JOB_ID"}' | jq

Success:
- completed 100 with result_recipe_card_id
or
- failed 100 with controlled error like insufficient_recipe_details or gemini_api_error

Not acceptable:
- generic worker_error/background_worker_error without root cause
- stuck analyzing
