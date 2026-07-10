# M22E Professional UX Recovery

## M22E.1R-B: AI-Generated Tags End-to-End

Status: implementation is pushed to `develop` as `d3b29b3`; development Render
deployment observation and one disposable development media-job verification are
blocked in this workspace because the Render dashboard is not signed in. The
milestone must not be marked done until that verification is recorded.

The active user-facing path is `media_jobs -> queue worker -> recipe_cards`.
The prior queue prompt already named `tags`, but the intermediate result model
dropped the list and the `recipe_cards` mapper did not write `tags_json`.

M22E.1R-B keeps quota/access logic, RevenueCat, package IDs, video limits, and
media handling unchanged. It updates only the active queue structured prompt,
the result contract, safe tag normalization, and the existing recipe-card insert
payload. The legacy `generate-card` path is not changed.

Acceptance for a successful development media job:

- The linked `recipe_cards.tags_json` contains 5-8 lowercase tags.
- Tags are unique, trimmed, no longer than 24 characters, useful for searching,
  and contain no generic `food`/`recipe`/`video`, raw handles, or URLs.
- The app can render and search the stored tags; an older card with `[]` remains
  readable without an error.

Development rollout:

1. Run the worker checks and the app's read-only static/environment checks.
2. Commit and push `develop`; allow only `recipe-dost-media-worker-dev` to deploy.
3. Confirm the development worker health endpoint has the expected queue config.
4. Create one disposable development AI media job using an authorized/public
   recipe video under the two-minute beta limit.
5. After the job completes, inspect only the linked card's tag-array shape/count
   and confirm `tags_json` has 5-8 compliant values. Do not log the source URL,
   recipe payload, or secrets.
6. Do not deploy or smoke production as part of this milestone.
