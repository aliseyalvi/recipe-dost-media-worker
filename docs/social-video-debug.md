# Social Video Debug Notes

## Current Status

- YouTube worker pipeline works.
- Instagram works through self-hosted Cobalt.
- TikTok failed on Cloud Run through yt-dlp and Cobalt.
- Local Mac yt-dlp can resolve TikTok with TikTok cookies and returns playable MP4 entries in `formats[]`.
- M14G-4R1 parser fix is implemented: direct MP4 selection, selected `http_headers`, bounded in-memory stream, sanitized debug output.
- M14G-4SOC1 unified routing: YouTube native, TikTok TikWM, Instagram/Facebook Cobalt.

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

Failure codes include: `invalid_tiktok_url`, `tikwm_disabled`, `tikwm_api_http_error`, `tikwm_api_failed`, `tikwm_invalid_response`, `tikwm_stream_url_missing`, `cobalt_resolver_failed`, `cobalt_picker_not_supported`, `cobalt_local_processing_not_supported`, `social_resolver_unavailable`, `unsupported_social_platform`, `social_stream_forbidden`, `social_stream_expired`, `media_memory_limit_exceeded`, `media_unsupported_type`, `media_download_failed`, `gemini_video_bytes_failed`.

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

- TikTok: expect `tikwm_resolver_completed`, `social_stream_completed`, `video_observation_started`, `recipe_normalization_started`, `recipe_card_saved`, `completed`.
- Instagram: expect `cobalt_resolver_completed`, `social_stream_completed`, `video_observation_started`, `recipe_normalization_started`, `recipe_card_saved`, `completed`.
- Facebook: complete if Cobalt supports the URL; otherwise controlled Cobalt error.
- YouTube: unchanged native path.
