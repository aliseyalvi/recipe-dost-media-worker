# Commands Log

Keep only commands that future turns may need.

## Validation

```bash
python -m compileall app
# local zsh: python may be missing

python3 -m compileall app
# passed

env PYTHONPYCACHEPREFIX=/private/tmp/recipe-dost-pycache .venv/bin/python -m compileall app
# passed
```

## Local Server

```bash
uvicorn app.main:app --reload --port 8080
curl http://127.0.0.1:8080/health
```

## TikTok yt-dlp Repro

```bash
python -m yt_dlp --dump-single-json --no-download --no-playlist \
  --no-warnings --socket-timeout 15 --retries 1 --fragment-retries 1 \
  --force-ipv4 --cookies "./app/cookies/tiktok.txt" \
  --format "best[ext=mp4][height<=720]/best[height<=720]/best" \
  "PASTE_TIKTOK_URL_HERE"
```

## Docs Compaction

```bash
wc -l AGENTS.md README.md docs/worker-context.md docs/milestones.md docs/decision-log.md docs/commands-log.md docs/social-video-debug.md
# reduced from 640 total lines to about 380 before this log entry

python -m compileall app
# zsh: command not found: python

python3 -m compileall app
# passed

env PYTHONPYCACHEPREFIX=/private/tmp/recipe-dost-pycache .venv/bin/python -m compileall app
# passed
```

## M14G-4TWM0 Validation

```bash
python -m compileall app
# zsh: command not found: python

python3 -m compileall app
# passed

env PYTHONPYCACHEPREFIX=/private/tmp/recipe-dost-pycache .venv/bin/python -m compileall app
# passed
```

## M14G-4SOC1 Validation

```bash
python -m compileall app
# local zsh: python may be missing

python3 -m compileall app
# passed
```

## W-M14G-4SOC2A Validation

```bash
python -m compileall app
# zsh: command not found: python

python3 -m compileall app
# passed
```

## W-M14G-4SOC2B Validation

```bash
python -m compileall app
# zsh: command not found: python

python3 -m compileall app
# passed

grep -R "normalizing_recipe\|saving_result\|analyzing_video" -n app
# no matches
```

## W-M14G-4SOC2C Validation

```bash
python -m compileall app
# zsh: command not found: python

python3 -m compileall app
# passed

grep -R "normalizing_recipe|saving_result|analyzing_video" -n app
# no matches
```

## W-M14R1 Validation

```bash
python -m compileall app
# zsh: command not found: python

python3 -m compileall app
# passed

env PYTHONPYCACHEPREFIX=/private/tmp/recipe-dost-pycache .venv/bin/python -m compileall app
# passed

env PYTHONPYCACHEPREFIX=/private/tmp/recipe-dost-pycache .venv/bin/python - <<'PY'
from app.schemas import ProcessJobRequest, ProcessJobResponse
from uuid import UUID
jid = '00000000-0000-0000-0000-000000000000'
assert ProcessJobRequest.model_validate({'media_job_id': jid}).media_job_id == UUID(jid)
assert ProcessJobRequest.model_validate({'job_id': jid}).media_job_id == UUID(jid)
print(ProcessJobResponse(accepted=True, job_id=UUID(jid), message='ok').model_dump(mode='json', exclude_none=True))
PY
# passed

# legacy upload/temp-url marker grep requested for app and docs
# passed after removing old self-matching command-log text
```

## W-M14R1A Validation

```bash
python -m compileall app
# zsh: command not found: python

python3 -m compileall app
# passed

env PYTHONPYCACHEPREFIX=/private/tmp/recipe-dost-pycache .venv/bin/python -m compileall app
# passed

# requested initial-status wording grep over app and docs
# passed with no matches
```

## W-M14R1B Validation

```bash
python -m compileall app
# zsh: command not found: python

env PYTHONPYCACHEPREFIX=/private/tmp/recipe-dost-pycache python3 -m compileall app
# passed

env PYTHONPYCACHEPREFIX=/private/tmp/recipe-dost-pycache .venv/bin/python -m compileall app
# passed

# requested YouTube-only fallback grep over app
# matched the intended fallback in app/job_runner.py; exact grep also reported stale pycache
```

## W-M14R1C Validation

```bash
python3 -m compileall app
# passed

grep -R "temporary_download_url" -n app docs || true
# no matches
```

## W-M15A Validation

```bash
python3 -m compileall app
# passed

grep -R "temporary_download_url" -n app docs || true
# no matches (only the command itself in commands-log)

grep -Rn "cdn_download_url.*insert\|cdn_download_url.*update" app docs || true
# no matches
```

## W-M15D0 Validation

```bash
python3 -m compileall app
# passed

grep -R "temporary_download_url" -n app docs || true
# matched only historical commands-log entries

grep -R "cdn_download_url.*insert\|cdn_download_url.*update" -n app docs || true
# matched only historical commands-log entries
```

## W-M15D1 Validation

```bash
python3 -m compileall app
# passed

grep -R "cdn_url.*source_metadata\|source_metadata.*cdn_url\|temporary_download_url" -n app docs || true
# matched only historical commands-log entries
```

## W-M15D2 Validation

```bash
python3 -m compileall app
# passed

grep -R "cdn_url.*source_metadata|source_metadata.*cdn_url|temporary_download_url" -n app docs || true
# matched only this historical commands-log entry
```

## W-M16Q-POC Validation

```bash
python3 -m compileall app
# passed

env PYTHONPYCACHEPREFIX=/private/tmp/recipe-dost-pycache .venv/bin/python -m compileall app
# passed

rg -n "cdn_download_url|cdn_url|source_url|stream_url" app/queue_worker.py app/social/stream_download.py
# passed: queue events/public metadata use only CDN booleans; private metadata stores cdn_url

WORKER_API_SECRET=test SUPABASE_URL=https://example.supabase.co SUPABASE_SERVICE_ROLE_KEY=test WORKER_EXECUTION_MODE=queue ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR=true MEDIA_EXTRACT_OUTPUT_MODE=temp_file .venv/bin/python - <<'PY'
from app.config import Settings
s = Settings()
assert s.worker_execution_mode == "queue"
assert s.enable_unified_ytdlp_stream_extractor is True
assert s.media_extract_output_mode == "temp_file"
print("settings smoke passed")
PY
# passed
```

## W-M16Q-POC Post-Extraction Fix Validation

```bash
python3 -m compileall app
# passed

env PYTHONPYCACHEPREFIX=/private/tmp/recipe-dost-pycache .venv/bin/python -m compileall app
# passed

grep -R "cdn_url.*source_metadata\|source_metadata.*cdn_url\|temporary_download_url" -n app docs || true
# matched generated app/__pycache__ plus historical commands-log entries only; no source-code public CDN storage match
```

## W-M16Q-POC-C1 Validation

```bash
python3 -m compileall app
# passed

env PYTHONPYCACHEPREFIX=/private/tmp/recipe-dost-pycache .venv/bin/python -m compileall app
# passed

WORKER_API_SECRET=test SUPABASE_URL=https://example.supabase.co SUPABASE_SERVICE_ROLE_KEY=test WORKER_EXECUTION_MODE=queue ENABLE_YTDLP_COOKIES=true ENABLE_INSTAGRAM_COOKIES=true ENABLE_METADATA_ONLY_MOCK_FALLBACK=true .venv/bin/python - <<'PY'
from app.config import Settings
from app.main import cookies_configured
s = Settings()
assert s.enable_ytdlp_cookies is True
assert s.enable_instagram_cookies is True
assert s.enable_metadata_only_mock_fallback is True
assert cookies_configured(s, "instagram") in {True, False}
print("settings health smoke passed")
PY
# passed; local venv emitted urllib3 LibreSSL warning

grep -R "cdn_url.*source_metadata\|source_metadata.*cdn_url\|temporary_download_url\|COOKIES_B64\|cookies.txt" -n app docs || true
# matched existing/app intentional cookie file paths, generated __pycache__, and historical commands-log entries; no source-code public CDN storage match
```

## W-M16G-POC Validation

```bash
python3 -m compileall app
# passed

env PYTHONPYCACHEPREFIX=/private/tmp/recipe-dost-pycache .venv/bin/python -m compileall app
# passed

WORKER_API_SECRET=test SUPABASE_URL=https://example.supabase.co SUPABASE_SERVICE_ROLE_KEY=test AI_ANALYSIS_MODE=gemini_video_poc .venv/bin/python - <<'PY'
from app.config import Settings
s = Settings()
assert s.ai_analysis_mode == "gemini_video_poc"
assert s.effective_gemini_video_model
assert s.real_ai_fallback_to_mock is False
print("gemini_video_poc settings smoke passed")
PY
# passed

grep -R "cdn_url.*source_metadata\|source_metadata.*cdn_url\|temporary_download_url\|COOKIES_B64\|cookies.txt" -n app docs || true
# matched existing/app intentional cookie file paths, generated __pycache__, and historical commands-log entries; no source-code public CDN storage match
```

## W-M16G-C1 Validation

```bash
python3 -m compileall app
# passed

grep -R "cdn_url.*source_metadata\|source_metadata.*cdn_url\|temporary_download_url\|COOKIES_B64\|cookies.txt\|file_uri\|uploaded_file.uri" -n app docs || true
# matched intentional cookie file config/path strings, generated __pycache__, and historical commands-log entries; no queue Gemini event/source metadata leakage and no app source lower-snake Gemini file reference after cleanup
```
