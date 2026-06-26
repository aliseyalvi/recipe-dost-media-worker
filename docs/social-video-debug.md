# Social Video Debug Notes

## Current TikTok finding

Cloud Run debug initially failed with:

- `yt_dlp_extract_timeout`
- then `Unexpected response from webpage request`

Local Mac test succeeded with TikTok cookies.

Successful local command output contained:

```json
{
  "id": "7599246984318373140",
  "formats": [
    {
      "ext": "mp4",
      "url": "https://v16-webapp-prime.tiktok.com/video/tos/...",
      "http_headers": {
        "User-Agent": "Mozilla/5.0...",
        "Referer": "https://www.tiktok.com/"
      }
    }
  ]
}