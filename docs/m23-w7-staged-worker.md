# M23.W7A-W7C Staged Combined Worker

## Current Monolithic Execution Map

The pre-refactor queue is one asynchronous polling task around one synchronous
job function:

1. `app.main.start_queue_worker()` runs at FastAPI startup. It validates config,
   sweeps old temp directories, and calls `ensure_queue_worker_started()`.
   `GET /wake` calls the same starter without claiming inline.
2. `app.queue_worker.run_queue_loop()` is asynchronous. Each iteration delegates
   `process_next_queued_job()` to `asyncio.to_thread()`, so the FastAPI event loop
   stays available while one synchronous worker thread owns the only slot.
3. `MediaJobRepository.claim_oldest_queued_job()` first selects the oldest
   `queued` row, then performs a conditional `id + status=queued` update to
   `resolving`. The second statement is the atomic compare-and-set. There is no
   lease owner, lease expiry, attempt token, or heartbeat.
4. `process_queue_job()` owns the slot until terminal cleanup. It routes by
   `platform_for_job()`, records the reservation audit event, and runs either:
   - YouTube: `process_youtube_gemini_url_job()`, or
   - social: `resolve_queue_media()` followed by thumbnail and metadata writes.
5. External social ingest runs through
   `media_resolvers.pipeline.resolve_external_media()`. Provider HTTP, guarded
   download, `media_processing.probe_media()`, optional
   `media_processing.mux_video_audio()`, and final probe are synchronous.
   Legacy ingest runs `social.stream_download.extract_social_video()` and
   synchronous yt-dlp/Requests/ffprobe/ffmpeg helpers.
6. `upload_thumbnail_best_effort()`, `save_metadata()`, and
   `update_job_source_fields()` perform synchronous HTTP/storage/database waits.
   Provider URLs remain ephemeral and are no longer stored in private metadata.
7. `run_gemini_video_poc_analysis()` performs synchronous Files API upload,
   `wait_for_gemini_file_active()` polling with `time.sleep()`, structured
   extraction, optional bounded repair/fallback, and previously recipe saving.
   `guarded_generate_content()` preserves the repository-backed per-job call
   ceiling before every provider request.
8. `save_gemini_recipe_card()` normalizes/validates, inserts the recipe card,
   and updates the job to `completed`. Failure uses
   `record_queue_failure_with_reporting()` and updates to `failed`.
9. `finalize_quota_transition_safely()` records exactly one success/release
   transition; replay records a separate idempotent audit event.
10. `process_queue_job()` finally deletes local media and closes buffers.
    Only after it returns can `run_queue_loop()` sleep and claim the next job.

All Supabase `.execute()`, Requests calls, Gemini SDK calls, yt-dlp operations,
`time.sleep()`, and subprocess calls are synchronous. In sequential mode they
run inside the one `asyncio.to_thread()` call. Direct subprocess boundaries are
yt-dlp, ffprobe, ffmpeg mux/optimization, and thumbnail frame sampling.

The cross-stage objects are: durable job reference; claim/attempt reference;
normalized platform; safe resolver category; materialization mode; in-memory
local media path; safe size/duration/dimension categories; bounded source
context; uploaded Gemini file object/reference; structured recipe; thumbnail
result; cleanup obligations; and timestamps. Raw source/provider URLs are not
stage payload fields.

Before this milestone a crash left the row in `resolving`, `downloading`, or
`analyzing` forever. Startup removed sufficiently old recognized temp
directories but did not requeue the row. The implicit quota reservation also
remained active because status remained active.

## Final Boundaries

- Dispatcher: resource admission, oldest-job conditional claim, reservation
  audit, and ingest enqueue.
- Ingest: resolver/download/probe/mux, thumbnail selection/upload, public and
  private metadata, then analysis enqueue.
- Analysis: Gemini upload, ACTIVE wait, local-file deletion, structured
  extraction, then finalization enqueue.
- Finalization: recipe normalization/save, terminal job update, quota
  finalization, terminal event, and idempotent local cleanup.
- Recovery/cleanup: startup stale-directory sweep, per-stage failure cleanup,
  and durable-transport/lease capability reporting.

`app.pipeline.contracts` defines `DispatchEnvelope`, `IngestedMedia`,
`GeminiAnalysisInput`, `GeminiAnalysisResult`, and `FinalizationInput`.
Runtime-only job snapshots, paths, thumbnail URLs, source metadata, and clients
are excluded from serialization. The durable job row remains authoritative, so
future durable delivery can reconstruct ephemeral inputs instead of persisting
source/CDN URLs or local paths.

## Transport and Startup

`StageTransport` is the internal interface.
`InProcessStageTransport` uses bounded `asyncio.Queue` instances.
`SupabaseStageTransport` is an explicit unimplemented boundary and fails closed.

Render needs one service and one process:

```bash
python -m app.workers.combined_worker
```

FastAPI still exposes `/health`, `/ping`, `/wake`, `/metadata-preview`, debug
routes, and queue-compatible `/process-job`. In `sequential` mode startup runs
the previous queue loop. In `staged` mode it starts one dispatcher, configured
ingest/analyzer/finalizer tasks, and a watchdog. Shutdown stops dispatch,
boundedly drains queues, then cancels idle stage tasks.

These future commands are importable but deliberately refuse standalone
startup:

```bash
python -m app.workers.ingest_worker
python -m app.workers.analyzer_worker
python -m app.workers.finalizer_worker
```

Independent deployment requires an approved durable transport plus lease owner,
lease expiry, heartbeat, attempt fencing, durable stage, and idempotent result
key support. Setting `WORKER_STAGE_TRANSPORT=supabase` currently reports that
the additive migration/implementation is missing; it does not silently use
process-local queues.

## Defaults and Backpressure

```env
WORKER_PIPELINE_MODE=sequential
WORKER_STAGE_TRANSPORT=in_process
WORKER_DISPATCH_CONCURRENCY=1
WORKER_INGEST_CONCURRENCY=1
WORKER_GEMINI_ANALYSIS_CONCURRENCY=1
WORKER_FINALIZE_CONCURRENCY=1
WORKER_FFMPEG_CONCURRENCY=1
WORKER_GEMINI_UPLOAD_CONCURRENCY=1
WORKER_INGEST_QUEUE_SIZE=1
WORKER_ANALYSIS_QUEUE_SIZE=2
WORKER_FINALIZE_QUEUE_SIZE=2
WORKER_MAX_RSS_MB=448
WORKER_MAX_ACTIVE_TEMP_MB=256
WORKER_MIN_FREE_TEMP_MB=256
WORKER_ADMISSION_RETRY_SECONDS=5
```

The dispatcher checks that the ingest worker itself is free, its bounded queue
has capacity, current RSS is under budget, active temp bytes plus a worst-case
split-A/V reservation fit, free temp storage remains above its floor, and
ffmpeg/upload budgets are available. Failure leaves the durable row `queued`;
it creates no reservation and retries after a bounded delay. The dispatcher
concurrency is fixed at one for FIFO claims.

The estimate is one maximum progressive file, or video + audio + mux output for
split A/V. Known ffprobe/ffmpeg and Files API upload calls use process-wide
bounded slots. No default concurrency exceeds one.

## Temp Lifecycle

Files API upload completion alone is insufficient because Gemini may still be
processing the file. `ACTIVE` proves Gemini can use the uploaded object.
Structured extraction and configured model fallback consume that Gemini object,
not the local path. Therefore the worker deletes local media immediately after
`gemini_file_active`, before structured extraction. It never deletes during
upload. Failure before ACTIVE reaches stage cleanup; later cleanup safely
repeats deletion. Startup recognizes both external temp directories and
UUID-named legacy directories and removes only old recognized paths.

## Crash and Lease Matrix

| Crash point | Durable authority and restart behavior |
|---|---|
| ingest/download/ffmpeg | Source job row remains sufficient to rematerialize; old local files are swept |
| Gemini upload/ACTIVE wait | Restart must rematerialize and upload again; Gemini/local orphan cleanup is best effort |
| structured extraction | Restart can rematerialize; call events retain the per-job budget |
| finalization | Terminal quota events are idempotent; recipe insert fencing is not proven without an idempotency key |
| cleanup | Cleanup is idempotent and stale files are swept |

Correct automatic reclaim is not claimable with the current schema. The exact
missing additive capability is:

- `lease_owner` and `lease_expires_at`;
- `heartbeat_at`;
- monotonic `attempt_no` or opaque `attempt_token` used to fence every write;
- durable `pipeline_stage`;
- an idempotency key/unique constraint for recipe result insertion.

Without those fields, reclaiming by `started_at` could steal a live long-running
job during a rolling deploy. No unsafe reclaim was added and no migration was
created. Combined in-process staged mode is suitable for overlap testing, but
durable crash recovery and separate services remain blocked pending approval.

## Observability and Field Definitions

New safe events are `worker_pipeline_started`, `worker_stage_enqueued`,
`worker_stage_started`, `worker_stage_completed`, `worker_stage_failed`,
`worker_stage_backpressure`, `worker_stage_recovered`, and
`worker_pipeline_shutdown`. Metadata is restricted to stage/mode/concurrency,
queue-depth/active/RSS/temp/latency buckets, recovery category, and lease-state
category. No event contains an ID, URL, filename, prompt, response, token,
temporary path, or user identifier.

Corrections:

- `probe_performed` means a successful final ffprobe supplied the reported A/V
  facts. External validated media now sets it true.
- `thumbnail_source_present` means a thumbnail source was selected or a
  thumbnail was uploaded. It does not depend on retaining a redacted provider
  URL.
- `successful_count` is a database snapshot. `quota_count_observed_at` now says
  `reservation_after_claim`, `terminal_after_status_update`, or
  `terminal_replay`. Counts can correctly advance between job creation and
  claim when other jobs complete.

## Offline Performance and QA

Verified live sequential baseline supplied by the owner: a 12.3 MiB, 84-second
TikTok job completed in about 51.2 seconds, including successful
resolve/download/probe, Files API ACTIVE wait, one structured call, result
save, quota commit, and cleanup.

The deterministic staged manifest reports:

- seven of seven terminal; five expected completions and two expected failures;
- FIFO claim/enqueue order and bounded queue backpressure;
- maximum active ingest/analyzer/finalizer `1/1/1`;
- ingest/analyzer overlap observed;
- zero active reservations after drain and cleanup true;
- peak RSS bucket `64_127mb` and temp bucket `under_64mb` on the local run;
- concurrency-two probe: maximum analyzers `2`, ingest/finalizer remain `1`,
  all seven terminal, per-job call budgets isolated, and one failure does not
  cancel another.

Offline stage milliseconds are synthetic harness timings, not provider
benchmarks. After pipeline fill, ideal cycle time approaches the slowest stage
rather than the sum of ingest + analysis + finalization, while each job's
latency remains approximately the sum. No Render throughput claim is made
without owner-run RSS/temp/provider measurements.

## Development Deployment Procedure (Owner Only)

Codex did not execute this procedure:

```bash
cd /Users/sadaqatali/Documents/AiAppBuilder/recipe-dost/recipe-dost-media-worker
git checkout develop
git pull --ff-only origin develop
make check-dev
git push origin develop
```

In the development Render service only:

1. Set the start command to `python -m app.workers.combined_worker`.
2. First deploy with `WORKER_PIPELINE_MODE=sequential`; verify `/health`,
   `/ping`, `/wake`, and one existing platform smoke.
3. Set `WORKER_PIPELINE_MODE=staged`; leave every concurrency value at `1`.
4. Deploy the same `develop` revision. Verify `/health.safe_config` reports
   staged/in-process and the expected queue sizes.
5. Do not modify production or `main`.

## Owner Soak Procedure

1. Submit the known bounded batch: two Instagram and two TikTok Premium jobs,
   back-to-back, with analyzer concurrency `1`.
2. Observe FIFO claims, ingest/analyzer overlap, one terminal status/job, one
   terminal quota event/job, zero active reservations after drain, no signed
   URL persistence, and temp deletion after ACTIVE.
3. Record per job: queue wait, ingest, upload/ACTIVE, structured extraction,
   finalization, total duration, max active stages, peak RSS/temp buckets, and
   cleanup.
4. Repeat restart tests separately during ingest, upload/ACTIVE wait,
   extraction, finalization, and cleanup. Until the lease migration exists,
   manually reset only owner-selected development test jobs; do not represent
   this as automatic lease recovery.
5. Only after concurrency-one resource results pass, set
   `WORKER_GEMINI_ANALYSIS_CONCURRENCY=2` in development. Keep ingest, ffmpeg,
   upload, and finalizer concurrency at `1`; run the same bounded batch and
   compare RSS/temp/provider errors. Do not enable concurrency two on
   production or claim Render safety from offline QA.

## Rollback

Set `WORKER_PIPELINE_MODE=sequential` and redeploy the same development
revision, or redeploy the previous development revision. No database rollback
is required because this milestone adds no schema.

