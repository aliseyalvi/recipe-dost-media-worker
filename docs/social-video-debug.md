# Social Video Debug Notes

## Current Status

- YouTube worker pipeline works.
- Instagram works through self-hosted Cobalt.
- TikTok failed on Cloud Run through yt-dlp and Cobalt.
- Local Mac yt-dlp can resolve TikTok with TikTok cookies and returns playable MP4 entries in `formats[]`.
- M14G-4R1 parser fix is implemented: direct MP4 selection, selected `http_headers`, bounded in-memory stream, sanitized debug output.

## TikTok / TikWM Finding

TikWM was validated outside Cloud Run in Google Colab:

```text
POST https://www.tikwm.com/api/
form: url=<tiktok_url>, hd=1
success: code=0, data.cover, data.title, data.hdplay or data.play
stream: requests.get(video_url, stream=True) into BytesIO worked
```

M14G-4TWM0 adds the protected debug endpoint. M14G-4TWM1 should prove the same metadata and first-byte stream probe work from Cloud Run.

## Debug Safety Rules

- Debug endpoints require `X-Worker-Secret`.
- Never return or log source URLs, stream URLs, full provider JSON, cookies, auth headers, or raw bytes.
- Probe streams only up to the configured byte limit.
- Do not call Gemini or write DB rows during TikWM debug.
- Do not integrate TikWM into media jobs until Cloud Run debug passes.

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

Failure codes to use: `invalid_tiktok_url`, `tikwm_disabled`, `tikwm_api_http_error`, `tikwm_api_failed`, `tikwm_invalid_response`, `tikwm_stream_url_missing`, `tikwm_stream_forbidden`, `tikwm_stream_expired`, `tikwm_stream_probe_failed`.

## Local Request

```bash
curl -X POST http://localhost:8080/debug/tikwm \
  -H "Content-Type: application/json" \
  -H "X-Worker-Secret: replace-with-your-worker-secret" \
  -d '{"url":"https://www.tiktok.com/@example/video/0000000000000000000","test_stream":true}'
```
