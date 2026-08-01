# Commands Log

Keep only commands that future turns may need.

## M23.W10F Instagram Unified v1 provider routing

```bash
PATH="$PWD/.venv/bin:$PATH" make check-dev
APP_ENV=production PATH="$PWD/.venv/bin:$PATH" make check-prod
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
.venv/bin/python -m pip check
git diff --check
# passed: development checks; 279 tests; no broken requirements; diff check
# production config/resolver checks passed; the exact check-prod command stopped
# only at the expected develop-vs-main deploy guard. The same offline check
# passed with ALLOW_NON_MAIN_PROD_DEPLOY=1; nothing was deployed.
```

No RapidAPI, media CDN, Supabase, Gemini, Render, Sentry, deployment, commit,
push, schema, dependency, queue, quota, or frontend command was run.

## M23.W10C explicit source/recipe duration semantics

```bash
.venv/bin/python -m compileall app scripts
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
# passed: 275 tests

.venv/bin/python scripts/check_runtime_config.py development
.venv/bin/python scripts/check_runtime_config.py production
.venv/bin/python scripts/validate_env.py development --example
.venv/bin/python scripts/validate_env.py production --example
# passed; both environments inherit MAX_SOURCE_VIDEO_DURATION_SECONDS=180

.venv/bin/python -m pip check
.venv/bin/python scripts/worker_qa.py batch \
  --manifest tests/fixtures/worker_qa/replay_manifest.json
.venv/bin/python scripts/worker_qa.py batch \
  --manifest tests/fixtures/worker_qa/mixed_batch_manifest.json
git diff --check
```

No live media, Gemini, provider, Supabase, Sentry, Render, migration,
deployment, commit, or push command was run.

## M23.W10A-CFG queue-liveness configuration alignment

```bash
.venv/bin/python -m unittest \
  tests.test_file_runtime_config tests.test_queue_liveness \
  tests.test_fastapi_startup_config
# 50 focused config/queue/startup/health tests passed

.venv/bin/python -m unittest \
  tests.test_queue_gemini_invocation tests.test_queue_gemini_guard \
  tests.test_gemini_reliability tests.test_media_resolver_adapters \
  tests.test_external_queue_routing tests.test_external_resolver_config
# 63 unchanged Gemini/provider/resolver tests passed

.venv/bin/python -m compileall app scripts
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
.venv/bin/python -m pip check
.venv/bin/python scripts/check_runtime_config.py development
.venv/bin/python scripts/check_runtime_config.py production
.venv/bin/python scripts/worker_qa.py batch \
  --manifest tests/fixtures/worker_qa/replay_manifest.json
.venv/bin/python scripts/worker_qa.py batch \
  --manifest tests/fixtures/worker_qa/mixed_batch_manifest.json
git diff --check
# passed: compile; 264 tests; no broken requirements; both config checks;
# 11-case replay; 7-case mixed batch; diff check
```

The mixed batch was FIFO, single-active-job, terminal 7/7, unstarved, and
ended with zero active reservations. No live Supabase, Gemini, provider,
Sentry, Render, deployment, database, frontend, secret, or dependency action
occurred.

## M23.W8A-W8D Instagram Unified v1 adapter

```bash
PATH="$PWD/.venv/bin:$PATH" python --version
# Python 3.12.13

PATH="$PWD/.venv/bin:$PATH" python -m unittest -q \
  tests.test_media_resolver_adapters tests.test_file_runtime_config \
  tests.test_instagram_live_diagnostic \
  tests.test_instagram_candidate_compatibility \
  tests.test_external_media_processing tests.test_external_media_security \
  tests.test_provider_http_client tests.test_external_queue_routing \
  tests.test_queue_gemini_guard tests.test_external_resolver_config
# 86 focused tests passed

PATH="$PWD/.venv/bin:$PATH" python -m compileall app scripts
PATH="$PWD/.venv/bin:$PATH" python -m unittest discover \
  -s tests -p "test_*.py"
PATH="$PWD/.venv/bin:$PATH" python -m pip check
PATH="$PWD/.venv/bin:$PATH" python scripts/worker_qa.py batch \
  --manifest tests/fixtures/worker_qa/replay_manifest.json
PATH="$PWD/.venv/bin:$PATH" python scripts/worker_qa.py batch \
  --manifest tests/fixtures/worker_qa/mixed_batch_manifest.json
git diff --check
# passed: compile; 202 tests; no broken requirements; 11-case replay;
# 7-case mixed batch; diff check.
```

The owner-only Instagram diagnostic was compile-tested and unit-tested but not
executed. No live provider/media URL, Gemini, Supabase, Sentry, Render,
deployment, main merge, mobile, schema, cookie, entitlement, quota, secret
read, or secret output occurred.

## M23.W7A-W7C staged combined worker

```bash
PATH="$PWD/.venv/bin:$PATH" python --version
# Python 3.12.13

PATH="$PWD/.venv/bin:$PATH" python -m compileall app scripts

PATH="$PWD/.venv/bin:$PATH" python -m unittest discover \
  -s tests -p "test_*.py"
# intermediate pass: 190 tests

PATH="$PWD/.venv/bin:$PATH" python scripts/worker_qa.py pipeline \
  --manifest tests/fixtures/worker_qa/mixed_batch_manifest.json \
  --pipeline-mode staged
# passed: 7 terminal, 5 expected completed, FIFO/backpressure/overlap/cleanup,
# active reservations 0; analyzer-two probe reached 2 analyzers with ingest 1
```

No live provider, Gemini, Supabase, Sentry, or Render call was made. No deploy,
branch switch, main merge, mobile/schema/quota-limit/direct-CDN/cookie/secret
operation occurred. Final validation results are appended after the full pass.

Final validation:

```bash
PATH="$PWD/.venv/bin:$PATH" python -m compileall app scripts
PATH="$PWD/.venv/bin:$PATH" python -m unittest discover \
  -s tests -p "test_*.py"
PATH="$PWD/.venv/bin:$PATH" python -m pip check
PATH="$PWD/.venv/bin:$PATH" python scripts/worker_qa.py batch \
  --manifest tests/fixtures/worker_qa/mixed_batch_manifest.json
PATH="$PWD/.venv/bin:$PATH" python scripts/worker_qa.py pipeline \
  --manifest tests/fixtures/worker_qa/mixed_batch_manifest.json \
  --pipeline-mode staged
git diff --check
# passed with Python 3.12.13: compile; 194 tests; no broken requirements;
# 7-case batch; 7-case staged/concurrency-two QA; diff check.
```

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

## W-M16G-C2 Validation

```bash
python3 -m compileall app
# passed

grep -R "cdn_url.*source_metadata\|source_metadata.*cdn_url\|temporary_download_url\|COOKIES_B64\|cookies.txt\|file_uri\|uploaded_file.uri" -n app docs || true
# matched intentional cookie file config/path strings, generated __pycache__, and historical commands-log entries only
```

## W-M16G-C3 Validation

```bash
python3 -m compileall app
# passed

grep -R "cdn_url.*source_metadata\|source_metadata.*cdn_url\|temporary_download_url\|COOKIES_B64\|cookies.txt\|file_uri\|uploaded_file.uri" -n app docs || true
# matched intentional cookie file config/path strings, generated __pycache__, and historical commands-log entries only
```

## M17A Validation

```bash
python3 -m compileall app
# passed

grep -R "cdn_url.*source_metadata\|source_metadata.*cdn_url\|temporary_download_url\|COOKIES_B64\|cookies.txt\|file_uri\|uploaded_file.uri" -n app docs || true
# matched intentional cookie file config/path strings, generated __pycache__, and historical commands-log entries only
```

## M18 Validation

```bash
python3 -m compileall app
# passed

grep -R "cdn_url.*source_metadata\|source_metadata.*cdn_url\|temporary_download_url\|COOKIES_B64\|cookies.txt\|file_uri\|uploaded_file.uri" -n app docs || true
# matched intentional cookie file config/path strings, generated __pycache__, and historical commands-log entries only
```

## Queue Gemini Token Optimization Validation

```bash
python3 -m compileall app
# passed

WORKER_API_SECRET=test SUPABASE_URL=https://example.supabase.co SUPABASE_SERVICE_ROLE_KEY=test .venv/bin/python - <<'PY'
from app.config import Settings
s = Settings(_env_file=None)
assert s.gemini_video_brief_max_output_tokens == 768
assert s.gemini_recipe_json_max_output_tokens == 1536
assert s.gemini_recipe_brief_max_chars == 4000
print("gemini token cap settings passed")
PY
# passed
```

## M20 Output Quality Hardening Validation

```bash
python3 -m compileall app
# passed

env PYTHONPYCACHEPREFIX=/private/tmp/recipe-dost-pycache .venv/bin/python -m compileall app
# passed

grep -R "cdn_url.*source_metadata|source_metadata.*cdn_url|temporary_download_url|COOKIES_B64|cookies.txt|file_uri|uploaded_file.uri" -n app docs || true
# matched only this M20 commands-log validation entry

WORKER_API_SECRET=test SUPABASE_URL=https://example.supabase.co SUPABASE_SERVICE_ROLE_KEY=test .venv/bin/python - <<'PY'
from app.config import Settings
s = Settings(_env_file=None)
assert s.recipe_quality_gate_enabled is True
assert s.recipe_repair_enabled is True
assert s.recipe_min_ingredients == 2
assert s.recipe_min_steps == 3
assert s.recipe_min_brief_length == 500
assert s.gemini_video_brief_retry_enabled is True
assert s.gemini_max_output_tokens == 4096
assert s.recipe_bilingual_output_enabled is True
assert s.recipe_urdu_fallback_enabled is True
print("m20 settings smoke passed")
PY
# passed

WORKER_API_SECRET=test SUPABASE_URL=https://example.supabase.co SUPABASE_SERVICE_ROLE_KEY=test .venv/bin/python - <<'PY'
from app.config import Settings
from app.queue_worker import fallback_recipe_json_from_brief, normalize_recipe_card_payload, recipe_quality_passed, PROMPT_VERSION
brief = """TITLE
Mutton Karahi
SUMMARY
A stovetop mutton karahi with tomatoes and spices.
INGREDIENTS
- Mutton (bone-in): ~1 kg
- Tomatoes: 4 medium
- Ginger garlic paste: 2 tbsp
STEPS
1. Heat oil and add mutton.
2. Add ginger garlic paste and cook until fragrant.
3. Add tomatoes and spices, then simmer until tender.
GROCERY_LIST
- Mutton (bone-in): ~1 kg
- Tomatoes: 4 medium
CONFIDENCE
medium
MISSING_OR_UNCERTAIN_DETAILS
Exact salt quantity is not shown.
"""
settings = Settings(_env_file=None)
parsed = fallback_recipe_json_from_brief(brief, {"title": "Mutton Karahi", "platform": "tiktok"})
result = normalize_recipe_card_payload(parsed, {"title": "Mutton Karahi"}, "tiktok", "gemini-2.5-flash-lite", PROMPT_VERSION)
assert len(result.ingredients) >= 2
assert len(result.steps) >= 3
assert len(result.grocery_list) >= 1
assert result.ingredients[0].name_en == "Mutton (bone-in)"
assert result.ingredients[0].quantity == "~1 kg"
assert "Recipe Facts:" not in result.short_summary_en
assert recipe_quality_passed(result, settings)
print("m20 parser quality smoke passed")
PY
# passed; local venv emitted urllib3 LibreSSL warning
```

## M20 Gemini 3.1 Single-Pass Validation

```bash
python3 -m compileall app
# passed

env PYTHONPYCACHEPREFIX=/private/tmp/recipe-dost-pycache .venv/bin/python -m compileall app
# passed

grep -R "cdn_url.*source_metadata\|source_metadata.*cdn_url\|temporary_download_url\|COOKIES_B64\|cookies.txt\|file_uri\|uploaded_file.uri" -n app docs || true
# matched intentional cookie path config, generated pycache, and historical commands-log validation text only

WORKER_API_SECRET=test SUPABASE_URL=https://example.supabase.co SUPABASE_SERVICE_ROLE_KEY=test .venv/bin/python - <<'PY'
from app.config import Settings
from app.queue_worker import validate_and_normalize_canonical_recipe, recipe_quality_passed
s = Settings(_env_file=None)
assert s.gemini_extraction_mode == "single_pass_structured"
assert s.gemini_primary_model == "gemini-3.1-flash-lite"
assert s.effective_gemini_video_model == "gemini-3.1-flash-lite"
assert s.effective_gemini_text_model == "gemini-3.1-flash-lite"
assert s.gemini_fallback_model == "gemini-2.5-flash"
assert s.gemini_thinking_level == "high"
assert s.gemini_max_output_tokens == 12000
canonical = {
  "title_en": "Mutton Karahi", "title_ur": "مٹن کڑاہی",
  "summary_en": "A stovetop mutton karahi with tomatoes and spices.",
  "summary_ur": "ٹماٹر اور مصالحوں کے ساتھ مٹن کڑاہی۔",
  "ingredients": [
    {"name_en": "Mutton (bone-in)", "name_ur": "مٹن", "quantity": "~1 kg", "notes_en": "", "notes_ur": ""},
    {"name_en": "Tomatoes", "name_ur": "ٹماٹر", "quantity": "4 medium", "notes_en": "", "notes_ur": ""}
  ],
  "steps": [
    {"step_number": 1, "instruction_en": "Heat oil and add mutton.", "instruction_ur": "تیل گرم کر کے مٹن ڈالیں۔", "tip_en": "", "tip_ur": ""},
    {"step_number": 2, "instruction_en": "Add ginger garlic paste and cook until fragrant.", "instruction_ur": "ادرک لہسن پیسٹ ڈال کر بھونیں۔", "tip_en": "", "tip_ur": ""},
    {"step_number": 3, "instruction_en": "Add tomatoes and spices, then simmer until tender.", "instruction_ur": "ٹماٹر اور مصالحے ڈال کر گلنے تک پکائیں۔", "tip_en": "", "tip_ur": ""}
  ],
  "grocery_list": [],
  "time": {"prep_minutes": None, "cook_minutes": None, "total_minutes": None},
  "servings": None, "difficulty": "medium", "cuisine_en": "Pakistani", "cuisine_ur": "پاکستانی",
  "tags": ["karahi"], "missing_info": ["Exact salt quantity is not shown."], "confidence": "high"
}
result, errors = validate_and_normalize_canonical_recipe(canonical, {"platform": "tiktok", "title": "Mutton Karahi"}, s)
assert not errors
assert recipe_quality_passed(result, s)
assert len(result.grocery_list) == len(result.ingredients)
assert result.ingredients[0].name_ur
assert result.steps[0].instruction_ur
assert result.source_assumptions_en == []
print("single-pass canonical smoke passed")
PY
# passed; local venv emitted urllib3 LibreSSL warning
```

## M20.5 Worker Repo Cleanup Validation

```bash
python -m compileall app
# zsh: command not found: python

python3 -m compileall app
# passed

grep -R "cdn_url.*source_metadata\|source_metadata.*cdn_url\|temporary_download_url\|COOKIES_B64\|cookies.txt\|file_uri\|uploaded_file.uri" -n app docs README.md || true
# matched intentional literal cookie path/config strings, ignored generated pycache,
# and historical commands-log entries only; no committed secret values found
```

## M21.7A Metadata Preview Validation

```bash
python -m compileall app
# zsh: command not found: python

python3 -m compileall app
# passed

WORKER_API_SECRET=test SUPABASE_URL=https://example.supabase.co SUPABASE_SERVICE_ROLE_KEY=test .venv/bin/python - <<'PY'
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

get_settings.cache_clear()

class FakeResponse:
    def raise_for_status(self):
        return None
    def json(self):
        return {
            'title': 'Chicken & Rice #dinner #easy #quick #food',
            'thumbnail_url': 'https://i.ytimg.com/vi/abc123/hqdefault.jpg',
            'author_name': 'Recipe Dost Test',
        }

with patch('app.metadata_preview.requests.get', Mock(return_value=FakeResponse())):
    client = TestClient(app)
    unauthorized = client.post('/metadata-preview', json={'url': 'https://www.youtube.com/watch?v=abc123abc12'})
    assert unauthorized.status_code == 401, unauthorized.text
    response = client.post(
        '/metadata-preview',
        headers={'Authorization': 'Bearer test'},
        json={'url': 'https://www.youtube.com/watch?v=abc123abc12', 'platform': None},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['platform'] == 'youtube'
    assert body['resolver'] == 'youtube_oembed'
    assert body['title'].startswith('Chicken & Rice')
    assert body['thumbnail_url'].startswith('https://i.ytimg.com/')
    assert body['metadata_status'] == 'completed'
    assert 'cdn' not in body
    health = client.get('/health')
    assert health.status_code == 200, health.text
    health_body = health.json()
    assert health_body['metadata_preview_enabled'] is True
    assert 'youtube' in health_body['supported_metadata_platforms']
print('metadata preview route smoke passed')
PY
# passed; local venv emitted urllib3 LibreSSL warning

grep -R "cdn_url.*source_metadata\|source_metadata.*cdn_url\|temporary_download_url\|COOKIES_B64\|cookies.txt\|file_uri\|uploaded_file.uri" -n app docs README.md || true
# matched existing cookie path literals, generated pycache binaries, README cookie docs,
# and historical commands-log validation text only; no metadata-preview CDN/video URL return path found
```

## M21.9B Thumbnail Selection Validation

```bash
python -m compileall app
# zsh: command not found: python

python3 -m compileall app
# passed

WORKER_API_SECRET=test SUPABASE_URL=https://example.supabase.co SUPABASE_SERVICE_ROLE_KEY=test .venv/bin/python - <<'PY'
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.social.thumbnail_selection import safe_image_url, select_ytdlp_thumbnail
from app.social.thumbnail_upload import frame_score

best = select_ytdlp_thumbnail({
    'thumbnail': 'https://cdn.example.com/avatar_150x150.jpg',
    'thumbnails': [
        {'url': 'https://cdn.example.com/avatar_150x150.jpg', 'width': 150, 'height': 150, 'id': 'avatar'},
        {'url': 'https://cdn.example.com/frame_small.jpg', 'width': 320, 'height': 180},
        {'url': 'https://cdn.example.com/frame_large.jpg', 'width': 1280, 'height': 720},
    ],
})
assert best.url == 'https://cdn.example.com/frame_large.jpg'
assert best.source == 'ytdlp_thumbnails_array'
assert best.selected is True
assert select_ytdlp_thumbnail({'thumbnail': 'https://cdn.example.com/food.webp'}).source == 'ytdlp_thumbnail'
assert safe_image_url('https://cdn.example.com/video.mp4') is None
assert frame_score(bytes([0, 0, 0]) * (160 * 90)) == 0

get_settings.cache_clear()
class FakeResponse:
    def raise_for_status(self):
        return None
    def json(self):
        return {'title': 'Chicken & Rice', 'thumbnail_url': 'https://i.ytimg.com/vi/abc123/hqdefault.jpg'}

with patch('app.metadata_preview.requests.get', Mock(return_value=FakeResponse())):
    client = TestClient(app)
    response = client.post(
        '/metadata-preview',
        headers={'Authorization': 'Bearer test'},
        json={'url': 'https://www.youtube.com/watch?v=abc123abc12', 'platform': None},
    )
    assert response.status_code == 200
    assert response.json()['thumbnail_url'].startswith('https://i.ytimg.com/')
print('thumbnail selection smoke passed')
PY
# passed; local venv emitted urllib3 LibreSSL warning

grep -R "cdn_url.*source_metadata\|source_metadata.*cdn_url\|temporary_download_url\|COOKIES_B64\|cookies.txt\|file_uri\|uploaded_file.uri" -n app docs README.md || true
# matched existing cookie path literals, generated pycache binaries, README cookie docs,
# and historical commands-log validation text only; no new thumbnail leak pattern found
```

## M21.9B.1B Platform-First Thumbnail Validation

```bash
python -m compileall app
# zsh: command not found: python

python3 -m compileall app
# passed

WORKER_API_SECRET=test SUPABASE_URL=https://example.supabase.co SUPABASE_SERVICE_ROLE_KEY=test .venv/bin/python - <<'PY'
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.social.thumbnail_selection import safe_image_url, select_ytdlp_thumbnail, should_update_thumbnail
from app.social.thumbnail_upload import frame_score

best = select_ytdlp_thumbnail({
    'thumbnail': 'https://cdn.example.com/avatar_150x150.jpg',
    'thumbnails': [
        {'url': 'https://cdn.example.com/avatar_150x150.jpg', 'width': 150, 'height': 150, 'id': 'avatar'},
        {'url': 'https://cdn.example.com/frame_small.jpg', 'width': 320, 'height': 180},
        {'url': 'https://cdn.example.com/frame_large.jpg', 'width': 1280, 'height': 720},
    ],
}, 'instagram')
assert best.source == 'instagram_ytdlp_array'
assert best.provider == 'instagram'
assert best.strategy == 'platform_first_v2'
assert best.candidate_count == 3
assert select_ytdlp_thumbnail({'thumbnail': 'https://cdn.example.com/food.webp'}, 'tiktok').source == 'tiktok_ytdlp_thumbnail'
assert safe_image_url('https://cdn.example.com/video.mp4') is None
assert frame_score(bytes([0, 0, 0]) * (160 * 90)) == 0
assert should_update_thumbnail(existing_url='https://i.ytimg.com/vi/x/hqdefault.jpg', existing_source=None, new_source='early_frame_fallback') is False
assert should_update_thumbnail(existing_url='https://storage.example.com/old.jpg', existing_source=None, new_source='instagram_ytdlp_thumbnail') is True

get_settings.cache_clear()
class FakeResponse:
    def raise_for_status(self):
        return None
    def json(self):
        return {'title': 'Chicken & Rice', 'thumbnail_url': 'https://i.ytimg.com/vi/abc123/hqdefault.jpg'}

with patch('app.metadata_preview.requests.get', Mock(return_value=FakeResponse())):
    client = TestClient(app)
    response = client.post(
        '/metadata-preview',
        headers={'Authorization': 'Bearer test'},
        json={'url': 'https://www.youtube.com/watch?v=abc123abc12', 'platform': None},
    )
    assert response.status_code == 200
    body = response.json()
    assert body['thumbnail_source'] == 'youtube_oembed'
    assert body['thumbnail_provider'] == 'youtube'
    assert body['thumbnail_strategy'] == 'platform_first_v2'
    assert body['thumbnail_selected'] is True
print('platform_first_v2 thumbnail smoke passed')
PY
# passed; local venv emitted urllib3 LibreSSL warning
```

## M21.9B.2 YouTube Shorts thumbnail variant fix

```bash
python3 -m compileall app
# passed
```

## M22B.2D Gemini structured extraction resilience

```bash
python3 -m compileall app
# passed
```

## M22D.0B Worker env source-of-truth

```bash
python scripts/validate_env.py development
# zsh: command not found: python

python3 scripts/validate_env.py development
# failed gracefully because env/development.env has not been created yet

python3 scripts/validate_env.py production
# may fail locally until env/production.env is created from env/production.example.env

python3 scripts/export_render_env.py development --output /tmp/recipe-dost-worker-development.env
# validates first, prints key names only, and writes a temporary secret-bearing upload file

python3 scripts/export_render_env.py production --output /tmp/recipe-dost-worker-production.env
# validates first, prints key names only, and writes a temporary secret-bearing upload file

python3 -m compileall app scripts
# passed

python3 -m compileall app
# passed

PYTHONPATH=scripts python3 - <<'PY'
from pathlib import Path
from validate_env import read_env_file, validate_values
for env in ('development', 'production'):
    values = read_env_file(Path(f'env/{env}.example.env'))
    validate_values(env, values)
print('example env templates validate')
PY
# passed
```

## M22D.0C Worker env names audit and cleanup

```bash
rg -n --hidden --glob '!/.env' --glob '!/.env.*' --glob '!recipe-dost-media-worker-queue-poc.env' --glob '!cloudrun.env.yaml' --glob '!app/cookies/**' "os\.environ|os\.getenv|dotenv|SUPABASE_|GEMINI_|MEDIA_WORKER_|COBALT|TIKWM|RAPIDAPI|APP_ENV|MAX_VIDEO_SECONDS|ALLOW_RAW_URL_LOGS|LOG_LEVEL" .
# reviewed without reading real env files

python3 -m compileall app scripts
# passed

python3 scripts/validate_env.py development
# failed gracefully because env/development.env has not been created yet

python3 scripts/validate_env.py development --example
# passed; printed masked values only

python3 scripts/validate_env.py production --example
# passed; printed masked values only

rg -n --hidden --glob '!/.env' --glob '!/.env.*' --glob '!recipe-dost-media-worker-queue-poc.env' --glob '!cloudrun.env.yaml' --glob '!app/cookies/**' "COBALT|TIKWM|RAPIDAPI" env README.md docs/m22d-worker-environments.md docs/m22d-worker-env-variable-map.md scripts Makefile
# matched only the variable-map documentation
```

## M22D.0D Worker env/config rationalization

```bash
python3 - <<'PY'
from pathlib import Path
for name in ['.env', 'recipe-dost-media-worker-queue-poc.env']:
    path = Path(name)
    if not path.exists():
        print(f'{name}: missing')
        continue
    keys = []
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].strip()
        if '=' in line:
            keys.append(line.split('=', 1)[0].strip())
    print(f'{name}: {len(keys)} keys')
    for key in sorted(set(keys)):
        print(f'- {key}')
PY
# printed key names only, no values

python3 -m compileall app scripts
# passed

python3 scripts/validate_env.py development --example
# passed; masked env and config values

python3 scripts/validate_env.py production --example
# passed; masked env and config values

python3 scripts/validate_env.py development
# failed gracefully because env/development.env does not exist

APP_ENV=development MEDIA_WORKER_SHARED_SECRET=test SUPABASE_URL=https://example.supabase.co SUPABASE_SERVICE_ROLE_KEY=test GEMINI_API_KEY=test .venv/bin/python - <<'PY'
from app.config import Settings
s = Settings()
assert s.worker_api_secret.get_secret_value() == 'test'
assert s.ai_analysis_mode == 'gemini_video_poc'
assert s.enable_unified_ytdlp_stream_extractor is True
assert s.max_video_duration_seconds <= 120
print('settings config smoke passed')
PY
# passed

APP_ENV=development MEDIA_WORKER_SHARED_SECRET=test SUPABASE_URL=https://example.supabase.co SUPABASE_SERVICE_ROLE_KEY=test GEMINI_API_KEY=test AI_ANALYSIS_MODE=mock_fixture ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR=false .venv/bin/python - <<'PY'
from app.config import Settings
s = Settings()
assert s.ai_analysis_mode == 'mock_fixture'
assert s.enable_unified_ytdlp_stream_extractor is False
print('legacy env override smoke passed')
PY
# passed

PYTHONPATH=scripts python3 - <<'PY'
from export_render_env import render_values
values = render_values({
    'APP_ENV': 'development',
    'LOG_LEVEL': 'debug',
    'SUPABASE_URL': 'https://example.supabase.co',
    'SUPABASE_SERVICE_ROLE_KEY': 'test',
    'MEDIA_WORKER_SHARED_SECRET': 'secret',
    'GEMINI_API_KEY': 'test',
    'AI_ANALYSIS_MODE': 'mock_fixture',
    'MAX_VIDEO_DURATION_SECONDS': '120',
})
assert 'WORKER_API_SECRET' in values
assert 'MEDIA_WORKER_SHARED_SECRET' not in values
assert 'AI_ANALYSIS_MODE' not in values
assert 'MAX_VIDEO_DURATION_SECONDS' not in values
print('render export filter smoke passed')
PY
# passed
```

## M22D.3 Production worker setup result log

```bash
python3 -m compileall app scripts
# passed
```

## M22D.5A Production worker extractor config parity fix

```bash
python3 -m compileall app scripts
# passed

python3 scripts/validate_env.py production --example
# passed; masked env and config values

python3 scripts/validate_env.py development --example
# passed; masked env and config values

python3 scripts/validate_env.py production
# passed; env/production.env exists locally and output stayed masked

APP_ENV=production WORKER_API_SECRET=test SUPABASE_URL=https://example.supabase.co SUPABASE_SERVICE_ROLE_KEY=test GEMINI_API_KEY=test .venv/bin/python - <<'PY'
from app.config import Settings
s = Settings(_env_file=None)
assert s.worker_execution_mode == 'queue'
assert s.ai_analysis_mode == 'gemini_video_poc'
assert s.enable_unified_ytdlp_stream_extractor is True
assert s.enable_render_instagram_resolver is True
assert s.enable_ytdlp_cookies is True
assert s.enable_instagram_cookies is True
assert s.media_extract_output_mode == 'temp_file'
assert s.max_video_duration_seconds <= 120
assert s.real_ai_fallback_to_mock is False
print('production settings smoke passed')
PY
# passed
```

## M22D.5B Social resolver production parity and TikTok NameError fix

```bash
python3 -m compileall app scripts
# passed

python3 scripts/validate_env.py development --example
# passed; masked env and config values

python3 scripts/validate_env.py production --example
# passed; masked env and config values

python3 scripts/validate_env.py production
# passed; env/production.env exists locally and output stayed masked

APP_ENV=production WORKER_API_SECRET=test SUPABASE_URL=https://example.supabase.co SUPABASE_SERVICE_ROLE_KEY=test GEMINI_API_KEY=test .venv/bin/python - <<'PY'
from app.config import Settings
from app.main import safe_config_summary
s = Settings(_env_file=None)
summary = safe_config_summary(s)
assert summary['app_env'] == 'production'
assert summary['worker_execution_mode'] == 'queue'
assert summary['ai_analysis_mode'] == 'gemini_video_poc'
assert summary['enabled_platforms']['instagram'] is True
assert summary['enabled_platforms']['tiktok'] is True
assert summary['resolvers']['tiktok'] == 'tikwm'
assert summary['enable_unified_ytdlp_stream_extractor'] is True
assert summary['enable_instagram_cookies'] is True
assert summary['enable_ytdlp_cookies'] is True
assert summary['media_extract_output_mode'] == 'temp_file'
assert summary['max_video_duration_seconds'] <= 120
assert summary['raw_url_logging_enabled'] is False
assert summary['mock_fallback']['real_ai_fallback_to_mock'] is False
assert isinstance(summary['has_instagram_cookies'], bool)
assert isinstance(summary['has_youtube_cookies'], bool)
assert isinstance(summary['has_tiktok_cookies'], bool)
print('safe config summary smoke passed')
PY
# passed
```

## M22D.5C Social pipeline source-of-truth audit

```bash
python3 -m compileall app scripts
# passed
```

## M22D.5D Queue orchestration compatibility fix

```bash
python3 -m compileall app scripts
# passed

python3 scripts/validate_env.py development --example
# passed; masked env and config values

python3 scripts/validate_env.py production --example
# passed; masked env and config values

python3 scripts/validate_env.py production
# passed; env/production.env exists locally and output stayed masked

python3 scripts/check_process_job_queue_mode.py
# failed locally because system python does not have FastAPI installed

.venv/bin/python scripts/check_process_job_queue_mode.py
# passed; local venv emitted urllib3 LibreSSL warning
```

## M22D.5E Production config packaging/loading fix

```bash
python3 -m compileall app scripts
# passed

python3 scripts/check_runtime_config.py production
# passed; loaded worker.defaults.json and worker.production.json

python3 scripts/check_runtime_config.py development
# passed; loaded worker.defaults.json and worker.development.json

python3 scripts/validate_env.py production --example
# passed; masked env and config values

python3 scripts/validate_env.py development --example
# passed; masked env and config values

python3 scripts/validate_env.py production
# passed; env/production.env exists locally and output stayed masked
```

## M22D.5F Production worker smoke completion log

```bash
python3 -m compileall app scripts
# passed

python3 scripts/check_runtime_config.py production
# passed; loaded worker.defaults.json and worker.production.json
```

## M22E.0B Worker branch and environment safety

```bash
python3 -m compileall app scripts
# passed

python3 scripts/check_runtime_config.py development
# passed; loaded worker.defaults.json and worker.development.json

python3 scripts/check_runtime_config.py production
# passed; loaded worker.defaults.json and worker.production.json

python3 scripts/check_environment.py development
# passed on branch develop; output limited to branch, env identity, config filenames,
# Supabase host only, and non-secret runtime config checks

python3 scripts/check_environment.py production
# blocked as designed on branch develop:
# production deployment requires branch main unless ALLOW_NON_MAIN_PROD_DEPLOY=1

ALLOW_NON_MAIN_PROD_DEPLOY=1 python3 scripts/check_environment.py production
# passed; verifies the explicit one-command override path without printing secrets

make check-dev
# passed

make check-prod
# blocked as designed on branch develop after compile and runtime config checks passed
```

## M22E.1R-B AI-generated tags end-to-end

```bash
python3 -m compileall app scripts
# passed

.venv/bin/python scripts/check_recipe_tags.py
# passed; checks lowercase/trim/dedupe/length/generic/unsafe filtering and the
# in-memory recipe_cards insert payload maps the final list to tags_json

python3 scripts/check_runtime_config.py development
# passed; loaded worker.defaults.json and worker.development.json

python3 scripts/check_runtime_config.py production
# passed; loaded worker.defaults.json and worker.production.json

python3 scripts/check_environment.py development
# passed on develop; output limited to branch, environment identity, Supabase host,
# and non-secret runtime config values

cd ../recipe-dost && npm run qa:static
# passed; typecheck, Expo config, and static guards passed

cd ../recipe-dost && node scripts/env/env-doctor.mjs development
# passed; development env doctor did not print secret values

git push origin develop
# passed; d3b29b3 was pushed to the development branch

# Render deployment/database smoke not run: dashboard.render.com required sign-in
# in this workspace. No development media job was created and no production action occurred.
```

## M22E.2C Privacy-safe worker Sentry monitoring

```bash
.venv/bin/python -m pip install 'sentry-sdk>=2.65,<3.0'
# installed sentry-sdk 2.65.0 without upgrading unrelated packages

.venv/bin/python -m unittest discover -s tests -v
# passed: 19 focused policy, sanitizer, reporting, controlled-test, and active-queue-boundary tests

python3 -m compileall app scripts tests
# passed

.venv/bin/python scripts/check_process_job_queue_mode.py
# passed

.venv/bin/python scripts/check_recipe_tags.py
# passed

python3 scripts/check_runtime_config.py development
python3 scripts/check_runtime_config.py production
# passed for both environments

python3 scripts/validate_env.py development --example
python3 scripts/validate_env.py production --example
# passed; output remained masked

.venv/bin/python -m pip check
# passed: no broken requirements

# Bounded startup/shutdown smoke used mocked queue startup and fake local settings.
# It made no Supabase, Gemini, URL, or Render calls and passed.

# Actual sentry-sdk transport smoke patched the HTTP transport in memory, captured
# one sanitized current-flow event, flushed, and passed without a network event.

make check-dev
# passed on develop

git diff --check
# passed
```

No Render deploy, live Sentry event, live Supabase/Gemini call, real URL processing, or production action was performed.

## M23.W2 adaptive media and terminal outcomes

```bash
.venv/bin/python -m unittest discover -s tests -v
# passed: 47 tests covering adaptive selection/download guards/cleanup,
# incident success and terminal fixtures, existing DB failure fields, queue
# terminal behavior, Sentry policy/grouping/sanitization, and DSN isolation

.venv/bin/python -m pytest -q
# unavailable: the existing virtual environment has no pytest module; no
# dependency was installed. Full unittest discovery above is the repository's
# available suite.

python3 -m compileall app scripts
.venv/bin/python scripts/check_process_job_queue_mode.py
.venv/bin/python scripts/check_recipe_tags.py
.venv/bin/python -m pip check
git diff --check
# passed

.venv/bin/python scripts/check_runtime_config.py development
.venv/bin/python scripts/check_runtime_config.py production
.venv/bin/python scripts/validate_env.py development --example
.venv/bin/python scripts/validate_env.py production --example
make check-dev
# passed with masked/safe output
```

No production URL/media, Supabase, Gemini, Sentry transport, Render deployment,
mobile write, `main` checkout/merge, or live job was used.

## M23.W2B direct Instagram candidate regression

```bash
.venv/bin/python -m unittest tests.test_adaptive_media -v
.venv/bin/python -m unittest tests.test_queue_failure_contract -q
# focused candidate/direct/cookie/probe/size/contract tests passed

.venv/bin/python -m unittest discover -s tests -v
# passed: 61 tests

python3 -m compileall app scripts
.venv/bin/python scripts/check_process_job_queue_mode.py
git diff --check
# passed

.venv/bin/python -m pytest -q
# not rerun: pytest is not installed in the existing repository virtualenv;
# unittest discovery is the available repository suite
```

`command -v ffprobe` and `command -v ffmpeg` returned no executable. Dockerfile
inspection also confirmed neither dependency is installed. No live media,
Instagram, Supabase, Gemini, Sentry transport, Render deployment, mobile write,
or `main` operation occurred.

## M23.W3A external media resolver architecture audit

```bash
python3 -m compileall app
.venv/bin/python scripts/check_runtime_config.py development
.venv/bin/python scripts/check_runtime_config.py production
.venv/bin/python scripts/check_process_job_queue_mode.py
git diff --check
# passed; the queue-mode check emitted only the existing local LibreSSL warning

git status --short
# documentation files only: the consolidated M23.W3A plan and the required
# decision, milestone, command, and social-debug tracking updates
```

The audit used targeted source searches and limited ranges in the worker and
read-only Build 3 repositories. It did not call a provider, process a URL or
media file, inspect secrets, contact Supabase/Gemini/Sentry/Render, modify the
mobile repository, deploy, change schema/runtime configuration, switch or merge
`main`, or install dependencies.

## M23.W3C feature-flagged external resolver

```bash
python3 -m compileall app scripts
# passed

.venv/bin/python -m unittest discover -s tests -v
# passed: 96 tests

.venv/bin/python -m pytest -q
# unavailable: pytest is not installed in the existing virtual environment;
# no dependency was added

.venv/bin/python scripts/check_runtime_config.py development
.venv/bin/python scripts/check_runtime_config.py production
.venv/bin/python scripts/validate_env.py development --example
.venv/bin/python scripts/validate_env.py production --example
.venv/bin/python scripts/check_process_job_queue_mode.py
.venv/bin/python scripts/check_recipe_tags.py
.venv/bin/python -m pip check
git diff --check
# passed; env output was masked and the queue check emitted only the existing
# local LibreSSL warning
```

Sanitized fixture scans found no live provider URL, signed URL, or token-shaped
credential. The supplied TikTok token was not written and must be revoked and
rotated. No provider/media/Gemini/Supabase/Sentry network call, Render action,
deployment, dependency installation, schema/mobile change, branch switch,
commit, push, or `main` merge occurred.

## M23.W3C-R file-based resolver configuration

```bash
uv venv --python 3.12 /private/tmp/recipe-dost-m23w3c-r.0eMw1j/venv
uv pip install --python /private/tmp/recipe-dost-m23w3c-r.0eMw1j/venv/bin/python -r requirements.txt
# Python 3.12.13; installed from the same requirements.txt used by Docker

/private/tmp/recipe-dost-m23w3c-r.0eMw1j/venv/bin/python \
  -m unittest discover -s tests -p "test_*.py" -q
# passed: 117 tests, 0 failures, 0 errors, 0 skipped, exit 0

/private/tmp/recipe-dost-m23w3c-r.0eMw1j/venv/bin/python -m compileall app scripts
/private/tmp/recipe-dost-m23w3c-r.0eMw1j/venv/bin/python -m pip check
/private/tmp/recipe-dost-m23w3c-r.0eMw1j/venv/bin/python scripts/check_process_job_queue_mode.py
/private/tmp/recipe-dost-m23w3c-r.0eMw1j/venv/bin/python scripts/check_recipe_tags.py
git diff --check
# passed

# Development diagnostic: RENDER=true, RENDER_GIT_BRANCH=develop, and dummy
# provider credentials only. Production diagnostic: main branch metadata and
# no provider credentials. Both passed and sourced all six resolver controls
# from the matching config file.

make resolver-config-docker
# not executed: this Mac has no Docker executable; make exited 1 before build

# A /service layout containing the Dockerfile COPY inputs (app/config/scripts)
# was exercised with Python 3.12. Both development and production diagnostics
# passed. This is not recorded as a Docker build pass.
```

The secret-pattern scan found no token-shaped credential. Signed-URL markers
matched only deliberate `secret.invalid`/`provider.invalid` sanitizer and SSRF
test strings. No actual provider secret was read or printed. No provider,
Gemini, Supabase, or Sentry call, deployment, Render mutation, mobile/schema
change, branch switch, commit, push, or main merge occurred.

## M23.W3D-TK AKNG TikTok contract repair

```bash
uv venv --python 3.12 /private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv
uv pip install --python /private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python \
  -r requirements.txt
# Python 3.12.13; dependencies came from the same requirements.txt used by Docker

/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python -m unittest -q \
  tests.test_media_resolver_adapters tests.test_provider_http_client \
  tests.test_external_media_security tests.test_external_media_processing \
  tests.test_external_queue_routing tests.test_queue_claim \
  tests.test_queue_failure_contract tests.test_queue_gemini_guard \
  tests.test_metadata_preview_no_cookies tests.test_sentry_observability \
  tests.test_external_resolver_config tests.test_external_circuit_breaker \
  tests.test_tiktok_source_context
# passed: 78 focused tests

/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python \
  -m unittest discover -s tests -p "test_*.py" -q
# passed after final regression pass: 134 tests, 0 failures, 0 errors, 0 skipped, exit 0

/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python -m compileall -q app scripts
/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python -m pip check
/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python scripts/check_process_job_queue_mode.py
/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python scripts/check_recipe_tags.py
git diff --check
# passed

# Development config diagnostic used only dummy provider credentials plus
# RENDER=true and RENDER_GIT_BRANCH=develop. Production used branch main and no
# provider credentials. Both passed with resolver controls sourced from the
# matching repository JSON file.

# Privacy scan: no worktree match for the owner-provided token, real TikTok ID,
# or short-link code. Generic credential-name matches were limited to config
# identifiers and example/documentation placeholders; no value was read.
```

`scripts/check_tiktok_media_candidate.py` was compile-checked but deliberately
not executed because it is an owner-only live development diagnostic. No AKNG,
Instagram, Gemini, Supabase, Sentry, or Render request was made. No deployment,
Render mutation, mobile/schema change, branch switch, commit, push, or main
merge occurred.

## M23.W3D-TK3 short-link expansion and local media gate

```bash
git status --short --branch
git branch --show-current
git diff --check
# branch: develop; AKNG token presence checked as a boolean only: false

# Owner response inspected with a jq shape/count projection only. No URL, ID,
# token, content text, or response body was printed or committed.

/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python -m compileall -q app scripts
/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python -m unittest \
  tests.test_media_resolver_adapters tests.test_tiktok_short_link \
  tests.test_external_media_security
# 32 passed

/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python \
  -m unittest discover -s tests -p "test_*.py"
# 145 passed, 0 failures, 0 errors, exit 0 (intermediate TK3 pass)
```

The required opt-in live diagnostic was not run because
`TIKTOK_AKNG_ACCESS_TOKEN` was absent from the Codex process environment. No
provider, Gemini, Supabase, Sentry, or Render request was made. Final compile,
test, dependency, diff, diagnostic-guard, and secret-scan results are recorded
after the final validation pass below.

```bash
/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python -m unittest -q \
  tests.test_file_runtime_config tests.test_external_resolver_config \
  tests.test_media_resolver_adapters tests.test_tiktok_short_link \
  tests.test_external_queue_routing tests.test_metadata_preview_no_cookies \
  tests.test_provider_http_client tests.test_external_media_security \
  tests.test_external_media_processing tests.test_sentry_observability \
  tests.test_queue_claim tests.test_queue_failure_contract \
  tests.test_queue_gemini_guard tests.test_tiktok_safe_diagnostics \
  tests.test_tiktok_source_context
# 109 focused tests passed, exit 0

/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python \
  -m unittest discover -s tests -p "test_*.py"
# 146 tests passed, 0 failures, 0 errors, 0 skipped, exit 0

env -u TIKTOK_AKNG_ACCESS_TOKEN APP_ENV=development \
  ALLOW_LIVE_TIKTOK_DIAGNOSTIC=true \
  /private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python \
  scripts/check_tiktok_akng_end_to_end.py --input-kind numeric_id
# refused before input/provider work as designed: token not configured; exit 1

/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python -m compileall app scripts
/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python -m pip check
git diff --check
# passed on develop; generic credential, signed-URL, and live-response-marker
# scans each found zero files
```

The owner later verified AKNG manually with numeric-ID input. Its attached
response was inspected only through a `jq` shape/count projection: complete
`data.itemInfo.itemStruct.video`, 6 video entries, 0 separate-audio entries,
and 22 aggregate download URLs. No ID, token, URL, response body, or response
file was printed or committed, and Codex made no provider call.

## M23.W3D-TK3B3 Python/curl parity final offline validation

```bash
/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python --version
# Python 3.12.13

/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python -m compileall app scripts

/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python -m unittest -q \
  tests.test_file_runtime_config tests.test_external_resolver_config \
  tests.test_media_resolver_adapters tests.test_tiktok_short_link \
  tests.test_tiktok_candidate_fallback tests.test_tiktok_live_diagnostic \
  tests.test_external_queue_routing tests.test_metadata_preview_no_cookies \
  tests.test_provider_http_client tests.test_external_media_security \
  tests.test_external_media_processing tests.test_sentry_observability \
  tests.test_queue_claim tests.test_queue_failure_contract \
  tests.test_queue_gemini_guard tests.test_tiktok_safe_diagnostics \
  tests.test_tiktok_source_context
# 119 passed, 0 failures, 0 errors, exit 0

/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python \
  -m unittest discover -s tests -p "test_*.py"
# 156 passed, 0 failures, 0 errors, 0 skipped, exit 0

/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python -m pip check
git diff --check
# passed; no broken requirements

env -u TIKTOK_AKNG_ACCESS_TOKEN APP_ENV=development \
  ALLOW_LIVE_TIKTOK_DIAGNOSTIC=true \
  /private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python \
  scripts/check_tiktok_akng_end_to_end.py --input-kind numeric_id
# failed closed before prompt/provider work, failure_stage=input_validation,
# provider_call_attempted=false, cleanup_succeeded=true, exit 1
```

The live numeric-ID and short-link commands were intentionally not executed by
Codex. No provider, Gemini, Supabase, Sentry, or Render call was made.

## M23.W3D-TK3B3 live root-cause repair and final validation

- Live short-link-only reproduction first failed at
  `short_link_redirect_validation`: the zero-length 3xx response had no retained
  urllib3 socket, so peer discovery returned no value. A DNS-pinned TLS GET then
  passed with one redirect and resolved both canonical URL and numeric ID.
- The local machine lacked ffprobe/ffmpeg. `brew install --yes ffmpeg` installed
  ffmpeg/ffprobe 8.1.2; the Docker image already installs ffmpeg.
- Live AKNG numeric-ID responses returned 2xx, a complete six-video contract,
  zero separate-audio candidates, and three compatible media candidates.
- Safe media probes showed AKNG proxy alternatives were HTTPS/public-peer
  verified, 2xx video responses with valid initial MP4 bytes. They ignored
  Range, omitted Content-Length, and used variable-speed chunked transfer.
- The approximately 6.7 MB H.264 stream did not complete inside the bounded
  budget. The approximately 4.7 MB H.264 stream completed in the 60–90 second
  bucket, passed signature validation, and ffprobe proved video plus embedded
  audio. TikTok selection now uses this evidence-backed smaller-first policy and
  throughput-ranks URL alternatives.
- Final explicit `numeric_id` and `short_url` shared-path diagnostics both
  exited zero with `provider_status_class=2xx`, complete contract, six raw/three
  eligible candidates, H.264 MP4 selection, verified download/signature/video/
  audio, and cleanup. Inputs, token, response bodies, URLs, IDs, and temp paths
  were not emitted or persisted.

```bash
/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python --version
# Python 3.12.13

/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python -m compileall app scripts
/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python \
  -m unittest discover -s tests -p "test_*.py"
# 162 passed, 0 failures, 0 errors, 0 skipped, exit 0

/private/tmp/recipe-dost-m23w3d-tk.LnFxdz/venv/bin/python -m pip check
git diff --check
# passed; no broken requirements
```

No Gemini, Supabase, Sentry, Render, deployment, main merge, mobile, schema,
cookie, entitlement, or quota operation occurred.

## M23.W3I-IG2 Instagram split-AV compatibility repair

```bash
PATH="$PWD/.venv/bin:$PATH" python -m compileall app scripts
PATH="$PWD/.venv/bin:$PATH" python -m unittest discover -s tests -p "test_*.py"
PATH="$PWD/.venv/bin:$PATH" python -m pip check
git diff --check
# Python 3.12.13; compile passed; 175 tests passed with 0 failures/errors;
# no broken requirements; diff check passed.
```

Focused tests generated only synthetic local H.264/AAC and incompatible MPEG-4
media with ffmpeg, then exercised the shared ffprobe/mux path. The owner-only
Instagram diagnostic was compile-tested and unit-tested but deliberately not
executed. No live provider/media URL, Gemini, Supabase, Sentry, Render,
deployment, main merge, production worker, mobile, schema, cookie, entitlement,
quota, secret read, or secret output occurred.

## M23.W6A-W6C deterministic replay and Gemini reliability

```bash
PATH="$PWD/.venv/bin:$PATH" python scripts/worker_qa.py batch \
  --manifest tests/fixtures/worker_qa/mixed_batch_manifest.json
# passed: 7/7 terminal, 5 completed, 2 deliberate failures, FIFO, concurrency 1,
# cleanup true for all, active reservations 0, success ratio 0.7143

PATH="$PWD/.venv/bin:$PATH" python -m unittest -q \
  tests.test_gemini_reliability tests.test_worker_qa \
  tests.test_queue_gemini_guard
# passed: 14 tests
```

No live mode, provider, Gemini, Supabase, Sentry, Render, deployment, main
merge, mobile, schema, entitlement-limit, concurrency, or secret change was
performed.

Final validation:

```bash
PATH="$PWD/.venv/bin:$PATH" python -m compileall app scripts
PATH="$PWD/.venv/bin:$PATH" python -m unittest discover -s tests -p "test_*.py"
PATH="$PWD/.venv/bin:$PATH" python -m pip check
PATH="$PWD/.venv/bin:$PATH" python scripts/worker_qa.py batch \
  --manifest tests/fixtures/worker_qa/replay_manifest.json
PATH="$PWD/.venv/bin:$PATH" python scripts/worker_qa.py batch \
  --manifest tests/fixtures/worker_qa/mixed_batch_manifest.json
git diff --check
# passed: compile; 188 tests; pip check; 11-case replay; 7-case mixed batch;
# diff check. Live mode also refused without owner credentials as designed.
```
