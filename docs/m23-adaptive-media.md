# M23.W2 Adaptive Media Processing

This policy applies only to the active serialized queue path for TikTok and
Instagram: queue claim -> unified yt-dlp extractor -> verified temporary file ->
Gemini Files API. YouTube's native Gemini URL path and all legacy/manual paths
are unchanged.

## M23.W2B direct-candidate regression addendum

The development regression occurred because the shared collector exposed one
top-level direct URL, while active candidate construction required a non-empty
`format_id` and explicit non-`none` audio/video codecs. Top-level normalization
also omitted those fields, and `requested_formats` was not read. Thus thumbnail
`candidate_count=1` and `has_cdn_url=true` coexisted with
`format_candidate_count=0` and zero adaptive attempts.

The active extractor now has one normalization layer, used identically with and
without platform cookies. Priority is `formats`, `requested_formats` (including
yt-dlp's `requested_downloads` compatibility shape), then `top_level_direct`.
Duplicates are removed in memory without logging or persisting candidate URLs.
Top-level candidates may omit format ID, filesize, duration, and codecs when an
HTTP(S) direct URL plus a supported extension or plausible dimensions exists.

Audio is tri-state: explicit codec/audio-extension/bitrate evidence is
`audio_present`; explicit `acodec=none` is `audio_absent`; absent or null audio
metadata is `audio_unknown`. Only explicit absence is rejected during candidate
construction. Unknown audio receives a hard-byte-guarded temporary download and
post-download probe before it can reach Gemini.

`ffprobe` and `ffmpeg` remain optional, bounded runtime tools. The supported
Docker image and the audited local runtime contain neither executable, and this
milestone does not add them. Therefore an unknown-audio direct candidate is now
correctly normalized and attempted, but the currently deployed runtime cannot
confirm its streams; it must fail safely after cleanup until the supported image
provides `ffprobe`. When available, probing records only stream-presence flags,
duration, and dimensions. A valid direct file above 36 MiB may use the bounded
ffmpeg optimization path (720-high H.264/AAC MP4, metadata stripped, output size
and streams rechecked); a file above 50 MiB is never fully downloaded to
transcode. If ffmpeg is absent, a verified file below 50 MiB remains usable.

Failure distinctions are unchanged at the app boundary: no candidate uses
`unified_extractor_failed`; explicit/post-probe no-audio also keeps that existing
code and safe copy; all candidates above 50 MiB use `media_size_exceeded`;
malformed probe/download/optimization failures are technical errors. Build 3
fields and quota behavior are unchanged.

Safe event/log telemetry adds `normalized_candidate_count`,
`candidate_source_type`, `audio_state`, attempt count, probe presence/results,
optimization presence/results, resolution/size buckets, and terminal state.
URLs, paths, codecs, media text, identifiers, and credentials remain excluded.
Terminal size failure keeps its grouped Sentry warning. During beta, a
discovered candidate ending in the existing unsupported-media outcome gets one
grouped `worker_outcome/no_supported_media` warning; intermediate and successful
fallbacks remain silent. Technical probe/download/optimization failures remain
single error exceptions.

Development verification: deploy from `develop`, confirm the new safe fields on
`unified_extractor_completed` or `unified_extractor_failed`, and use only an
authorized non-production link. For the incident shape, expect normalized and
format counts of one, source `top_level_direct`, audio `audio_unknown`, and at
least one adaptive attempt. Before expecting success, verify the image actually
contains `ffprobe`; otherwise the safe probe-unavailable outcome is expected.
Rollback remains redeploying the previous development revision or reverting the
W2B commit. No database, client, quota, or mobile rollback is needed.

## Audited runtime path

- Render starts `uvicorn app.main:app --host 0.0.0.0 --port $PORT
  --timeout-keep-alive 120` (`render.yaml`; Docker uses the same command with a
  `10000` port fallback).
- FastAPI startup or `/wake` starts `run_queue_loop`; the loop calls
  `process_next_queued_job`, then `process_queue_job` in `app/queue_worker.py`.
- The active social extractor is `app/social/stream_download.py`. It runs
  metadata-only yt-dlp first, builds combined audio/video candidates, and pins
  an exact format ID for each bounded download attempt.
- Queue concurrency is one in this process. The active social output mode is a
  temporary file, not an in-memory copy. The file and partials are removed
  between attempts and from the queue `finally` cleanup.

## Media policy map

All values named `*_MB` are historical configuration names but are normalized
as binary MiB (`value * 1,048,576` bytes). The Gemini limit is an exact byte
value because the provider documents decimal GB.

| Control | Current value | Source | Purpose/type | Risk and action |
|---|---:|---|---|---|
| Pre-download preferred target | 36 MiB | `PREFERRED_VIDEO_SIZE_MB`, defaults JSON; explicit production JSON | Soft | New. Rank useful formats below this target before download. |
| Streaming/temp-file hard maximum | 50 MiB (52,428,800 bytes) | `MAX_VIDEO_SIZE_MB`, defaults and production JSON | Hard | Raised modestly from 30 MiB only after provider/runtime audit. Abort once downloaded bytes exceed it and remove partials. |
| Active output mode | `temp_file` | `MEDIA_EXTRACT_OUTPUT_MODE`, production JSON | Hard architecture choice | Avoids holding the complete video in RAM. Ephemeral data is deleted. |
| Memory-buffer maximum | 35 MiB | `MAX_MEMORY_BUFFER_MB`, `app/config.py` | Hard, inactive production mode | Remains for the non-production output option; the active temp-file path does not use it. |
| Gemini Files upload maximum | 2,000,000,000 bytes (2 GB) | `GEMINI_FILE_MAX_SIZE_BYTES`, defaults JSON; Gemini Files documentation | Hard startup invariant | Worker hard limit must not exceed it. Current 50 MiB is far below it. |
| Source-video duration | 180 seconds | `MAX_SOURCE_VIDEO_DURATION_SECONDS`, defaults JSON; development/production inherit unless intentionally overridden | Hard | Allowed range is 60–900 seconds. Known over-limit media is rejected before download/Gemini. This value is separate from structured recipe duration. |
| Maximum useful video resolution | 720 short-edge pixels | `MAX_VIDEO_HEIGHT`, defaults/production JSON | Soft quality ceiling | Short edge is intentional for portrait media; avoids excluding 720x1280 as the old height-only selector did. |
| Minimum useful video resolution | 360 short-edge pixels | `MIN_VIDEO_HEIGHT`, defaults/production JSON | Hard acceptance floor | 360p is the bounded last quality; no `worst` selection. |
| Audio | Required | candidate filter in `stream_download.py` | Hard | Recipe speech/audio remains present. Audio-only and video-only candidates are rejected. |
| Video/audio bitrate | No fixed cap | yt-dlp `tbr`, or `vbr + abr` | Soft ranking/estimate | Estimate `duration * total bitrate * 1.05`; actual-byte guard is authoritative. |
| yt-dlp socket timeout | 15 seconds | `YTDLP_SOCKET_TIMEOUT_SECONDS`, `app/config.py` | Per socket operation | Existing value retained. There is no active unified-extractor whole-download deadline; monitor Render duration. |
| yt-dlp retries | 1 | `YTDLP_RETRIES`, `app/config.py` | Per candidate/request | Reused instead of the former hard-coded value. Adaptive format attempts are separate. |
| Adaptive format attempts | maximum 4 | `MEDIA_ADAPTIVE_MAX_ATTEMPTS`, defaults/production JSON | Hard bound | One queue job remains one AI/quota job. |
| Gemini upload request timeout | 120 seconds | `GEMINI_FILE_UPLOAD_TIMEOUT_SECONDS`, defaults JSON | Hard request timeout | Existing value retained. |
| Gemini file processing wait | 120 seconds, poll every 5 seconds | `GEMINI_FILE_MAX_WAIT_SECONDS`, `GEMINI_FILE_POLL_INTERVAL_SECONDS` | Hard bounded wait | Existing values retained. |
| Gemini observation timeout | 120 seconds | `GEMINI_VIDEO_OBSERVATION_TIMEOUT_SECONDS`, `app/config.py` | Hard model-call timeout | Existing quota/call guards remain before the provider call. |
| Uvicorn keep-alive | 120 seconds | `render.yaml`, `Dockerfile` | HTTP connection setting | Not a queue job deadline; queue work continues in the serialized loop. |

Related but inactive in the production queue flow: the legacy TikWM path has a
30 MiB `TIKWM_MAX_VIDEO_SIZE_MB` limit and 15-second resolver timeout; the
legacy `stream_video_to_memory` helper is capped by `MAX_MEMORY_BUFFER_MB=35`
MiB with a `(10, 120)` connect/read request timeout; legacy Gemini helpers use
one configured retry, 90-second file-upload/video-call timeouts, and 120-second
file-processing timeouts. These values were audited but not changed because the
milestone explicitly leaves legacy/manual execution untouched. Metadata debug
stream probing reads at most 256 KiB and is not part of AI media processing.

Configuration loading validates, at startup, positive ordered sizes, a hard
worker limit no greater than the provider limit, 1-4 attempts, and
`360 <= min resolution <= max resolution <= 720`. Malformed values fail
startup. `/health.safe_config` and the startup log expose only normalized safe
values, never the environment or secrets.

## Provider and Render constraints

- Gemini Files permits up to 2 GB per file, 20 GB per project, and stores files
  for 48 hours. Supported video types include MP4, QuickTime/MOV, WebM, AVI,
  MPEG, WMV, FLV, and 3GPP. The worker narrows downloads to combined audio/video
  MP4/M4V/MOV/WebM candidates compatible with the active path.
- Render Free documents 512 MB RAM and 0.1 CPU. Its filesystem is ephemeral,
  free services cannot attach persistent disks, and this worker processes one
  job at a time. Render does not document a fixed ephemeral-disk capacity for
  this service, so 50 MiB is a conservative worker safety cap rather than an
  assumed disk entitlement.
- Sources: [Gemini Files](https://ai.google.dev/gemini-api/docs/files),
  [Gemini video input](https://ai.google.dev/gemini-api/docs/video-understanding),
  [Render compute](https://render.com/docs/compute-plans), and
  [Render Free services](https://render.com/docs/free).

## Selection and adaptive attempts

Candidate size is determined in this order: exact `filesize`,
`filesize_approx`, duration times total bitrate plus 5% container overhead, then
bounded unknown size. Separate video/audio streams are not selected, so a merge
cannot silently add an unaccounted second stream.

The selector requires combined audio and video, a supported container, and a
360-720 short-edge resolution. Within each bucket it prefers resolution, MP4,
bitrate, capped FPS, then smaller size. Attempts follow 720p -> 540p -> 480p ->
360p, omitting unavailable buckets. A candidate estimated above the preferred
target is not chosen while a useful smaller candidate fits. If no candidate
fits the preferred target, one smallest useful candidate below the hard limit
may be tried under the streaming guard. Unknown-size media receives at most one
guarded final attempt. The old unrestricted `/best` fallback and `worst` are not
used.

The incident's 1080x1920 format was selected because the old yt-dlp expression
used `height<=720`. For portrait media, the 720p rendition is normally
720x1280, so it failed that test; the final unrestricted `/best` then allowed
1080x1920. The format discovered during metadata extraction was also not pinned
for download. The incident does not contain a saved candidate inventory, so it
cannot prove whether that specific URL exposed a smaller rendition. Regression
fixtures cover both a smaller 720p rendition and an all-candidates-too-large
case without containing production identifiers or URLs.

Actual downloaded bytes are checked by a yt-dlp progress hook and again from
the completed file. Exceeding the preferred target advances to the next smaller
candidate when present. Exceeding the hard limit aborts the attempt. Before each
attempt the job directory is cleared, and terminal cleanup removes all partials.
Cookie access is preserved, but a terminal size/duration result does not blindly
repeat the same download with cookies.

## Compression decision

Compression is deliberately not implemented. The supported Docker image does
not install ffmpeg, local runtime inspection found no ffmpeg executable, and
Render Free provides only 0.1 CPU. Source-format selection is cheaper and safer;
adding a transcoder would expand the image and timeout/resource risk. Optional
thumbnail frame sampling remains conditional on an externally available ffmpeg
and is unrelated to media compression.

## Failure, logging, and Sentry contracts

After all acceptable formats are exhausted the worker persists the existing
fields:

- `status=failed`
- `error_code=media_size_exceeded`
- `error_message=Recipe Dost tried a smaller video version, but this video is
  still too large for automatic recipe analysis. Try a shorter video or another
  recipe link.`

The same safe message is included in the existing failure event metadata. No
schema or response shape changes. Build 3 queries `error_code,error_message`,
but its installed failure-copy functions do not map `media_size_exceeded` and
ignore the server message for this case. It therefore still shows generic copy;
showing the improved wording requires a later mobile update. The worker must not
rename or disguise the error code.

Intermediate fallbacks log `media_format_fallback` at INFO with attempt,
resolution buckets, normalized reason, and `terminal=false`. A terminal size
outcome logs `worker_outcome` at WARNING with only platform, stage, normalized
code, attempt count, duration/size/resolution buckets, terminal flag, and the
Sentry event ID or `none`. Unexpected terminal technical exceptions use the
existing ERROR Sentry/reporting boundary. Logs never contain URLs, cookies,
paths, media metadata text, AI data, or credentials.

Previously `media_size_exceeded` was intentionally classified as an expected
product failure, `report_queue_job_failure` returned when no exception existed,
and `before_send` rejected events without an exception. Terminal size exhaustion
now captures exactly one warning message with fingerprint
`[worker_outcome, media_size_exceeded]`. The strict sanitizer retains only the
active-flow tags and safe buckets. Intermediate attempts and successful
fallbacks do not capture. Separate failed jobs remain separate occurrences in
one grouped issue. Capture/delivery failure cannot alter database job behavior.
If persisting any terminal failure itself raises, the existing
`terminal_failure_persistence_failed` error capture remains active.

## Development verification

1. On worker `develop`, run `make check-dev`, `python3 -m compileall app scripts`,
   and `python3 -m pytest -q`.
2. Review Render development environment keys for stale non-secret overrides.
   Do not print values. Remove an obsolete override only through the established
   env migration process; effective health must show preferred 36 MiB, hard 50
   MiB, four attempts, 360-720, temp-file mode, and provider max 2,000,000,000.
3. Push `develop` only after review; allow the development Render service's
   existing branch deployment to finish.
4. Check `/health.safe_config` before creating a job. Confirm queue mode,
   unified extraction, raw URL logging false, and the normalized limits above.
5. Use a controlled public/authorized non-production fixture. Confirm one job,
   one final result, format fallback logs when applicable, cleanup, and unchanged
   quota behavior. Do not use the incident URL.
6. Verify a terminal synthetic size case writes the existing code/message and a
   searchable WARNING. Normal development Sentry runtime remains disabled by
   policy; do not send a production Sentry test.

## Production deployment and rollback

1. Complete development verification first. Review and merge through the normal
   repository process; production must deploy only from `main`.
2. On `main`, run `make check-prod`, compileall, and pytest. Confirm production
   config and any safe Render override agree on 36/50 MiB and 360-720.
3. Let the existing production Render service deploy the reviewed `main` commit.
   Do not manually run a queue job until `/health.safe_config` matches policy.
4. Perform one controlled authorized smoke job per active route as required.
   Check final DB fields, quota/accounting, privacy-safe Render logs, temporary
   cleanup, and one grouped Sentry occurrence only for a terminal synthetic size
   outcome. Never process the production incident URL.
5. Roll back by redeploying the last known-good `main` Render revision or by
   reverting the reviewed commit and deploying that new `main` revision. Verify
   health and queue completion afterward. As a temporary config mitigation for
   the new code only, preferred and hard values must be lowered together (for
   example both to 30 MiB); setting only the hard limit below preferred will
   intentionally fail startup. Unsetting `SENTRY_DSN` disables worker Sentry but
   is not a media-processing rollback.

Remaining risks: format metadata and platform extractors can change; unknown
sizes depend on progress-hook cadence; there is no active unified whole-download
deadline; Render does not publish a fixed ephemeral-disk capacity; and Build 3
cannot render the improved server copy. Monitor duration, fallback rates,
technical errors, and grouped terminal size occurrences after staged rollout.
