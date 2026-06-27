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
