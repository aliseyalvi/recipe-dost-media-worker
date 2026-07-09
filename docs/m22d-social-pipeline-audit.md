# M22D.5C Social Pipeline Audit

Status date: 2026-07-08.

M22D.5C was an audit-only note. M22D.5D adds the worker-only queue compatibility fix described in the addendum below, without changing resolver internals, env files, Supabase logic, quota/access logic, or app repo code.

## Executive Finding

The repo has two social-job paths:

- Primary production path: `WORKER_EXECUTION_MODE=queue` starts the queue loop on FastAPI startup or `/wake`. YouTube goes through oEmbed plus Gemini native URL input. TikTok and Instagram go through unified yt-dlp temp-file extraction, then Gemini Files API.
- Legacy/manual/debug path: `/process-job` schedules `JobRunner.run()` in a FastAPI background task. In that path, TikTok is hard-routed to TikWM text-first, and Instagram checks `ENABLE_RENDER_INSTAGRAM_RESOLVER` before SnapInsta.

The observed production failures match the legacy/manual path, not the intended queue path:

- `tikwm_resolver_started` is emitted only by `app/job_runner.py` in `_run_tiktok_text_first()`.
- `instagram_resolver_disabled` is emitted by the legacy/manual Instagram resolver gate, not by the queue unified extractor.
- `background_worker_error` is emitted by `app/main.py` when a `/process-job` background task raises an unexpected exception.

`/metadata-preview` is separate from both full AI paths. It uses YouTube oEmbed first, social yt-dlp metadata-only second, and OpenGraph fallback. A 200 from metadata preview does not prove the full media-job AI resolver path is configured correctly.

## Evidence Summary

- `docs/worker-context.md` says the working production path is queue mode, with YouTube using Gemini URL input and TikTok/Instagram using unified extraction to a verified temp file.
- `README.md` says `/process-job` is legacy/manual/debug and production queue mode is primary.
- `docs/decision-log.md` records older TikTok/TikWM and Instagram/Facebook disabled Render POC decisions, then later queue-mode unified extraction decisions.
- `git log -S "tikwm"` shows TikWM was introduced as debug/legacy social resolver first, then retained in later mock/text-first paths.
- `git log -S "ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR"` shows unified extraction was introduced by `9077fc2 Add queue worker unified yt-dlp POC` and later promoted for production cutover.
- `git log -S "metadata-preview"` shows preview was added separately in `2bac1db Add safe worker metadata preview endpoint`.
- Current `app/metadata_preview.py` confirms preview uses yt-dlp metadata-only/OpenGraph and never calls Gemini.
- Current `app/queue_worker.py` confirms queue jobs use YouTube Gemini URL for YouTube and unified extractor for other platforms when enabled.
- Current `app/job_runner.py` confirms `/process-job` TikTok uses TikWM and Instagram uses legacy resolver gates.

## Table A - Intended Original Workflow

| Platform | Original primary resolver | Original fallback resolver | Required flags/config | Required secrets/cookies | Evidence source file/commit/doc | Confidence |
|---|---|---|---|---|---|---|
| YouTube | Gemini native URL input after oEmbed metadata in queue mode | yt-dlp metadata in preview/mock/legacy paths | `WORKER_EXECUTION_MODE=queue`, `YOUTUBE_AI_INPUT_MODE=gemini_url`, `AI_ANALYSIS_MODE=gemini_video_poc` | Gemini API key for AI; no cookies by default | `docs/worker-context.md`, `README.md`, `57388e1`, `4bcd1d6` | High |
| TikTok | Queue unified yt-dlp extractor to verified temp file, then Gemini Files API | Legacy/manual TikWM text-first; older TikWM video fallback behind social-video flag | `WORKER_EXECUTION_MODE=queue`, `ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR=true`, `MEDIA_EXTRACT_OUTPUT_MODE=temp_file`, `AI_ANALYSIS_MODE=gemini_video_poc` | Gemini API key; platform cookies optional depending on source access | `docs/worker-context.md`, `README.md`, `9077fc2`, `85337d6`, `4bcd1d6` | High |
| Instagram | Queue unified yt-dlp extractor to verified temp file, then Gemini Files API | Older SnapInsta mock/legacy resolver; older Cobalt history before SnapInsta removal | Same as TikTok; Instagram cookies may be needed for private/authorized reels | Gemini API key; `INSTAGRAM_COOKIES_B64` only if cookie mode is enabled/needed | `docs/worker-context.md`, `README.md`, `9077fc2`, `a7f4d97`, `4bcd1d6` | High |
| Facebook | Disabled/controlled failure until separately validated | SnapSave legacy/mock resolver | `ENABLE_RENDER_FACEBOOK_RESOLVER=false` unless explicitly validating | Facebook cookies only if future support requires them | `docs/milestones.md`, `docs/social-video-debug.md` | Medium |
| Metadata preview | YouTube oEmbed; social yt-dlp metadata-only | OpenGraph fallback | `metadata_preview_enabled`; uses request platform/host detection | Optional platform cookies only as presence; no AI key needed | `app/metadata_preview.py`, `2bac1db`, `README.md` | High |

## Table B - Current Production Effective Workflow From Code/Config

| Platform | Current selected resolver | Why selected | Flags/config involved | Possible env override | Failure observed |
|---|---|---|---|---|---|
| YouTube queue job | YouTube oEmbed plus Gemini native URL | `queue_worker.should_use_youtube_gemini_url()` matches platform YouTube, `AI_ANALYSIS_MODE=gemini_video_poc`, and `YOUTUBE_AI_INPUT_MODE=gemini_url` | JSON defaults/prod: queue, Gemini POC, YouTube Gemini URL | Any Render env var named `WORKER_EXECUTION_MODE`, `AI_ANALYSIS_MODE`, `YOUTUBE_AI_INPUT_MODE` overrides JSON | Completed successfully |
| TikTok queue job | Unified yt-dlp stream extractor | Queue path skips TikWM and calls `extract_social_video()` for non-YouTube platforms | `ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR=true`, `MEDIA_EXTRACT_OUTPUT_MODE=temp_file` | Render env can override either key and disable/change queue behavior | Not the observed failure path |
| TikTok `/process-job` job | TikWM | `JobRunner.run()` checks `platform == "tiktok"` first and calls `_run_tiktok_text_first()`, which emits `tikwm_resolver_started` | `enable_tikwm_resolver`, `tikwm_enabled_platforms`, `tikwm_api_url`; not unified extractor | Render env can override `WORKER_EXECUTION_MODE` to webhook or Edge Function can call `/process-job`, bypassing queue intent | `tikwm_resolver_started` then `NameError`/`background_worker_error` |
| Instagram queue job | Unified yt-dlp stream extractor | Queue path has no Instagram resolver-disabled gate; if unified extractor is enabled, it calls `extract_social_video()` | `ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR`, cookie flags if cookies needed | Render env can override `ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR=false` | Earlier `unified_extractor_failed` if disabled |
| Instagram `/process-job` job | SnapInsta gate before resolver | `JobRunner._run_social_video()` and `simple_resolvers.resolve_with_snapinsta()` check `enable_render_instagram_resolver` | `ENABLE_RENDER_INSTAGRAM_RESOLVER` | Render env can override JSON with `ENABLE_RENDER_INSTAGRAM_RESOLVER=false`; pre-M22D.5A production JSON did not set it | `instagram_resolver_disabled` |
| Metadata preview | YouTube oEmbed or social yt-dlp metadata-only/OpenGraph | `fetch_metadata_preview()` never calls `JobRunner` or `queue_worker` | metadata preview code path only | Cookie env may affect metadata access, but preview success does not imply AI job path success | 200 OK despite media job failures |

## Table C - Dev/Prod Parity Gaps

| Setting/behavior | Dev expected | Prod current | Impact | Minimal fix |
|---|---|---|---|---|
| Full AI job entrypoint | Queue loop claims `queued` jobs; `/process-job` only legacy/manual/debug | Observed events imply `/process-job` background path is still being used for social jobs | TikTok goes to TikWM; Instagram hits disabled SnapInsta gate | Stop triggering `/process-job` for production create-media-job flow; use queue loop and `/wake` only |
| `WORKER_EXECUTION_MODE` | `queue` | JSON says queue; Render env may override; deployed revision may predate config fix | If not queue, startup loop may not process jobs and manual trigger path dominates | Verify `/health.safe_config.worker_execution_mode` after redeploy; remove old non-secret Render env override after smoke |
| `ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR` | `true` | Defaults true; pre-fix or env override may disable | Queue social jobs fail before extraction with `unified_extractor_failed` | Keep true in JSON; remove conflicting Render env override |
| Instagram resolver gate | Not needed for queue unified extractor; true only if legacy path remains reachable | Observed `instagram_resolver_disabled` means legacy path saw false | Legacy/manual Instagram fails before any extraction | Prefer queue path; if keeping `/process-job`, set legacy gate consistently or block social processing there |
| TikTok resolver choice | Queue unified extractor for production AI jobs | Observed `tikwm_resolver_started` means legacy/manual path | TikTok uses old text-first/TikWM path and hit NameError | Route production jobs through queue; keep M22D.5B NameError fix if legacy path remains |
| Metadata preview | Metadata-only preview, no Gemini, no job processing | Works | Can mask full AI path failures | Treat preview as separate smoke test only |
| Config source of truth | Non-secret runtime config in JSON; secrets in env | Env variables still override JSON for compatibility | Old Render non-secret env can silently undo JSON fixes | Audit Render env group keys after deploy; remove non-secret overrides only after smoke passes |

## Table D - Recommended Minimal Patch Plan

| Issue | Fix | Files to change | Risk | Test to verify |
|---|---|---|---|---|
| Production social jobs appear to use legacy `/process-job` path | Change production trigger flow so create-media-job leaves jobs queued and wakes queue only; do not call `/process-job` for normal production social jobs | Likely app/Edge Function outside this repo; worker docs only here | Medium; changes orchestration not resolver internals | New TikTok/Instagram jobs emit `queue_job_claimed`, then `unified_extractor_started`, not `tikwm_resolver_started` or `platform_resolver_started` |
| Old Render env non-secret overrides can defeat JSON | Remove/neutralize deprecated non-secret worker env keys from Render env group after verifying deployed `/health.safe_config` | Render env group, not committed env examples | Medium; bad removal can change runtime unexpectedly | `/health.safe_config` shows queue=true path, unified extractor true, raw URL logging false, duration <=120 |
| Instagram legacy gate still fails if `/process-job` remains reachable | Either keep legacy gate true for parity or make `/process-job` reject/social-debug-only for production social platforms | `config/worker.production.json` if keeping gate; `app/job_runner.py` only if intentionally changing legacy behavior in a future patch | Low for config; medium for code | Legacy Instagram either controlled-rejects with a clear message or routes consistently |
| TikTok legacy path NameError | Keep the M22D.5B `resolver_started_at` fix if `/process-job` remains callable | `app/job_runner.py` | Low; no resolver behavior change, only controlled error handling | TikTok legacy path no longer produces `NameError`; if it fails, event has safe resolver error |
| Need safe prod observability | Keep `/health.safe_config` if available; do not add secrets | `app/main.py`, `app/schemas.py` | Low if fields remain non-secret | `/health.safe_config` contains booleans/model names only and no URLs/cookies/payloads |

## Direct Answers

### Is TikWM supposed to be primary, fallback, or legacy/debug only?

Historically, TikWM was primary for the W-M14R1/W-M15 legacy `/process-job` TikTok text-first path. For the later production queue cutover, TikWM is not the intended primary AI job resolver; TikTok production AI jobs should use the queue unified yt-dlp extractor, then Gemini Files API. TikWM remains legacy/manual/debug unless explicitly using the old `/process-job` path.

### If TikWM is fallback only, why is production routing to it?

Production is routing to TikWM because the observed job entered `JobRunner.run()` through `/process-job`/FastAPI BackgroundTasks. In that code path, `platform == "tiktok"` unconditionally calls `_run_tiktok_text_first()`, which emits `tikwm_resolver_started` and calls `resolve_social_video(..., resolver="tikwm")`. Queue-mode TikTok jobs do not emit `tikwm_resolver_started`.

### What was the working dev TikTok pipeline?

The current documented working dev/production-intended pipeline is: queued job -> queue loop claim -> unified yt-dlp extractor -> verified temp file -> Gemini Files API -> structured recipe card -> temp cleanup. Older dev milestones also had TikWM text-first and mock fixture paths, but those are legacy/manual/debug relative to the queue production target.

### What was the working dev Instagram pipeline?

The current documented working pipeline is: queued job -> unified yt-dlp extractor -> verified temp file -> Gemini Files API -> structured recipe card. Instagram may need platform cookies in env, but cookie values must remain env-only. Older Instagram mock/legacy paths used SnapInsta, and even older docs mention Cobalt.

### Are metadata-preview and process-job using different resolver paths?

Yes. `metadata-preview` uses YouTube oEmbed, social yt-dlp metadata-only, then OpenGraph fallback, and never processes a media job or calls Gemini. `/process-job` uses `JobRunner`, where TikTok routes to TikWM and Instagram can hit `instagram_resolver_disabled`. Queue jobs use `queue_worker` and unified extraction for social platforms.

### Which exact production config value caused `instagram_resolver_disabled`?

In the legacy/manual resolver path, `settings.enable_render_instagram_resolver == false` causes `instagram_resolver_disabled`. That can come from code/default false, an older `config/worker.production.json` lacking `ENABLE_RENDER_INSTAGRAM_RESOLVER=true`, an older deployed revision, or a Render env override setting `ENABLE_RENDER_INSTAGRAM_RESOLVER=false`.

### Which exact production config value caused `unified_extractor_failed`?

In queue mode, `settings.enable_unified_ytdlp_stream_extractor == false` causes the controlled `unified_extractor_failed` message "Unified social video extraction is not enabled." That can come from `ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR=false` in Render env or from an older/default config before production parity was fixed.

### What minimal config/code change restores the original workflow without adding old messy env vars back?

The minimal restoration is orchestration-first: ensure production creates `queued` jobs and relies on the queue loop plus `/wake`, not `/process-job`, for normal social AI jobs. Keep non-secret runtime settings in `config/worker.production.json`, verify no Render env override disables them, and remove old non-secret Render env overrides after smoke. If `/process-job` must remain callable, keep the M22D.5B NameError fix and either align the Instagram legacy gate or reject social jobs there with a controlled message.

### Should previous M22D.5B changes be kept, reverted, or replaced?

Keep the diagnostic parts: `/health.safe_config` and the TikWM `resolver_started_at` NameError fix are useful and do not change the intended queue resolver selection. Reconsider or replace any M22D.5B change that makes TikWM look like the production primary resolver; TikWM should be documented as legacy/manual/debug for production AI jobs unless a future decision deliberately changes that.

## Next Patch Prompt Recommendation

```text
Milestone: M22D.5D - Force production social AI jobs onto queue path.

Use docs/m22d-social-pipeline-audit.md as source of truth.
Do not add old non-secret flags back to Render env examples.
Preserve YouTube production path.
Patch only worker-side behavior needed to prevent normal production social jobs from using legacy /process-job TikWM/SnapInsta flow, or document the exact app/Edge Function change required if the trigger lives outside this repo.
Keep /metadata-preview unchanged.
Keep secrets env-only.
Run python3 -m compileall app scripts and validate env examples.
```

## M22D.5D Addendum - Implemented Worker Compatibility Fix

Status date: 2026-07-08.

The worker now handles `/process-job` differently when `WORKER_EXECUTION_MODE=queue`:

- It still validates the worker secret and media job.
- It accepts only normal `queued` jobs for queue wake compatibility; already processing, failed, completed, or non-queued jobs return the existing skip-style response instead of entering the legacy direct runner.
- It inserts `process_job_queue_wake_requested` with safe non-secret metadata.
- It starts the queue loop and returns `202`.
- It does not schedule `run_job_background()` or call `JobRunner.run()` in queue mode.

TikTok production AI jobs should therefore use the queue unified yt-dlp path, not TikWM, when they are normal queued jobs. Instagram production AI jobs should use the same queue unified extractor path and should not hit the legacy SnapInsta disabled gate.

Updated minimal patch plan:

| Issue | Fix | Files changed | Risk | Test to verify |
|---|---|---|---|---|
| `/process-job` entered legacy direct runner in queue mode | Queue-mode `/process-job` is now a queue wake compatibility endpoint | `app/main.py` | Low/medium; changes trigger orchestration only | New social jobs emit `process_job_queue_wake_requested`, `queue_job_claimed`, `unified_extractor_started`; no `tikwm_resolver_started` or `instagram_resolver_disabled` |
| Production config implied TikWM was part of production parity | Removed explicit TikWM production overrides and validation requirement | `config/worker.production.json`, `scripts/validate_env.py` | Low; TikWM remains available through legacy/default code if deliberately used | Production validation passes; `/health.safe_config.resolvers.tiktok=queue_unified_ytdlp` in queue mode |
| Need a local guard against regression | Added script-level compatibility check for queue-mode `/process-job` | `scripts/check_process_job_queue_mode.py` | Low | Script returns `process-job queue mode compatibility check passed` |

Next production smoke expected event order for Instagram/TikTok:

1. App/Edge Function creates a `queued` media job.
2. Worker trigger returns success and `/process-job` records `process_job_queue_wake_requested` if that endpoint is still used as the wake mechanism.
3. Queue loop emits `queue_job_claimed`.
4. Social queue path emits `unified_extractor_started`.
5. Gemini file path emits the existing structured extraction events.

Unexpected for normal queue jobs: `tikwm_resolver_started`, `instagram_resolver_disabled`, or `background_worker_error`.

## M22D.5E Addendum - Production Config Loading Fix

Status date: 2026-07-08.

Observed production `/health` after M22D.5D proved the queue wake code was deployed, because `safe_config.process_job_mode=queue_wake_compatibility`. However, non-secret runtime settings still matched code fallbacks rather than `config/worker.production.json`: unified extractor disabled, Instagram/ytdlp cookies disabled, `AI_ANALYSIS_MODE=gemini`, and max duration `90`.

Root cause: the production Docker image did not package `config/`. Render env was clean and contained only env-only keys, so old Render env overrides were not the cause.

Fix: package `config/` in Docker, resolve config from the project root or `WORKER_CONFIG_DIR`, report safe config-source metadata on `/health`, and fail production startup if `worker.production.json` is missing. This protects the intended queue path from silently running with fallback defaults.

Expected after redeploy:

| Field | Expected |
|---|---|
| `safe_config.config_source.loaded_files` | `["worker.defaults.json", "worker.production.json"]` |
| `safe_config.config_source.production_config_loaded` | `true` |
| `safe_config.ai_analysis_mode` | `gemini_video_poc` |
| `safe_config.enable_unified_ytdlp_stream_extractor` | `true` |
| `safe_config.enable_instagram_cookies` | `true` |
| `safe_config.enable_ytdlp_cookies` | `true` |
| `safe_config.max_video_duration_seconds` | `120` |

## M22D.5F Addendum - Production Social Pipeline Proven

Status date: 2026-07-08.

Production worker smoke testing is complete. After M22D.5E, `/health` confirmed `worker.production.json` was loaded from the runtime image and the effective production settings matched the queue/unified-extractor path:

- `production_config_loaded=true`
- loaded files include `worker.defaults.json` and `worker.production.json`
- `worker_execution_mode=queue`
- `process_job_mode=queue_wake_compatibility`
- `enable_unified_ytdlp_stream_extractor=true`
- `enable_instagram_cookies=true`
- `enable_ytdlp_cookies=true`
- `max_video_duration_seconds=120`
- `raw_url_logging_enabled=false`

Production end-to-end results:

| Platform | Result | Proven path |
|---|---|---|
| YouTube | Passed | Queue mode with YouTube Gemini URL path |
| Instagram | Passed | Queue mode with unified yt-dlp temp-file extraction and Gemini Files API |
| TikTok | Passed | Queue mode with unified yt-dlp temp-file extraction and Gemini Files API |

The final root cause was packaging/config loading, not Render env override: the production Render env group contained secrets/environment values only, while the Docker image initially omitted `config/worker.production.json` and fell back to defaults.

Final production worker architecture: one main code branch, separate Render services/env groups for dev and prod. Keep secrets in Render env; keep non-secret production runtime behavior in `config/worker.production.json`.

Next milestone: app repo M22C.3 RevenueCat + Google Play billing setup.

## M22E.0B Addendum - Branch-Safe Release Workflow

Status date: 2026-07-09.

The social pipeline remains unchanged. M22E.0B adds release safety around the proven queue/unified-extractor path:

- Development worker service tracks `develop`.
- Production worker service tracks `main`.
- Dev and prod Render env groups remain separate.
- Render env contains secrets/environment identity only.
- Non-secret resolver, queue, Gemini, duration, and logging behavior stays in JSON config.

Before production smoke testing, run the production environment doctor from `main`. It checks that production config files load and that `worker_execution_mode=queue`, `enable_unified_ytdlp_stream_extractor=true`, `max_video_duration_seconds<=120`, and `raw_url_logging_enabled=false`. It does not inspect or print resolver payloads, cookies, source URLs, stream URLs, prompts, or raw Gemini responses.

Release order for worker changes is now: `develop` -> dev Render smoke -> merge to `main` -> prod Render deploy -> health and platform smoke.
