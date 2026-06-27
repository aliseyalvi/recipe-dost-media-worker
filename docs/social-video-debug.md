# Social Video Debug Notes

## Current Status

- YouTube worker pipeline works.
- Instagram works through self-hosted Cobalt.
- TikTok failed on Cloud Run through yt-dlp and Cobalt.
- Local Mac yt-dlp can resolve TikTok with TikTok cookies and returns playable MP4 entries in `formats[]`.
- M14G-4R1 parser fix is implemented: direct MP4 selection, selected `http_headers`, bounded in-memory stream, sanitized debug output.
- M14G-4SOC1 unified routing: YouTube native, TikTok TikWM, Instagram/Facebook Cobalt.
- W-M14G-4SOC2A addresses the next blocker: full TikTok jobs reached `gemini_inline_video_started`, `video_observation_started`, and `gemini_api_call_started`, then stayed at `status=analyzing progress=60`. TikWM and Cobalt stream resolution are validated; Gemini observation completion/timeout handling is the current proving point.
- W-M14G-4SOC2B fixes invalid `media_jobs.status` usage. Detailed stages now use progress and events while DB status stays one of `pending`, `resolving`, `downloading`, `analyzing`, `completed`, or `failed`.
- W-M14G-4SOC2C adds stable social analysis mode. TikTok/TikWM defaults to text-first caption/title analysis and skips stream download/video Gemini when a usable title is present. Social video Gemini is disabled by default and, when enabled, has a whole-call watchdog.

## TikTok / TikWM Finding

TikWM was validated outside Cloud Run in Google Colab:

```text
POST https://www.tikwm.com/api/
form: url=<tiktok_url>, hd=1
success: code=0, data.cover, data.title, data.hdplay or data.play
stream: requests.get(video_url, stream=True) into BytesIO worked
```

TikWM debug was proven from Cloud Run. TikTok full jobs now use TikWM; Instagram/Facebook use Cobalt. Cobalt failed TikTok and is not used for TikTok. Cobalt YouTube is not needed because YouTube has the native Gemini path.

## Debug Safety Rules

- Debug endpoints require `X-Worker-Secret`.
- Never return or log source URLs, stream URLs, full provider JSON, cookies, auth headers, or raw bytes.
- Probe streams only up to the configured byte limit.
- Do not call Gemini or write DB rows during TikWM debug.
- TikWM/Cobalt stream URLs are temporary internal values only.
- No Gemini prompt, raw response, recipe output, ingredient list, step list, raw video bytes, source URL, or stream URL should appear in logs/events.
- Raw social video is kept in memory only. With `GEMINI_SOCIAL_UPLOAD_MODE=files_api`, the worker uploads the in-memory file to Gemini and deletes the Gemini file best-effort after analysis.
- If resolver metadata has a known duration above `MAX_VIDEO_DURATION_SECONDS`, the worker records `video_duration_rejected` and fails with `video_duration_exceeded` before `social_stream_started` or any Gemini event.
- TikTok text-first may run even when duration is over the video limit because it does not upload video; the event metadata marks `video_duration_exceeded=true`.

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

Failure codes include: `invalid_tiktok_url`, `tikwm_disabled`, `tikwm_api_http_error`, `tikwm_api_failed`, `tikwm_invalid_response`, `tikwm_stream_url_missing`, `cobalt_resolver_failed`, `cobalt_picker_not_supported`, `cobalt_local_processing_not_supported`, `social_resolver_unavailable`, `unsupported_social_platform`, `social_caption_missing`, `insufficient_recipe_details`, `social_video_gemini_disabled`, `video_duration_exceeded`, `social_stream_forbidden`, `social_stream_expired`, `media_size_exceeded`, `media_memory_limit_exceeded`, `media_unsupported_type`, `media_download_failed`, `gemini_video_timeout`, `gemini_video_bytes_failed`.

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

Full test plan:

- TikTok text-first: expect `tikwm_resolver_completed`, `social_text_analysis_started`, `gemini_api_call_started`, `social_text_analysis_completed`, `recipe_card_saved`, `completed`; or `insufficient_recipe_details`.
- TikTok video fallback only runs when caption/title is missing or too short and `ENABLE_SOCIAL_VIDEO_GEMINI=true`.
- Instagram: with `ENABLE_SOCIAL_VIDEO_GEMINI=true`, expect `cobalt_resolver_completed`, `social_stream_completed`, `gemini_video_call_started`, video analysis events, `recipe_card_saved`, `completed`; or `gemini_video_timeout`.
- Facebook: complete if Cobalt supports the URL; otherwise controlled Cobalt error.
- YouTube: unchanged native path.
- Progress should move through analyzing progress 60 for TikTok text analysis, 85 for recipe-card saving, and `completed` 100. Video mode uses analyzing progress 50 for Gemini video start, 60 for video observation, 75 for recipe normalization, and must fail with `gemini_video_timeout` instead of staying at 50.
- Unknown duration should continue to stream and rely on `Content-Length`/memory guards.
