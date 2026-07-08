# Social Video Debug Notes

## Current Status

- YouTube worker pipeline works.
- Instagram works through self-hosted Cobalt.
- TikTok failed on Cloud Run through yt-dlp and Cobalt.
- Local Mac yt-dlp can resolve TikTok with TikTok cookies and returns playable MP4 entries in `formats[]`.
- M14G-4R1 parser fix is implemented: direct MP4 selection, selected `http_headers`, bounded in-memory stream, sanitized debug output.
- M14G-4SOC1 unified routing: YouTube native, TikTok TikWM, Instagram/Facebook Cobalt.
- W-M14G-4SOC2A addresses the next blocker: full TikTok jobs reached `gemini_inline_video_started`, `video_observation_started`, and `gemini_api_call_started`, then stayed at `status=analyzing progress=60`. TikWM and Cobalt stream resolution are validated; Gemini observation completion/timeout handling is the current proving point.
- W-M14G-4SOC2B/W-M14R1A fixes invalid `media_jobs.status` usage. Detailed stages now use progress and events while DB status stays one of `queued`, `resolving`, `downloading`, `analyzing`, `completed`, or `failed`.
- W-M14G-4SOC2C adds stable social analysis mode. TikTok/TikWM defaults to text-first caption/title analysis and skips stream download/video Gemini when a usable title is present. Social video Gemini is disabled by default and, when enabled, has a whole-call watchdog.
- W-M14R1 moves `/process-job` to FastAPI BackgroundTasks for Render Free testing. The HTTP request returns quickly; the background task must end every job as `completed` or `failed`.
- W-M14R1A accepts `queued` jobs from `create-media-job` manual mode and acquires them as `resolving`.
- W-M14R1B routes TikTok jobs by `media_jobs.platform` into TikWM text-first before the old YouTube-only unsupported branch.
- W-M14R1 keeps TikTok strictly text-first for the Render POC: TikWM metadata, duration guard, thumbnail upload, one Gemini text call, recipe card save. TikTok video bytes are not streamed in this milestone.
- W-M14R1C replaces the failing Gemini SDK helper with a direct synchronous REST call (`requests.post`) to `generateContent`. The call uses `timeout=45`, JSON generation config (`temperature=0.2`, `responseMimeType=application/json`), and explicit error handling for timeouts, rate limits (429), API key missing, and JSON parse failures. The caption prompt is a module-level constant `CAPTION_RECIPE_PROMPT` separate from the SDK's `SOCIAL_TEXT_PROMPT`. TikTok text-first now emits `gemini_api_call_started` and `gemini_api_call_completed` (or `gemini_api_call_failed`) events.
- W-M15A adds TikTok video-to-Gemini fallback: when `ENABLE_SOCIAL_VIDEO_GEMINI=true` and text-first returns `insufficient_recipe_details`, the worker falls back to video analysis. Video is downloaded from the TikWM internal URL to a temp file, uploaded to Gemini Files API via REST, and analyzed via `generateContent` with `fileData`. Temp files are cleaned up in `finally`. Gemini uploaded files are deleted best-effort. New events: `social_text_insufficient_video_fallback_started`, `social_video_download_started`/`completed`/`failed`, `gemini_video_upload_started`/`completed`/`failed`, `gemini_video_file_active`/`deleted`, `gemini_video_analysis_started`/`completed`/`failed`, `temp_video_deleted`.
- W-M15D0 adds `AI_ANALYSIS_MODE=mock_fixture` for quota-free TikTok development testing. TikWM resolution, duration guard, thumbnail upload, Supabase job updates, and recipe-card save stay real. Gemini text/video calls and Gemini quota guards are skipped. Expected mock events are `mock_ai_analysis_started` and `mock_ai_analysis_completed`; final job status should be `completed` progress `100` with `result_recipe_card_id`.
- W-M15D1 saves resolver metadata after TikWM completes. Public `source_metadata` is app-visible and contains title/description/duration/thumbnail/media flags only. Private `media_job_private_metadata` stores the CDN URL and raw resolver metadata. Metadata write failures emit `source_metadata_update_failed` or `private_metadata_save_failed` and do not fail the recipe job.
- W-M15D2 adds the mock-mode resolver matrix without Cobalt. TikTok keeps TikWM. YouTube uses yt-dlp metadata with no download. Instagram and Facebook use shallow experimental SnapInsta/SnapSave adapters behind disabled-by-default Render flags. Successful mock-mode resolver jobs save public/private metadata, upload thumbnails when available, save the mock fixture recipe, and complete without Gemini events.
- W-M16Q-POC adds `WORKER_EXECUTION_MODE=queue`. In queue mode the FastAPI startup task polls the oldest `queued` row, claims one job as `resolving`, uses unified `yt-dlp` extraction to create either a temp file or memory buffer, saves public/private metadata, uploads a thumbnail when available, saves the mock fixture recipe, and deletes temp media before polling again. The `/process-job` webhook path remains unchanged outside queue mode.
- W-M16Q-POC post-extraction fix: production proved `unified_extractor_completed`, so queue mode now reuses the webhook mock fixture recipe-card save helper after metadata persistence. Optional media-job source backfill failures no longer block mock analysis, and terminal failures report the real stage instead of defaulting to `unified_extractor_failed`.
- W-M16Q-POC-C1 adds cookie-aware queue extraction and metadata-only fallback. Unified yt-dlp first runs without cookies, retries with platform cookies when configured, emits `unified_extractor_video_verified` only when temp file/buffer bytes exist, and can complete mock jobs from OpenGraph metadata with `resolution_mode=metadata_only` when video bytes are unavailable.
- W-M16G-POC adds real Gemini video analysis behind `AI_ANALYSIS_MODE=gemini_video_poc`. It requires `video_bytes_verified=true` and an existing temp file, uploads that temp file to Gemini, validates strict recipe JSON, saves the recipe card, marks `ai_provider=gemini`, and still deletes temp media in cleanup.
- M20.5 production cutover cleanup confirms `feature/queue-ytdlp-poc` is ready to merge to `main`. Production should run from `main` in `WORKER_EXECUTION_MODE=queue`; `/wake` keeps the queue loop active, `/ping` and `/health` remain simple probes, and `/process-job` is legacy/manual/debug only.
- M22D.5A production extractor config parity fixes the post-env-cleanup Instagram failure. Production config explicitly enables queue mode, unified yt-dlp temp-file extraction, Instagram resolver gating, and Instagram yt-dlp cookies. Cookie values remain env-only through `INSTAGRAM_COOKIES_B64`; when cookie mode is enabled but cookies are missing, the queue extractor fails safely with `instagram_cookies_missing`.
- M22D.5B adds `/health.safe_config` and startup safe config logs for non-secret runtime debugging. YouTube production smoke passed. Instagram failures with `instagram_resolver_disabled` mean effective runtime config or deployed code still has the Instagram gate disabled. TikTok failures after `tikwm_resolver_started` were traced to a missing `resolver_started_at` assignment in the TikWM text-first path; unexpected resolver exceptions now emit safe `tikwm_resolver_exception` metadata.
- M22D.5C audit confirms the observed TikWM and disabled-Instagram events come from the legacy `/process-job` background path. Intended production social AI jobs should use queue mode: YouTube through Gemini URL, TikTok/Instagram through unified yt-dlp temp-file extraction. Metadata preview remains a separate metadata-only path and should not be used as proof of AI job resolver parity.
- M22D.5D changes `/process-job` in queue mode into a queue wake compatibility endpoint. It records `process_job_queue_wake_requested`, starts the queue loop, returns `202`, and does not schedule legacy `JobRunner.run()`. Normal production TikTok/Instagram jobs should now emit `queue_job_claimed` and `unified_extractor_started`, not `tikwm_resolver_started` or `instagram_resolver_disabled`.
- M22D.5E fixes the config loading layer beneath the resolver path. Production `/health` showed queue-wake code but fallback config values because Docker did not copy `config/`; Render env was clean. After redeploy, first verify `/health.safe_config.config_source.loaded_files` contains `worker.defaults.json` and `worker.production.json` before social smoke tests.
- M22D.5F completes production worker smoke testing. `/health` confirmed production config was loaded, queue wake compatibility was active, unified extractor/cookie flags were enabled, raw URL logging stayed false, and max duration stayed 120 seconds. Production YouTube, Instagram, and TikTok AI recipe generation all passed end to end.

## TikTok / TikWM Finding

TikWM was validated outside Cloud Run in Google Colab:

```text
POST https://www.tikwm.com/api/
form: url=<tiktok_url>, hd=1
success: code=0, data.cover, data.title, data.hdplay or data.play
stream: requests.get(video_url, stream=True) into BytesIO worked
```

TikWM debug was proven from Cloud Run. TikTok full jobs now use TikWM text-first only and should emit `tikwm_resolver_started` instead of the old YouTube-only unsupported failure. Instagram/Facebook are disabled for the Render POC until separately validated. Cobalt failed TikTok and is not used for TikTok. Cobalt YouTube is not needed because YouTube has the native Gemini path.

## Debug Safety Rules

- Debug endpoints require `X-Worker-Secret`.
- Never return or log source URLs, stream URLs, full provider JSON, cookies, auth headers, or raw bytes.
- Probe streams only up to the configured byte limit.
- Do not call Gemini or write DB rows during TikWM debug.
- TikWM/Cobalt stream URLs are temporary internal values only.
- No Gemini prompt, raw response, recipe output, ingredient list, step list, raw video bytes, source URL, or stream URL should appear in logs/events.
- Raw social video is kept in memory only for non-TikTok video-mode tests. With `GEMINI_SOCIAL_UPLOAD_MODE=files_api`, the worker uploads the in-memory file to Gemini and deletes the Gemini file best-effort after analysis.
- If TikWM metadata has a known duration above `MAX_VIDEO_DURATION_SECONDS`, the worker records `video_too_long` and fails with `video_duration_exceeded` before Gemini.
- TikTok text-first does not store or log the TikWM CDN URL and does not stream TikTok video bytes in W-M14R1.
- TikTok mock fixture mode uses `MOCK_VIDEO_PROCESSING_MODE=skip_video_download` by default, so no social video bytes are downloaded. `download_then_mock` may be used to exercise existing temp-file guards; the temp file is deleted before fixture analysis.
- CDN URLs must exist only in `media_job_private_metadata`; public source metadata and events may contain only boolean CDN flags.
- SnapInsta/SnapSave parsing is intentionally minimal: JSON first, then obvious OpenGraph/direct media links only. Unreliable pages should fail controlled with `instagram_resolver_failed` or `facebook_resolver_failed`.
- The old "Automatic video analysis is currently available for YouTube" fallback is only for truly unsupported platforms and must not catch TikTok.
- Queue mock-fixture mode does not call Gemini. Production queue Gemini mode uses Gemini only after YouTube native URL input or verified temp-file extraction. Expected extractor failures should become terminal `failed` jobs with `video_duration_exceeded`, `media_size_exceeded`, or `unified_extractor_failed`, not `worker_error` or `background_worker_error`.
- Post-extraction queue failures should use `private_metadata_save_failed`, `source_metadata_update_failed`, `mock_fixture_failed`, `recipe_card_insert_failed`, or `job_completion_update_failed` with sanitized `stage`, `exception_type`, `error_code`, and `safe_error_message` metadata only.
- Cookie retry events must not include cookie contents or file paths. Public `source_metadata.media` may include only `video_bytes_verified`, `output_mode`, and `cookie_mode`; CDN URLs remain private-only and OpenGraph fallback does not store `og:video` URLs.
- Gemini video POC events may include only platform, resolver, model, duration, size, `video_bytes_verified`, stage, error code, and exception type. Do not include source URLs, CDN URLs, cookie paths, temp paths, prompts, raw Gemini responses, or raw bytes.
- Gemini Files API queue mode uses only the verified temp file returned by unified extraction. Expected file stages are upload started/completed, processing started, file active, brief started/completed, JSON started/completed, recipe save started/saved, queue completed, and temp cleanup. Controlled Gemini/file failures use `video_required_for_ai`, `gemini_api_key_missing`, `gemini_file_upload_failed`, `gemini_file_processing_failed`, `gemini_file_processing_timeout`, `gemini_video_brief_empty`, `gemini_video_brief_failed`, `gemini_recipe_json_empty`, `gemini_recipe_json_invalid`, or `recipe_card_insert_failed`.
- Two-step Gemini queue mode replaces direct video JSON with `gemini_video_brief_started/completed`, then `gemini_recipe_json_started/completed`. Empty brief failures use `gemini_video_brief_empty` with safe candidate/parts/finish diagnostics.
- If brief-to-JSON fails after retry/fallback model attempts, queue mode emits `gemini_recipe_json_failed`, `brief_card_build_started`, `gemini_recipe_json_fallback_used`, and `brief_card_build_completed`, then saves a minimal recipe card from the brief.
- Render Free wakeups use `GET /wake`; it returns queue status and starts the queue loop when needed, but never claims a job inline. YouTube queue Gemini with `YOUTUBE_AI_INPUT_MODE=gemini_url` skips yt-dlp, saves oEmbed metadata when available, and sends only the YouTube URL to Gemini for the brief.
- M18 normalization emits `recipe_card_normalized` before save and ensures queue Gemini cards have clean summary text, structured ingredients, numbered steps, derived grocery items, and no tag-only values in missing info. Private metadata save failures emit `private_metadata_save_failed` but do not stop jobs when allowed.
- Queue Gemini token controls now keep video briefs concise, cap brief and JSON output tokens, and truncate oversized briefs before the text JSON call. The `/health` response reports the active caps for Render verification.
- M20/M22B single-pass mode emits `gemini_structured_extraction_started/completed/failed`; configured fallback emits `gemini_structured_extraction_fallback_started/completed/failed`. Weak or malformed canonical JSON can emit `recipe_card_repair_started/completed/failed`; terminal structured failures use `gemini_structured_extraction_failed` with safe `failure_category` metadata. Repair uses safe source title/platform, validation errors, and invalid JSON only, never video again.
- M20.5 production checks: `WORKER_EXECUTION_MODE=queue` is the primary flow; `YOUTUBE_AI_INPUT_MODE=gemini_url` keeps YouTube off yt-dlp; TikTok/Instagram require verified temp files before Gemini Files API; `GEMINI_EXTRACTION_MODE=single_pass_structured`, `GEMINI_PRIMARY_MODEL=gemini-3.1-flash-lite`, and `LOCAL_BRIEF_PARSER_ENABLED=false` are the documented defaults.
- M21.7A metadata preview checks: `POST /metadata-preview` is worker-secret protected, returns safe metadata only, uses YouTube oEmbed first, uses yt-dlp only with `download=False`, falls back to OpenGraph for social/other pages, and should never emit or return direct video/CDN URLs, cookie data, temp paths, prompts, raw responses, file references, or bytes.
- M21.9B thumbnail checks: preview and full jobs use the same metadata-first thumbnail selector. Expected safe event/source fields are `thumbnail_source` (`platform_thumbnail`, `ytdlp_thumbnail`, `ytdlp_thumbnails_array`, `sampled_frame`, or `fallback`) and `thumbnail_selected`; no thumbnail URL, video/CDN URL, cookie path, temp path, or provider payload should appear in events. Frame sampling is attempted only when no metadata thumbnail exists and `ffmpeg` is available.
- M21.9B.1B thumbnail checks: expected source labels are platform-specific (`youtube_oembed`, `youtube_ytdlp_thumbnail`, `youtube_ytdlp_array`, `youtube_deterministic`, `instagram_ytdlp_thumbnail`, `instagram_ytdlp_array`, `tiktok_ytdlp_thumbnail`, `tiktok_ytdlp_array`, `early_frame_fallback`, `existing_platform_thumbnail`, `placeholder`). Full jobs should emit `thumbnail_selected` with `will_update_recipe_item_thumbnail=false` when an existing platform thumbnail would be downgraded to an early-frame fallback.
- M22D.0B env checks: validate `env/development.env` and `env/production.env` with `scripts/validate_env.py`; export temporary Render upload files with `scripts/export_render_env.py`. Validation/output must mask values and never print service-role keys, worker shared secrets, Gemini keys, cookies, raw URLs, CDN URLs, prompts, or Gemini raw responses.
- M22D.0C env audit: clean templates intentionally omit TikWM, Cobalt, RapidAPI, cookie, and other experimental resolver settings. Those settings remain code-supported only for legacy/debug paths and should not be migrated from old env files unless deliberately re-enabled.
- M22D.0D config rationalization: non-secret runtime settings now load from committed JSON config first, while old Render env values still override config. Keep existing Render env group during Phase A/B verification; remove deprecated non-secret keys only after queue behavior is validated.

## Expected `/debug/tikwm` Shape

Success should report booleans and selected metadata only:

```json
{
  "ok": true,
  "platform": "tiktok",
  "resolver": "tikwm",
  "has_video_url": true,
  "has_thumbnail": true,
  "title_present": true,
  "video_id_present": true,
  "duration_seconds": null,
  "selected_ext": "mp4",
  "stream_probe": {
    "status_code": 200,
    "content_type": "video/mp4",
    "bytes_read": 262144,
    "accepted": true
  }
}
```

Failure codes include: `invalid_tiktok_url`, `tikwm_disabled`, `tikwm_api_http_error`, `tikwm_api_failed`, `tikwm_invalid_response`, `tikwm_stream_url_missing`, `instagram_resolver_disabled`, `instagram_cookies_missing`, `facebook_resolver_disabled`, `cobalt_resolver_failed`, `cobalt_picker_not_supported`, `cobalt_local_processing_not_supported`, `social_resolver_unavailable`, `unsupported_social_platform`, `social_caption_missing`, `insufficient_recipe_details`, `social_video_gemini_disabled`, `video_duration_exceeded`, `social_stream_forbidden`, `social_stream_expired`, `media_size_exceeded`, `media_memory_limit_exceeded`, `media_unsupported_type`, `media_download_failed`, `gemini_text_timeout`, `gemini_video_timeout`, `gemini_video_bytes_failed`.

## Local Request

```bash
curl -X POST http://localhost:8080/debug/tikwm \
  -H "Content-Type: application/json" \
  -H "X-Worker-Secret: replace-with-your-worker-secret" \
  -d '{"url":"https://www.tiktok.com/@example/video/0000000000000000000","test_stream":true}'
```

Unified resolver debug:

```bash
curl -X POST http://localhost:8080/debug/social-resolve \
  -H "Content-Type: application/json" \
  -H "X-Worker-Secret: replace-with-your-worker-secret" \
  -d '{"url":"https://www.instagram.com/reel/example/","resolver":"auto","test_stream":true}'
```

Metadata preview without Gemini or video download:

```bash
curl -X POST http://localhost:8080/metadata-preview \
  -H "Content-Type: application/json" \
  -H "X-Worker-Secret: replace-with-your-worker-secret" \
  -d '{"url":"https://www.youtube.com/watch?v=00000000000","platform":"youtube"}'
```

Full test plan:

- TikTok text-first: expect `worker_started`, `tikwm_resolver_started`, `tikwm_resolver_completed`, `thumbnail_upload_started`, `thumbnail_uploaded` or `thumbnail_upload_failed`, `social_text_analysis_started`, `gemini_api_call_started`, `gemini_api_call_completed`, `social_text_analysis_completed`, `recipe_card_saved`, and `completed`; or a controlled failure.
- TikTok mock fixture: with `AI_ANALYSIS_MODE=mock_fixture`, expect `worker_started`, `tikwm_resolver_started`, `tikwm_resolver_completed`, `thumbnail_upload_started`, `thumbnail_uploaded` or `thumbnail_upload_failed`, `mock_ai_analysis_started`, `mock_ai_analysis_completed`, `recipe_card_saved`, and `completed`; no `gemini_api_call_started` event should be emitted.
- TikTok metadata persistence: expect `private_metadata_saved`, `source_metadata_update_started`, and `source_metadata_updated`; after thumbnail upload succeeds, `source_metadata_updated` may appear again with the uploaded thumbnail URL folded into public metadata.
- Queue mode mock fixture: with `WORKER_EXECUTION_MODE=queue` and `ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR=true`, expect `queue_loop_started`, `queue_job_claimed`, `unified_extractor_started`, `unified_extractor_completed`, `private_metadata_saved`, `source_metadata_updated`, optional `thumbnail_uploaded`, `mock_ai_analysis_started`, `mock_ai_analysis_completed`, `recipe_card_saved`, `queue_job_completed`, and `temp_video_deleted` when a temp file was created. No Gemini events should be emitted.
- Queue cookie retry: if first yt-dlp extraction fails and platform cookies are configured, expect `unified_extractor_retry_with_cookies`; if bytes are downloaded, expect `unified_extractor_video_verified` and `source_metadata.resolution_mode=local_file_ready`.
- Queue metadata fallback: in mock mode with fallback enabled, if yt-dlp and cookie retry cannot access video bytes, expect `metadata_fallback_started`, then `metadata_fallback_completed` and normal mock recipe completion when OpenGraph title/image exists, or `<platform>_metadata_unavailable` when OpenGraph metadata is unavailable.
- Queue Gemini single-pass: with `AI_ANALYSIS_MODE=gemini_video_poc`, expect `unified_extractor_video_verified`, `gemini_file_upload_started`, `gemini_file_upload_completed`, `gemini_file_processing_started`, `gemini_file_active`, `gemini_structured_extraction_started`, `gemini_structured_extraction_completed`, `recipe_card_saving_started`, `recipe_card_saved`, `queue_job_completed`, and `temp_video_deleted`.
- M22B.2D structured extraction failures: malformed, empty, missing-candidate, schema, and quality failures should emit safe `failure_category` metadata only. Repair may run once when `RECIPE_REPAIR_ENABLED=true`, fallback may run once when `GEMINI_FALLBACK_MODEL` is configured and different, and terminal failures should use `gemini_structured_extraction_failed` with safe `repair_attempted`, `fallback_attempted`, `fallback_model`, and user-safe message metadata.
- YouTube queue Gemini URL: expect `queue_job_claimed`, `youtube_metadata_started`, `youtube_metadata_completed`, optional `thumbnail_uploaded`, `private_metadata_saved`, `source_metadata_updated`, `gemini_structured_extraction_started`, `gemini_structured_extraction_completed`, `youtube_gemini_url_completed`, `recipe_card_saved`, and `queue_job_completed`; failures should use `youtube_gemini_url_failed`, not yt-dlp errors.
- Queue Gemini token caps: verify `/health` includes `gemini_video_brief_max_output_tokens`, `gemini_recipe_json_max_output_tokens`, and `gemini_recipe_brief_max_chars`; default brief/JSON events still report sanitized `text_length` only.
- M20/M22B single-pass health: verify `/health` includes `gemini_extraction_mode`, `gemini_primary_model`, `gemini_fallback_model`, `gemini_thinking_level`, `gemini_max_output_tokens`, `gemini_structured_output_enabled`, `local_brief_parser_enabled`, `recipe_quality_gate_enabled`, and `recipe_repair_enabled`. New TikTok/Instagram/YouTube recipe cards should have at least 2 ingredients, 3 steps, and 1 grocery item when recipe content is available; unrecoverable weak structured output should fail with `gemini_structured_extraction_failed` and a safe `failure_category`.
- M22D.5B safe config health: verify `/health.safe_config` includes `app_env`, queue/Gemini modes, enabled platform booleans, resolver names, unified extractor flag, Instagram/yt-dlp cookie booleans, cookie presence booleans, media extract mode, max duration/size, Gemini model names, `raw_url_logging_enabled=false`, and mock fallback booleans. It must not include cookie values, raw URLs, source URLs, stream URLs, prompts, raw Gemini responses, or payloads.
- M22D.5D process-job queue compatibility: in production queue mode, a trigger through `/process-job` should return `202`, emit `process_job_queue_wake_requested`, and then rely on the queue loop. The follow-on queue events for TikTok/Instagram should include `queue_job_claimed` and `unified_extractor_started`; `tikwm_resolver_started` is only expected for deliberate legacy/manual TikTok processing outside queue mode.
- M22D.5E config source health: verify `/health.safe_config.config_source.production_config_loaded=true`, `defaults_config_loaded=true`, `missing_files=[]`, `enable_unified_ytdlp_stream_extractor=true`, `enable_instagram_cookies=true`, `enable_ytdlp_cookies=true`, and `max_video_duration_seconds=120` before running Instagram/TikTok smoke jobs.
- M22D.5F production smoke completion: YouTube, Instagram, and TikTok production queue jobs have passed. Future social debugging should treat this queue/unified-extractor path as the known-good production path and keep TikWM as legacy/manual/debug only.
- M21.7A metadata preview: verify `/health` includes `metadata_preview_enabled`, `max_video_duration_seconds`, and `supported_metadata_platforms`; YouTube preview returns title/thumbnail through oEmbed; TikTok/Instagram/Facebook preview returns yt-dlp metadata when available or safe OpenGraph partial metadata; over-limit duration sets `is_too_long=true` but still returns metadata.
- M21.9B thumbnail quality: verify yt-dlp social metadata uses a valid direct `thumbnail` when present; if direct thumbnail looks like an avatar or is missing, verify the largest non-avatar candidate from `thumbnails[]` is selected. `thumbnail_uploaded`/`thumbnail_upload_failed` events should include only safe thumbnail source labels and selection booleans.
- M21.9B.1B no-downgrade: verify `/metadata-preview` includes `thumbnail_source`, `thumbnail_provider`, `thumbnail_strategy=platform_first_v2`, `thumbnail_candidate_count`, and `thumbnail_selected`; verify existing `i.ytimg.com`, TikTok CDN image, or Instagram CDN image thumbnails are not replaced by `early_frame_fallback`.
- M21.9B.2 YouTube Shorts thumbnail variants: when oEmbed returns numbered frame variants such as `hq1`, `hq2`, or `hq3`, verify deterministic YouTube candidates are tried first and selected as `youtube_deterministic` when validation succeeds; logs/events must include only safe candidate kind/status fields, not URLs.
- YouTube mock fixture: with `AI_ANALYSIS_MODE=mock_fixture`, expect `platform_resolver_started`, `platform_resolver_completed`, metadata events, optional thumbnail upload, `mock_ai_analysis_started`, `mock_ai_analysis_completed`, `recipe_card_saved`, and `completed`; no `gemini_api_call_started`.
- Instagram/Facebook mock fixture: with default flags, expect `instagram_resolver_disabled` or `facebook_resolver_disabled`; when enabled, expect completion only if SnapInsta/SnapSave returns parseable metadata/direct media.
- TikTok with missing/thin title fails with `social_caption_missing` before stream download.
- Long TikTok metadata fails with `video_duration_exceeded` and `video_too_long` before Gemini.
- Instagram production queue path: with `WORKER_EXECUTION_MODE=queue`, `ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR=true`, and `MEDIA_EXTRACT_OUTPUT_MODE=temp_file`, expect unified yt-dlp extraction before Gemini. If Instagram cookie mode is enabled but the private cookie env value is absent, expect `instagram_cookies_missing`. The legacy/manual resolver path may still fail with `instagram_resolver_disabled` when intentionally disabled.
- Facebook: with default Render POC config, expect `facebook_resolver_disabled`.
- YouTube: unchanged native path.
- Progress should move through analyzing progress 60 for TikTok text analysis, 85 for recipe-card saving, and `completed` 100. Video mode uses analyzing progress 50 for Gemini video start, 60 for video observation, 75 for recipe normalization, and must fail with `gemini_video_timeout` instead of staying at 50.
- Unknown duration may continue to stream only for explicitly enabled non-TikTok video-mode tests, relying on `Content-Length` and memory guards.
