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
