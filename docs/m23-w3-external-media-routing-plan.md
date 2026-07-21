# M23.W3 External Media Resolver Architecture and M23.W3C Implementation

Status: M23.W3A audit complete and M23.W3C implemented locally on `develop`
after `fc72bb5`. The external route has fixture coverage but has not made a live
provider call or been deployed. Provider credentials remain Render-only.

Sections 1–18 preserve the source-grounded audit and design against base commit
`fc72bb5`. Section 19 is the authoritative current local implementation and
supersedes the earlier candidate-provider labels, non-atomic claim finding, and
cookie-metadata finding where they differ.

The repository path supplied without the intermediate `recipe-dost/` directory
does not exist on this machine. The audited worker is
`/Users/sadaqatali/Documents/AiAppBuilder/recipe-dost/recipe-dost-media-worker`.
The Build 3 reference repository was read only at
`/Users/sadaqatali/Documents/AiAppBuilder/recipe-dost/recipe-dost`.

## 1. Source-grounded audited baseline architecture

### Active worker runtime at `fc72bb5`

| Concern | Current active implementation | Evidence |
|---|---|---|
| Production start | `uvicorn app.main:app --host 0.0.0.0 --port $PORT --timeout-keep-alive 120` | `render.yaml`; Docker uses the same command with port `10000` fallback |
| Development start | Same Uvicorn command; the development Render service tracks `develop`, while production tracks `main` | `docs/worker-context.md`, `Dockerfile` |
| First application module | `app.main`; logging and `initialize_sentry()` run at import | `app/main.py` |
| Queue entry | FastAPI startup and `/wake` call `ensure_queue_worker_started()`, which creates `run_queue_loop()` | `app/main.py` |
| Poll/claim | `run_queue_loop()` -> `process_next_queued_job()` -> `get_oldest_queued_job()` -> unconditional `update_job(status="resolving")` | `app/queue_worker.py`, `app/supabase_client.py` |
| Platform route | `platform_for_job()` uses `media_jobs.platform` first, then URL host detection | `app/job_runner.py`, imported by `app/queue_worker.py` |
| YouTube route | `should_use_youtube_gemini_url()` -> oEmbed metadata -> Gemini `Part.from_uri` with the public YouTube source URL | `app/queue_worker.py` |
| Instagram/TikTok route | `extract_social_video()` -> yt-dlp metadata/cookie path -> bounded candidate selection -> verified temp file -> Gemini Files API | `app/queue_worker.py`, `app/social/stream_download.py` |
| AI submission | `run_single_pass_structured_extraction()` calls `client.models.generate_content`; social first uploads through `client.files.upload` | `app/queue_worker.py` |
| Result persistence | `save_gemini_recipe_card()` -> `MediaJobRepository.insert_recipe_card()` -> job `completed` with result ID | `app/queue_worker.py`, `app/supabase_client.py` |
| Terminal failure | `record_queue_failure_with_reporting()` -> `record_queue_failure()` -> job `failed`, progress `100`, code/message/event | `app/queue_worker.py` |
| Retry owner | The serialized queue owns one logical processing attempt. yt-dlp owns configured extractor retries and adaptive attempts; Gemini structured code owns repair/fallback. Failed jobs are not automatically requeued. | `app/queue_worker.py`, `app/social/stream_download.py` |
| Temp lifecycle | Per-job directory under `/tmp/recipe-dost-media`; partials are cleared between attempts and queue `finally` deletes the result directory. Process crashes can leave files until Render replaces the ephemeral instance. | `app/social/stream_download.py`, `app/queue_worker.py` |
| Sentry owner | `app.observability.sentry` is the only intended current-flow capture layer; queue boundaries call it, and shutdown flushes it | `app/main.py`, `app/queue_worker.py`, `app/observability/sentry.py` |

The claim is not atomic: two processes could read the same oldest row before
either unconditional update. Render concurrency one reduces but does not remove
this correctness risk. The next implementation must use an update conditional
on `status=queued` and proceed only when one row is returned.

`POST /process-job` is active only as queue-wake compatibility when
`WORKER_EXECUTION_MODE=queue`. Its direct `JobRunner.run()` branch,
`app/job_runner.py` social/TikWM/SnapInsta/SnapSave execution, debug endpoints,
Cobalt, and the old webhook/background flow are legacy/manual/debug and are not
integration targets. `app/job_runner.py` is currently imported only for a few
shared helpers and `platform_for_job`; that accidental dependency should not be
expanded.

### Audited baseline AI flow

1. Build 3 calls the authenticated `create-media-job` Edge Function.
2. The function loads the saved `recipe_items` row, checks duration, suppresses
   a duplicate active job for the recipe, computes server-side AI access, and
   inserts a `queued` `media_jobs` row only when allowed.
3. In queue processor mode the Edge Function records a safe event and calls
   `/wake`; it does not execute media processing.
4. The worker polls the oldest queued row, changes it to `resolving`, and routes
   by stored platform with host fallback.
5. YouTube in `gemini_url` mode skips yt-dlp, attempts oEmbed best-effort, saves
   metadata, and sends the public URL directly to Gemini.
6. Instagram/TikTok run unified yt-dlp extraction. Instagram production is
   configured cookie-first. A selected combined media candidate is written to a
   bounded temporary file and must be marked `video_bytes_verified`.
7. The worker saves public/private source metadata, optionally uploads the
   thumbnail, uploads social media to Gemini Files, waits for `ACTIVE`, performs
   structured extraction, validates/normalizes the card, inserts `recipe_cards`,
   and marks the job `completed`.
8. Any controlled failure marks the job `failed` at progress `100`. The queue
   `finally` closes memory buffers and deletes the temp directory.

### Current quota/accounting contract

`supabase/functions/_shared/ai-access.ts` is authoritative. It reads
`user_profiles` and server-side `media_jobs`; it does not trust client plan
labels.

- Free access defaults to three lifetime successful AI generations.
- Premium defaults to the existing monthly limit (currently 100 in server code).
- Admin/test users use server profile role/flags and optional server override.
- Successful usage is `status=completed` with a non-null result card and
  `ai_provider`.
- Reservation is implicit: any job in an active status is counted.
- A failed job releases capacity automatically because it is neither active nor
  successful. There is no separate reservation row to release.
- Duplicate active jobs for the same recipe are returned rather than inserted.

There is a source discrepancy that must be repaired before rollout. The active
queue directly calls Gemini and never invokes the legacy
`JobRunner._start_gemini_api_call()` repository guard. The active single-pass
path can call primary, repair, then fallback, despite
`GEMINI_MAX_CALLS_PER_JOB=2`. The Edge access check protects user entitlement,
but it is not a per-provider-call budget. The next milestone must extract a
shared, repository-backed call guard used immediately before every Gemini model
call, default repair/fallback chains off for free-tier testing, and test the
two-call ceiling.

## 2. Current saved-link and metadata flow

### Active Build 3 flow

1. The save/share modal validates an Instagram, TikTok, or YouTube URL and does
   an in-memory duplicate check.
2. `save-recipe-link` authenticates the user, validates HTTP(S), normalizes host
   and platform, builds a platform-specific canonical source key, checks exact
   URL/canonical/legacy duplicates for that user, and immediately inserts a
   minimal `recipe_items` row.
3. The minimal row has a fallback title such as `Instagram recipe link`, null
   thumbnail/duration, `metadata_status=pending`, and the canonical source key.
   Link saving therefore already succeeds without creating `media_jobs`,
   reserving AI access, calling Gemini, or downloading video.
4. The app puts that row in Library caches, starts
   `refresh-recipe-metadata` in the background, closes the modal, and opens the
   saved item. It polls pending metadata every two seconds.
5. The Edge Function authenticates again, loads the user's item, wakes the
   worker, then calls protected `/metadata-preview` with a bounded timeout and
   at most two attempts.
6. Worker preview validates the URL, detects/accepts platform, uses YouTube
   oEmbed first, or social yt-dlp with `download=False`, then OpenGraph fallback.
   Thumbnail selection is metadata-first.
7. The Edge Function writes title, description, thumbnail, duration,
   `metadata_status`, and safe `source_metadata` to `recipe_items`.
8. Build 3 displays the row from `recipe_items`; Library/detail queries also
   poll active AI jobs every 2.5 seconds from `media_jobs`.

The older `parse-url`/`recipe/preview` route is not called by the active save
modal and is excluded from the target flow. It should not be revived as a
second metadata architecture.

### Findings

| Question | Finding |
|---|---|
| Does metadata download media? | `/metadata-preview` uses yt-dlp `download=False` and OpenGraph; it does not intentionally download video bytes. It does download bounded/unbounded-enough HTML and image validation may make network requests. |
| Are cookies used? | Yes. `fetch_ytdlp_preview()` calls `get_platform_cookie_file()` and supplies `cookiefile` when configured. Production enables yt-dlp/Instagram cookies. This violates the target public/no-cookie metadata policy. |
| Which metadata platforms may use cookies? | Any platform whose cookie flag/file is enabled; production specifically enables Instagram. YouTube preview normally uses oEmbed first. |
| Is saving coupled to AI? | No: `save-recipe-link` inserts first and never creates an AI job. Metadata refresh is also quota-free. |
| Is metadata coupled to media extraction? | The standalone preview is metadata-only, but it shares cookie helpers. In the AI queue, social metadata is persisted only after successful unified media extraction, so that lane is incorrectly downstream of download. |
| Can incomplete metadata block saving? | No. A minimal pending row is inserted first; metadata failure preserves a fallback title and marks the row failed. |
| Is a minimal record supported? | Yes: platform, URL, fallback title, status, and source metadata are enough for Build 3. |
| Is canonical hashing/dedup present? | Per-user canonical keys and duplicate checks exist, but no hash is used. Refresh success/failure replaces `source_metadata` and drops `normalized_source_key`; later dedupe falls back to a bounded legacy scan of 500 rows. |
| Is metadata cached? | No shared cache exists across users/jobs. App query caching is only client state, not extraction caching. |
| Timeout/retry | Edge preview timeout is configurable and retries at most once after a warm-up. Worker oEmbed uses 10 seconds; yt-dlp uses socket timeout 15 seconds and one retry; OpenGraph connect/read is 10/20 seconds. The Edge abort does not cancel the synchronous worker extraction. |
| Abuse/rate limiting | Authentication, 2048-character URL limit, per-user dedupe, and bounded retries exist. There is no explicit per-user save/refresh rate limit, concurrency budget, negative cache, or cross-user metadata request suppression. |
| Privacy gaps | `refresh-recipe-metadata` currently logs user IDs plus URL host/length in safe-looking events. Those fields are outside the target privacy contract and must be removed before rollout. |

The active queue also writes a discovered CDN URL to
`media_job_private_metadata`. It is not user-visible, but the target external
architecture forbids persistent signed/direct media URLs. External adapters
must never call that field, and the legacy writer should be disabled when the
rollback window closes.

## 3. Target route matrix

| Product action | YouTube | Instagram | TikTok |
|---|---|---|---|
| Save link | Immediate minimal row; no AI job | Same | Same |
| Public metadata | oEmbed, then public/no-cookie fallback | Public/no-cookie metadata only | Public/no-cookie metadata only |
| AI media input | Public YouTube URL directly to Gemini when enabled | External provider adapter -> in-memory URL -> guarded temp download -> Gemini Files | API7 primary; eligible 7scorp fallback -> guarded temp download -> Gemini Files |
| Default downloader | None | External adapter | External adapter |
| Cookie downloader | Emergency rollback only, separately enabled, default false | Same | Same |

Facebook remains outside this milestone's supported target matrix. Provider
labels are candidates, not verified contracts. No endpoint path, HTTP method, or
response field may be hardcoded until owner-supplied sanitized fixtures and
provider documentation are reviewed.

### Metadata policy

- Saving is not artificially quota-limited and never reserves AI quota.
- “Unlimited saving” means no product credit, not unbounded infrastructure use.
- Initial technical controls: authenticated calls, at most 30 new saves per
  user/hour, at most 5 explicit refreshes per item/day, worker metadata
  concurrency 2, one retry, and a five-second end-to-end fetch budget. These are
  owner-tunable abuse controls, not plan entitlements.
- Canonicalize supported URLs once and store a SHA-256 canonical hash in existing
  JSON metadata; preserve it through metadata merges.
- Suppress duplicate work per canonical hash. Cache successful public metadata
  for 24 hours and negative/unavailable results for 15 minutes. Do not cache
  signed thumbnails or any direct media URL.
- Use public/no-cookie methods only. No account cookie, authenticated scraping
  account, video download, persistent media, or AI call is permitted.
- A cache miss may return the already-saved minimal row while refresh continues.
- Events contain only platform, normalized status, resolver class, cache
  hit/miss, attempt count, duration bucket, and result-presence booleans.

No schema migration is required for initial metadata caching/dedup because the
existing `recipe_items.source_metadata` JSON can preserve canonical hash and
cache timestamps. A later indexed cache table may be justified by volume, but
is explicitly outside the next milestone.

### What Build 3 displays

| Metadata result | Current Build 3 behavior | Mobile release needed? |
|---|---|---|
| Full metadata | Title, thumbnail, platform/status and available details | No |
| Thumbnail only | Fallback platform title plus thumbnail; server treats title/thumbnail as completed | No |
| Platform + URL only | Fallback title, placeholder/no image, saved item remains usable | No |
| Temporarily unavailable | Saved item remains; metadata status becomes failed and refresh can be retried | No for behavior; differentiated temporary copy needs Build 4 |
| Deleted/private | Same generic failed-metadata presentation; saved link is not deleted | No for preservation; private/deleted-specific copy needs Build 4 |

## 4. Server-side audience and entitlement routing

Use one exposure enum:

- `disabled`: external social routing is off. Before creating/reserving a social
  AI job, the Edge Function must return a safe unavailable response unless an
  explicit emergency legacy mode is enabled. YouTube direct remains independent.
- `selected_test_users`: external social routing is permitted only when the
  server profile says admin/test user. All other social requests fail before
  job insertion; free credits are not silently sent to the known-failing path.
- `premium_only`: server-confirmed Premium/admin/test users route externally.
  Free users retain their three successful generations for working routes such
  as YouTube, but unsupported social jobs fail before reservation.
- `all_ai`: after the normal server access check, both Premium allowance and a
  free user's remaining three successful credits may route externally.

The Edge Function already writes authoritative safe AI-access metadata into the
`media_job_created` event. The worker should read that server-written event (or
an equivalent server-written JSON field), validate a minimal route class, and
fail closed when absent. It must never accept a client plan/audience label.

Initial production canary recommendation: **selected test users only**. It is
narrower than `premium_only`, avoids using paying customers as first traffic,
and exercises the same server profile and job contract. Phase 4 broadens to
Premium. `all_ai` is then a Render/config change, not a code or mobile release.

## 5. Provider-neutral contract

Do not expose provider payloads to queue business logic. The proposed internal
boundary is:

```text
MediaResolver.resolve(MediaResolutionRequest) -> MediaResolution
```

`MediaResolutionRequest`:

- `platform`
- canonical source URL, memory only
- safe job/correlation UUID
- capability (`video_with_audio` initially)
- hard byte limit
- total resolver timeout budget

`MediaResolution`:

- adapter ID
- success/failure
- direct media URL, memory only
- expiry when known
- content type
- duration, width, height, and known/estimated size
- audio presence as present/absent/unknown
- safe thumbnail metadata, including whether a URL is public/persistable
- provider request ID only when contract review declares it safe
- normalized error code and retryability
- rate-limit limit/remaining/reset or retry-after when safely parsed
- asynchronous provider state, status token held in memory, poll-after, and
  terminal/completed state when applicable

The result object must make accidental serialization of direct URLs/status
tokens difficult (for example, secret wrapper fields and an explicit
`safe_event_metadata()` method). Queue events and job tables receive only that
safe projection.

## 6. Adapter boundaries and fixtures

Planned adapters:

- `instagram_rapidapi_media_downloader` (candidate provider only)
- `tiktok_api7` primary candidate
- `tiktok_7scorp` fallback candidate
- `youtube_direct_gemini` (no media download)
- `legacy_ytdlp_cookie` only for controlled rollback

Each external adapter owns only request construction, authentication header,
provider status/poll parsing, provider-specific schema validation, and mapping
to `MediaResolution`. It does not download media, call Gemini, update Supabase,
or decide entitlement/fallback. A shared transport owns timeouts, response-size
limits, redaction, rate-limit parsing, and no-body logging. A separate guarded
media fetcher owns the returned URL.

Store sanitized contract fixtures at:

```text
tests/fixtures/media_resolvers/<adapter_id>/
  success.json
  async_accepted.json
  async_progress.json
  async_completed.json
  invalid_url.json
  private_or_deleted.json
  rate_limited.json
  server_error.json
  timeout.json
  malformed.json
  authentication_error.json
```

Fixtures use `example.invalid` URLs, remove query strings/tokens, replace IDs
with stable placeholders, remove titles/captions/user handles, and contain no
headers, keys, cookies, signed URLs, or real provider bodies copied wholesale.
Keep only the minimal shape needed to prove parsing.

## 7. YouTube direct-Gemini policy

The active route already sends a public YouTube URL directly to Gemini and does
not use yt-dlp when `YOUTUBE_AI_INPUT_MODE=gemini_url`.

Target rules:

- Validate `youtube.com`, its supported subdomains, and `youtu.be`, plus a valid
  11-character watch/Shorts/embed ID.
- Restrict to public videos; oEmbed failure is metadata-only and may continue,
  but Gemini private/deleted/refusal outcomes map safely.
- `YOUTUBE_DIRECT_GEMINI_ENABLED=true` is independent of social resolver mode.
- Bound the Gemini request and the total job budget.
- Map refusal/unavailable/timeout to `youtube_processing_failed` with internal
  retryability detail.
- If provider URL support changes, disable the flag and fail safely. Do not
  download locally or route through RapidAPI by default. A future fallback
  requires a separate reviewed provider/flag.

## 8. Feature flags and secrets

Smallest coherent flag set:

| Flag | Values/default | Purpose |
|---|---|---|
| `MEDIA_RESOLVER_MODE` | `legacy|external|hybrid`; initially `external` in code but harmless while audience is disabled | Strategy |
| `EXTERNAL_MEDIA_RESOLVER_AUDIENCE` | `disabled|selected_test_users|premium_only|all_ai`; default `disabled` | Exposure and entitlement route |
| `INSTAGRAM_MEDIA_PROVIDER` | verified adapter ID; no active default until fixture review | Instagram adapter |
| `TIKTOK_MEDIA_PROVIDER_PRIMARY` | verified adapter ID; no active default | TikTok primary |
| `TIKTOK_MEDIA_PROVIDER_FALLBACK` | verified adapter ID or empty | Eligible fallback |
| `COOKIE_MEDIA_FALLBACK_ENABLED` | `false` | Separate emergency rollback gate |
| `YOUTUBE_DIRECT_GEMINI_ENABLED` | `true` after validation | Direct URL route |

Do not add percentage canary initially; selected server test users are clearer
and deterministic. If later needed, add `EXTERNAL_MEDIA_CANARY_PERCENT` as an
integer 0-100 applied by a stable job UUID hash after audience validation, never
by user ID.

Missing flags use closed defaults. Malformed enum/provider values fail startup;
an enabled audience with a missing selected provider/key also fails startup.
Safe startup/health output shows normalized modes, adapter IDs, and key-present
booleans only. Development and production JSON values remain independent.
Secrets remain Render environment values, not committed config.

Future server-only secret names:

- `INSTAGRAM_MEDIA_PROVIDER_API_KEY`
- `INSTAGRAM_MEDIA_PROVIDER_HOST`
- `TIKTOK_API7_PROVIDER_API_KEY`
- `TIKTOK_API7_PROVIDER_HOST`
- `TIKTOK_7SCORP_PROVIDER_API_KEY`
- `TIKTOK_7SCORP_PROVIDER_HOST`

Host identifiers are configuration, not credentials, but remain server-only
until verified. Prefer separate RapidAPI applications/keys for development and
production. Never put keys in `EXPO_PUBLIC_*`, mobile source, Supabase client
configuration, tables/events, examples with real values, Sentry, PostHog, or
logs.

Rotation: create a replacement key in the same environment, update only that
Render service's secret, redeploy/restart, verify safe health plus one fixture or
authorized canary, then revoke the old key. If leaked, disable audience first,
revoke immediately, review provider usage without exporting response bodies,
replace dev/prod independently, and reopen canary only after validation.

## 9. External URL and temporary-media security

Existing protections: streaming rather than whole-response reads, 50 MiB hard
byte guard, declared-length precheck, connect/read timeouts, per-job temp
directory, safe path segment, argument-array ffprobe/ffmpeg calls (no shell),
optional content-type checks in the legacy memory helper, post-download size
check, bounded probe subprocess, and normal-path cleanup.

Missing and required for the shared external fetcher:

1. Accept HTTPS only, reject credentials, fragments, malformed/oversized URLs,
   and unapproved ports.
2. Disable automatic redirects; allow at most two and validate every hop.
3. Resolve all A/AAAA addresses before each hop and reject loopback, private,
   link-local, multicast, reserved, unspecified, carrier-grade NAT, and metadata
   service ranges. Use `requests.Session(trust_env=False)` and validate the
   connected peer IP when available to close the DNS-rebinding gap. Prefer a
   verified CDN host allowlist. If peer validation cannot be guaranteed, fail
   closed.
4. Never issue HEAD by default. Use it only for a verified CDN, then perform the
   same guarded GET because size headers are advisory.
5. Require an approved video content type; allow octet-stream only when later
   signature/container checks succeed.
6. Stream in small chunks with connect/read and total wall-clock deadlines,
   enforce the byte limit on actual bytes, and add a low-throughput deadline
   (for example, abort sustained sub-16 KiB/s after 15 seconds) in the fetcher.
7. Create a random `0700` job directory and `0600` file under the configured temp
   root; never derive a path from provider data.
8. Validate magic/container signature, then run bounded ffprobe and require one
   video plus one audio stream before Gemini. Do not trust provider size/audio.
9. Clean up on each failed attempt, success, cancellation, and retry. Add a
   startup sweeper limited to children of the exact temp root older than the
   maximum job runtime plus margin for crash recovery.
10. Never persist or return the direct URL, redirect URL, status token, media
    bytes, or provider response. A public stable thumbnail may use the existing
    approved thumbnail lane; signed thumbnails may not.

The current direct downloader permits HTTP, follows unlimited library-default
redirects, has no IP/DNS validation, does not validate response content type or
signature, and cannot confirm unknown audio because the Docker image lacks
ffprobe. `Dockerfile` must supply a supported ffprobe runtime before external
media is allowed to reach Gemini; otherwise the route fails closed.

## 10. Idempotency, retries, and paid-call controls

- Atomically claim `queued -> resolving` with a conditional update before any
  provider request.
- Write a local attempt ledger to existing job-event JSON before/after each paid
  request: adapter, ordinal, deterministic idempotency-key hash, state, HTTP
  status class, retryability, and safe latency bucket only.
- Use provider idempotency keys when documented; base them on job UUID, adapter,
  and contract version, never source URL/user ID.
- Default to one submit request per adapter. Allow one retry only for a proven
  pre-connect failure or documented idempotent status check. Never repeat an
  ambiguous timed-out paid request without querying provider status.
- Async providers: maximum six polls within 45 seconds with bounded backoff
  (2/4/6/8/10/15 seconds, clipped to budget). Poll attempts are ledgered and
  stopped on terminal state or rate limit.
- Default maximum paid resolver calls: primary one plus fallback one. No fallback
  on authentication, authorization, malformed configuration, or invalid source.
  Eligible fallback: retryable provider outage/rate limit, unsupported provider
  media representation, no-audio result, or exhausted/expired media URL when the
  alternate provider may return a different asset.
- Re-resolve an expired signed URL at most once through provider status/idempotent
  lookup before fallback; never reuse a persisted URL.
- Per-provider concurrency one for Render Free. Trip an in-process circuit after
  five operational failures in two minutes, open for 60 seconds, then allow one
  half-open probe. Authentication/configuration failures open immediately until
  configuration changes.
- Parse `Retry-After`/rate-limit fields when documented; do not sleep beyond the
  job budget. A cost guard stops new provider submissions when the configured
  daily/monthly call budget is reached.
- Exactly one code path owns terminal job persistence. Intermediate adapters
  return results/errors and never update the job. One logical job consumes user
  quota only on its existing successful completion condition; retries and
  fallback do not double-charge.

The existing adaptive yt-dlp attempts are not paid provider calls. The existing
Gemini repair/fallback chain must be brought under the shared two-call guard
before external rollout.

## 11. Cost and usage model

Do not estimate price until the owner provides plan details.

```text
monthly external calls =
  free AI jobs routed externally
  + Premium/test AI jobs routed externally
  + paid retry submissions
  + fallback-provider submissions

monthly media bandwidth =
  successfully resolved media count * average guarded downloaded bytes

monthly provider cost =
  sum(provider billable calls * owner-supplied provider cost per unit)
```

Track per adapter/environment: submit calls, polls, success, normalized failure,
retry, fallback, circuit-open rejection, rate-limit count, resolution latency,
downloaded size bucket, download/validation success, Gemini completion, and
recipe-card completion. Add owner-supplied plan limit/cost as operational config,
not code comments pretending to be current pricing.

`selected_test_users` is the smallest volume. `premium_only` approximates
eligible Premium/test social jobs plus retries/fallback. `all_ai` adds free users'
remaining three successful-credit attempts; failed jobs do not add successful
quota but do add provider cost, so failure and retry rates are essential.

## 12. Build 3-compatible normalized errors

Keep `media_jobs.status`, `error_code`, and safe `error_message`. The extractor's
`safe_error_message` exists only in result/event metadata; there is no separate
job column. Build 3 polls `media_jobs` and does not read job events for UI.

| Provider-neutral code | Retryable | User-safe message category | Sentry | Fallback |
|---|---:|---|---|---|
| `media_unavailable` | no | Link/media unavailable | no issue | no |
| `media_private_or_deleted` | no | Private, deleted, or unavailable | no issue | no |
| `unsupported_media` | no | Unsupported media | no issue | no |
| `media_size_exceeded` | no | Too large/longer source suggested | grouped beta warning only | only if alternate quality is plausible |
| `media_has_no_audio` | no | No usable audio | no issue | eligible |
| `resolver_rate_limited` | yes | Resolver temporarily busy | grouped warning after exhaustion | eligible |
| `resolver_temporarily_unavailable` | yes | Try again later | grouped warning after exhaustion | eligible |
| `resolver_configuration_error` | no | Service not configured | Sentry error, one authoritative capture | never |
| `media_download_failed` | yes | Media could not be fetched | warning/error after exhaustion | eligible |
| `media_validation_failed` | depends | Media could not be validated | Sentry error for parser/validation bug | eligible when content-specific |
| `youtube_processing_failed` | depends | YouTube processing unavailable | warning/error by cause | no external fallback by default |
| `ai_provider_failed` | depends | AI processing failed | existing Gemini technical policy | not a media fallback |

Provider mapping rules:

- Provider invalid URL -> `media_unavailable`, non-retryable, no Sentry/fallback.
- Private/deleted/not-found -> `media_private_or_deleted`, non-retryable, no
  Sentry/fallback.
- 429 -> `resolver_rate_limited`, retry-after captured as a safe bucket, warning
  only after exhaustion, eligible fallback.
- 5xx/timeout -> `resolver_temporarily_unavailable`, retryable, grouped warning
  after exhaustion, eligible fallback.
- 401/403 credentials or missing host/key -> `resolver_configuration_error`,
  Sentry error, no fallback.
- Unexpected/malformed schema -> `resolver_configuration_error` when adapter
  contract mismatch, Sentry error with exception stack and no body, no fallback
  until reviewed.
- Expired direct URL -> one status/re-resolve check, then
  `media_download_failed`, eligible fallback.
- Missing audio/oversize/invalid container -> the corresponding media code,
  normally no Sentry unless the adapter contradicted its verified contract.

Compatibility: Build 3 has special copy only for
`gemini_structured_extraction_failed`, `gemini_daily_dev_limit_reached`, and
duration codes. All resolver codes above currently show generic “Could not
generate AI recipe”/“Video analysis failed” copy, although the database message
is safe. Build 4 is required for differentiated private/deleted, rate-limit,
no-audio, size, provider-temporary, and validation copy. No existing code should
be renamed during the canary.

## 13. Sentry, Render, and Supabase observability

Current normal development Sentry is intentionally disabled: configuration
requires Render, a valid DSN, `APP_ENV=production`, and branch `main`. Development
is enabled only for the explicit controlled test command. Therefore development
active-flow warnings return no event ID. If a production warning is missing,
the source-supported checks are valid `SENTRY_DSN`, `RENDER=true`,
`APP_ENV=production`, and `RENDER_GIT_BRANCH=main`; this audit did not inspect
secret values or live Render state.

The next milestone must add an explicit privacy-safe development-canary mode (or
equivalent reviewed policy) so real selected-test-user failures can reach a
development Sentry environment without enabling FastAPI/log auto-capture. It
must also add adapter IDs/new normalized codes to the sanitizer allowlists and
emit a safe startup reason/mode, never the DSN. One authoritative capture occurs
after all retry/fallback attempts, not per attempt.

Policy:

- Expected content outcomes: safe job event and Render info/warning; normally no
  Sentry issue.
- Provider operational exhaustion: grouped warning tagged by adapter and code;
  no body/URL.
- Credentials/configuration/parser/unexpected schema/downloader/persistence:
  Sentry error with safe stack, captured once.
- Safe fields: service, `flow=current`, stage, platform, adapter ID, normalized
  code, HTTP status class, attempt, fallback used, server route class
  (`premium`, `free_credit`, `test`), safe duration/size/latency buckets,
  release, terminal.
- Never capture URL, provider body, key, user ID, recipe content, cookies,
  prompt/output, request headers/body, or direct provider request token.

Preserve existing events and add only JSON metadata (no schema migration):

- `resolver_strategy`
- `resolver_adapter`
- `route_audience`
- `provider_attempt`
- `fallback_used`
- `provider_status_class`
- `normalized_error_code`
- `media_size_bucket`
- `duration_bucket`
- `media_validation_result`
- `external_resolution_latency_bucket`

Never store direct/provider URLs, raw responses, keys, cookies, provider-signed
tokens, or extra title/description beyond the approved metadata lane. Remove
user IDs and URL host/length from Edge metadata-refresh logs.

## 14. Next implementation file plan

Smallest worker set:

- `app/config.py` — strict enums, adapter/key-presence validation.
- `config/worker.defaults.json`, `config/worker.development.json`,
  `config/worker.production.json` — disabled/default and independent exposure;
  no secrets.
- `app/media_resolvers/contracts.py` (new) — request/result/error types and safe
  projection.
- `app/media_resolvers/router.py` (new) — audience/mode/platform route and
  primary/fallback policy.
- `app/media_resolvers/rapidapi_transport.py` (new) — bounded/redacted provider
  HTTP transport.
- `app/media_resolvers/instagram_rapidapi.py` (new),
  `tiktok_api7.py` (new), `tiktok_7scorp.py` (new), and
  `legacy_ytdlp.py` (new thin rollback wrapper).
- `app/media_resolvers/media_fetch.py` (new) — SSRF-safe URL validation,
  redirects, streaming limit, signature/probe, and temp cleanup.
- `app/queue_worker.py` — replace only the social resolver boundary; preserve
  YouTube/result/error contracts; add shared Gemini call guard.
- `app/supabase_client.py` — conditional claim, route-event lookup, attempt
  ledger queries, and shared Gemini/provider guards.
- `app/main.py` — safe health/startup mode and adapter summaries.
- `app/observability/sentry.py` — new codes/tags, development-canary policy, one
  authoritative capture.
- `Dockerfile` — provide verified ffprobe support before enabling traffic.
- `tests/fixtures/media_resolvers/**`, plus focused resolver routing, contract,
  security, quota, and observability tests.

No `requirements.txt` change is expected if the existing `requests` plus Python
standard library can implement the verified contract safely. Do not add a
dependency speculatively.

Read-only app/backend files expected in a separately authorized coordinated
change, with no mobile UI edit:

- `supabase/functions/create-media-job/index.ts` — server route decision and
  pre-insert failure for audiences that must not use the known-failing route.
- `supabase/functions/_shared/ai-access.ts` — only if a small safe route-class
  projection cannot reuse existing metadata.
- `supabase/functions/save-recipe-link/index.ts` — stable canonical hash/merge
  and bounded save abuse control.
- `supabase/functions/refresh-recipe-metadata/index.ts` — no-cookie contract,
  cache/dedup, retry budget, preserve canonical metadata, and remove identity/URL
  logging.

Do not change Build 3 screens, polling types, Supabase schema, legacy resolver
modules, debug endpoints, `render.yaml`, or deployment files merely to add the
adapter.

## 15. Staged rollout and rollback

### Phase 0 — current milestone

Complete audit; obtain sanitized provider contracts/fixtures and owner plan
limits. No provider traffic.

### Phase 1 — disabled implementation

Add contract/router/adapters/fetcher behind `audience=disabled`; fixture and
mock failure matrix only. Conditional claim, call ledger, Gemini guard, Sentry
canary policy, and cleanup must land before traffic.

### Phase 2 — development selected test users

Enable external mode for server-confirmed test users. Cookie route remains off.
Use low concurrency and authorized disposable URLs only after mocked matrix
passes. Verify DB contract, successful/failed quota accounting, provider cost,
latency, cleanup, Render logs, and development-canary Sentry.

### Phase 3 — production selected-test-user canary

Deploy reviewed `main`, leave audience narrow, concurrency one, cookie fallback
false. Stop on credential/parser issue, URL leak, duplicate paid call, quota
regression, or material provider error/latency/cost breach.

### Phase 4 — Premium

Set audience to `premium_only` without code/mobile changes after canary gates
pass. Non-Premium social AI still fails before reservation; YouTube remains
available under the existing access rules.

### Phase 5 — all AI

Set `all_ai` after cost/success evidence. Free users' existing three successful
generations route externally; failed jobs remain uncharged.

### Phase 6 — retire cookies

Keep rollback wrapper for a bounded stability window, then remove production
cookie secrets and obsolete code. Record an owner-selected removal date.

Rollback requires no mobile build: first set audience `disabled`. If only one
provider is unhealthy, disable its adapter/audience route. Emergency legacy
rollback requires both `MEDIA_RESOLVER_MODE=legacy` (or `hybrid`) and
`COOKIE_MEDIA_FALLBACK_ENABLED=true`; it must remain audience-gated and must not
run for auth/config errors. If cookie isolation cannot meet logging/persistence
rules, do not enable it—redeploy the last known-good worker revision instead.
YouTube can be independently disabled with its flag. Verify queued jobs reach a
defined safe outcome after any rollback; do not strand free-credit social jobs
on the known-failing path.

## 16. Next-milestone test plan

Routing:

- metadata save never downloads media or reserves AI quota
- Instagram AI uses configured external adapter
- TikTok primary then only eligible fallback
- YouTube direct Gemini and no downloader
- disabled mode gives the defined pre-reservation social outcome
- server-written entitlement/test route only; client plan ignored

Provider contracts: success, async accepted/progress/completed, invalid URL,
private/deleted, rate limit, server error, timeout, malformed response, bad
credentials, expired URL, missing audio, and oversize.

Security: key/URLs/body/user ID absent from logs/events/Sentry; HTTP/private and
reserved addresses blocked; DNS/peer and each redirect checked; redirect/byte/
time/low-speed limits; content signature and audio/video validation; no shell
construction; temp cleanup on success/error/retry/cancellation plus startup
sweep.

Reliability/cost: atomic claim; no duplicate paid request after ambiguous
timeout; ledger recovery; bounded submit/poll/fallback; circuit breaker;
provider outage does not stop queue; shared Gemini two-call guard; exactly one
terminal persistence; no quota double-charge.

Compatibility: unchanged Build 3 poll select/status/result fields; existing
success recipe shape; normalized error persists to code/message/event; failed
job releases implicit reservation; metadata minimal/full behavior remains or
improves.

## 17. Required owner inputs

For each provider candidate, supply only:

1. Provider endpoint identifier/product/version (not a secret URL containing a
   token).
2. Request method and non-secret parameters.
3. RapidAPI host identifier.
4. Minimal sanitized success response.
5. Sanitized async accepted, progress, and completed responses if applicable.
6. Sanitized invalid URL and private/deleted responses.
7. Sanitized rate-limit response and documented rate-limit headers.
8. Sanitized provider/server failure, timeout behavior, malformed, and
   authentication/configuration responses.
9. Plan limits, billable-call definition, retry/poll billing, rate limits, and
   cost per unit supplied by the owner.
10. Direct URL expiry behavior and whether a status lookup can recover an
    ambiguous request.
11. Available qualities, size/duration/dimensions fields, and whether returned
    media includes audio.
12. Documented CDN hosts/redirect behavior/content types for allowlisting.
13. Separate development/production application and key readiness.

Do not provide an actual key in Codex output, fixtures, chat, source, tests, or
documentation.

## 18. Definition of done and unresolved decisions

Next integration is done only when disabled/fixture tests pass; atomic claim,
paid-call ledger, Gemini guard, SSRF-safe fetch, ffprobe validation, cleanup,
normalized errors, safe events, and development-canary Sentry are verified; one
selected development test user completes success and controlled failures; Build
3 polling/quota tests pass; and no direct URL/key/body/user ID appears in any
observable or persistent lane.

Unresolved owner decisions:

- verified Instagram/TikTok provider contracts and pricing/limits
- whether providers support idempotency/status lookup and async polling
- verified CDN host allowlist and expiry behavior
- owner-approved external call/cost ceilings and stability-window duration
- ffprobe image-size/startup tradeoff on Render Free
- whether initial metadata cache remains JSON-backed or is deferred until a
  separately approved indexed cache design
- final Build 4 copy for normalized resolver failures

## 19. M23.W3C implemented architecture

The active queue now uses a provider-neutral package under
`app/media_resolvers/`. Queue business logic selects a strategy and receives a
verified temporary MP4; it does not import provider response fields.

The exact current metadata path is Build 3 save -> immediate minimal
`recipe_items` row -> background `refresh-recipe-metadata` -> protected worker
`/metadata-preview` -> public YouTube oEmbed or social yt-dlp with
`download=False` -> OpenGraph fallback -> existing item fields -> app polling.
The worker no longer supplies a cookie file to metadata preview. This path does
not create a media job, call an external media adapter or Gemini, or reserve AI
quota.

The exact current AI path is create-media-job server access check -> queued row
and access event -> atomic queue claim -> stored-platform routing -> YouTube
public URL directly to Gemini, or authorized Instagram/TikTok external adapter
-> guarded temporary A/V media -> existing Gemini Files analysis -> existing
recipe-card/job persistence. Terminal failure still writes the existing Build
3 code/message/event fields once, and failed jobs remain outside successful and
active quota counts.

```text
server-authorized queued job
  -> atomic queued/resolving claim
  -> YouTube direct Gemini, or social route decision
  -> registered adapter chain (maximum primary + one fallback)
  -> guarded provider JSON request
  -> normalized candidates
  -> H.264 MP4 candidate selection
  -> guarded URL alternatives/redirects/download
  -> ffprobe
  -> optional audio download + ffmpeg mux
  -> final ffprobe and size/duration checks
  -> existing Gemini Files and recipe-card persistence
  -> existing completed/failed quota semantics
```

Implemented adapters:

- `rapidapi_instagram_reels13`: `GET /download`, configured RapidAPI host/key,
  normalized `medias` entries, and split-A/V whenever an audio entry is present.
  MP4 codec may be unknown at resolution and is therefore probe-gated.
- `akng_tiktok`: `GET /tiktok/video`, configured server token, structured
  `bitrateInfo` and `bitrateAudioInfo` only. The broad `download_urls` list is
  intentionally ignored.

No actual provider response, source URL, media URL, token, title, or body was
copied into fixtures or observability. The supplied TikTok token appeared in
user-provided input and must be revoked/rotated before development traffic.

### Implemented flags and configuration

Committed safe defaults and production remain:

```text
MEDIA_RESOLVER_MODE=legacy
EXTERNAL_MEDIA_RESOLVER_AUDIENCE=disabled
INSTAGRAM_RESOLVER_CHAIN=[]
TIKTOK_RESOLVER_CHAIN=[]
COOKIE_MEDIA_FALLBACK_ENABLED=false
YOUTUBE_DIRECT_GEMINI_ENABLED=true
PREFERRED_VIDEO_SIZE_MB=36
MAX_VIDEO_SIZE_MB=50
MAX_VIDEO_DURATION_SECONDS=120
EXTERNAL_PROVIDER_TIMEOUT_SECONDS=15
EXTERNAL_MEDIA_DOWNLOAD_TOTAL_TIMEOUT_SECONDS=120
```

The owner-requested development config is committed in
`config/worker.development.json`:

```text
MEDIA_RESOLVER_MODE=external
EXTERNAL_MEDIA_RESOLVER_AUDIENCE=all_authorized_ai
INSTAGRAM_RESOLVER_CHAIN=rapidapi_instagram_reels13
TIKTOK_RESOLVER_CHAIN=akng_tiktok
COOKIE_MEDIA_FALLBACK_ENABLED=false
YOUTUBE_DIRECT_GEMINI_ENABLED=true
```

These are config values, not Render environment variables. Only secrets remain
in the development Render environment:

```text
INSTAGRAM_RAPIDAPI_KEY=<development secret>
TIKTOK_AKNG_ACCESS_TOKEN=<rotated development secret>
```

`INSTAGRAM_RAPIDAPI_HOST`, `INSTAGRAM_RAPIDAPI_BASE_URL`,
`TIKTOK_AKNG_BASE_URL`, and the provider-specific CDN host suffixes are
non-secret config JSON. Startup fails closed for unknown modes/adapters,
incompatible platform chains, missing active credentials, non-HTTPS/mismatched
provider endpoints, or missing media-host policy. Startup logs only modes,
adapter IDs, and credential-present booleans.

### Selection and split-stream policy

- Candidate URLs stay in repr-hidden, compare-hidden fields with explicit safe
  serialization. TikTok candidates keep at most three structured URL
  alternatives and try them sequentially through the same safety gate.
- Selection permits H.264/AVC MP4 up to a 720 short edge. Instagram MP4 with an
  unknown codec remains probe-gated. Explicit H.265/DASH is excluded for now.
- Candidates within 36 MiB outrank over-target candidates. Within target, the
  best supported resolution wins. The supplied fixture therefore selects the
  24,828,499-byte H.264 576x1024 candidate rather than either 47+ MB candidate.
- The video is probed first. Embedded audio avoids a merge. Otherwise the
  highest useful bounded audio candidate is downloaded and ffmpeg first tries
  stream copy, then bounded H.264/AAC conversion at maximum 720p. The final file
  must probe as video plus audio and remain at or below 50 MiB and 120 seconds.

The Docker image now installs `ffmpeg`, which also supplies `ffprobe`. External
traffic fails closed when either required tool is unavailable.

### Security, attempts, and cleanup

- Provider JSON calls disable environment proxies, use explicit connect/read
  timeouts, cap JSON at 2 MiB, require JSON content type, never follow provider
  redirects, and wrap all failures without response bodies or request URLs.
- Media fetches require HTTPS/443 and a configured provider-specific host
  suffix. Every DNS answer and connected peer must be public/global. Redirects
  are manual, limited to two, and fully revalidated.
- Actual streamed bytes enforce 50 MiB even when `Content-Length` is absent or
  false. Content type plus MP4/WebM signature is checked before ffprobe.
- Temporary directories are randomized `0700`, files are `0600`, partials are
  deleted, queue cleanup removes success/failure files, and startup removes only
  stale `external-*` directories beneath the exact temp root.
- Queue claim is now conditional on `status=queued`. A job event ledger blocks a
  second paid call to the same adapter. One adapter call plus one configured
  fallback is the maximum. A five-failure/two-minute local circuit opens for 60
  seconds; configuration failures open immediately.
- Every active queue Gemini `generate_content` call now uses the existing
  repository count immediately before execution. The existing two-call job
  ceiling and development daily/model limits therefore cover primary,
  repair/fallback, translation, YouTube, and social calls.

### Metadata, entitlement, errors, and observability

`/metadata-preview` no longer loads platform cookies. It remains
`download=False`, public yt-dlp/oEmbed/OpenGraph only and never invokes an
external paid adapter. The app repository still inserts a minimal saved row
before refresh; no mobile change was required. A later app/backend change may
add shared metadata caching and preserve the canonical hash during metadata
merges, as identified in the audit.

`all_authorized_ai` does not grant access. The worker reads the existing
server-written `media_job_created.metadata.ai_generation_access` object and
requires `allowed=true`. Free-credit, Premium, and test/admin routes are tagged
only after this check. Failed jobs remain neither active nor successful and do
not consume successful-generation quota.

New terminal codes use existing Build 3 fields and its generic fallback:
`media_unavailable`, `media_private_or_deleted`, `unsupported_media`,
`media_has_no_audio`, `resolver_rate_limited`,
`resolver_temporarily_unavailable`, `resolver_configuration_error`,
`media_download_failed`, `media_validation_failed`, `media_merge_failed`,
`youtube_processing_failed`, and `ai_provider_failed`. Existing
`media_size_exceeded` and duration behavior are unchanged. Differentiated copy
still requires Build 4.

Safe events are `resolver_route_selected`, `external_resolver_started`,
`external_resolver_completed`, `external_resolver_failed`,
`media_candidate_selected`, `external_media_download_started`,
`external_media_download_completed`, `media_probe_completed`,
`media_merge_started`, `media_merge_completed`, and
`external_media_validated`. Only normalized adapters, route classes, attempts,
status/quality/resolution/size/duration/latency buckets, modes, and booleans are
allowed.

Provider exhaustion/rate limits produce one grouped Sentry warning after the
chain. Configuration/contract/download/probe/merge technical errors produce one
sanitized error. Expected private/deleted/invalid outcomes normally do not.
Development runtime Sentry requires explicit `SENTRY_DEV_CANARY=true`, a valid
development DSN, Render, and branch `develop`; it remains off otherwise.

### Enablement and rollback

Development live test, only after rotating the exposed token and installing
development secrets:

1. Run all offline tests and secret scan.
2. Push reviewed `develop`; let only the development Render service build.
3. Confirm `/health.safe_config` shows external/all-authorized, both adapter
   IDs, cookie fallback false, direct YouTube true, and credential booleans true.
4. Optionally set `SENTRY_DEV_CANARY=true` only for the bounded canary.
5. Run one authorized test-user Instagram job, then one TikTok job under 120
   seconds. Verify safe events, one provider attempt, final A/V validation,
   unchanged recipe-card result, quota, cleanup, and no URL/body/token leakage.
6. Disable Sentry canary again after evidence is collected.

The first production canary remains server-selected test users. After the
development evidence is reviewed, change only production config to external,
`test_users`, the verified adapter chains/hosts/suffixes, cookie fallback false,
and direct YouTube true; add separate production secrets in Render. Do not use
`all_authorized_ai` for the first production canary.

Rollback requires no mobile release: set the affected environment audience to
`disabled`. If external traffic must stop while no-cookie legacy remains, set
mode `legacy`. Cookie rollback additionally requires
`COOKIE_MEDIA_FALLBACK_ENABLED=true` and the existing platform cookie settings;
it remains false by default. YouTube can be independently disabled through its
flag.

Remaining owner verification before live development traffic:

- revoke/rotate the exposed TikTok token and confirm separate dev/prod keys
- confirm provider plan limits, billing rules, and rate-limit headers
- provide sanitized 401/403, 404/private, 429, 5xx, and malformed real shapes
- confirm the committed CDN suffix families cover every legitimate redirect
  without adding a broad Internet allowlist
- confirm whether Instagram video entries ever guarantee embedded audio (the
  worker probes regardless)
- confirm AKNG token/query contract and whether a status/idempotency endpoint
  exists for ambiguous provider timeouts
