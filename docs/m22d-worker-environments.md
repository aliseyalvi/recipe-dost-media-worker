# M22D Worker Environments

M22D keeps secrets and environment identity in env files, and moves non-secret worker runtime configuration into committed JSON config files.

## Source Files

- Development source: `env/development.env`
- Production source: `env/production.env`
- Committed templates: `env/development.example.env` and `env/production.example.env`
- Runtime defaults: `config/worker.defaults.json`
- Environment overrides: `config/worker.development.json` and `config/worker.production.json`

Never commit real env values. `env/development.env`, `env/production.env`, root `.env`, and local `.env.*` files are ignored.

If an existing root `.env` is present, do not print or delete it automatically. If it points to the dev Supabase project, migrate those values manually into `env/development.env`, then stop using root `.env` as the shared source of truth.

The old `recipe-dost-media-worker-queue-poc.env` file may contain useful historical dev values, but do not copy it wholesale. Review key names against [docs/m22d-worker-env-variable-map.md](m22d-worker-env-variable-map.md), copy only env-only secrets/environment values, and leave non-secret runtime settings in `config/worker.*.json`.

Do not fill `env/production.env` until the production Supabase project and production service-role key exist.

## Safety Rules

- Never reuse Supabase service-role keys across environments.
- Dev worker must never use the production Supabase service-role key.
- Production worker must never use the development Supabase service-role key.
- Production validation rejects the known dev Supabase ref `xvsiuwjidmkdqzkswoki`.
- Keep `APP_ENV=production` and `LOG_LEVEL=info` in production.
- Keep `ALLOW_RAW_URL_LOGS=false` in config.
- Keep `MAX_VIDEO_DURATION_SECONDS=120` or lower in config.
- Do not store raw video, resolver CDN URLs, cookies, Gemini prompts, Gemini raw responses, or secrets in env docs or logs.
- Keep non-secret runtime config out of clean env files. Legacy Render env values can remain during transition and still override config.

## Validate

```bash
python scripts/validate_env.py development
python scripts/validate_env.py production
python scripts/validate_env.py development --example
python scripts/validate_env.py production --example
```

Make targets:

```bash
make env-check-dev
make env-check-prod
make env-check-examples
```

The validator prints key names with masked values only. Missing env files fail with a migration hint. It also validates merged config JSON safety rules.

## Export For Render

Generate a temporary Render-compatible env file:

```bash
python scripts/export_render_env.py development --output /tmp/recipe-dost-worker-development.env
python scripts/export_render_env.py production --output /tmp/recipe-dost-worker-production.env
```

Make targets:

```bash
make render-env-dev
make render-env-prod
```

The export validates first, prints key names only, and writes env-only values. `MEDIA_WORKER_SHARED_SECRET` is exported as `WORKER_API_SECRET`. Config-only values are intentionally not exported.

The output file contains secrets. Delete it after uploading to Render.

## Render Setup

Use separate Render environment groups:

- `recipe-dost-worker-dev`
- `recipe-dost-worker-prod`

Use separate Render services:

- `recipe-dost-media-worker-dev`
- `recipe-dost-media-worker-prod`

Upload flow:

1. Copy the correct template to `env/development.env` or `env/production.env`.
2. For dev, migrate only current keys from the root `.env` or old `recipe-dost-media-worker-queue-poc.env`; do not print either file.
3. Fill values from the matching Supabase/Gemini/worker environment only.
4. Leave production unfilled until prod Supabase exists.
5. Run the matching validator.
6. Export the temporary Render env file.
7. In Render, upload it to the matching environment group.
8. Attach only the dev group to the dev service and only the prod group to the prod service.
9. Restart or redeploy the worker service after uploading the env group.
10. Delete the temporary `/tmp/recipe-dost-worker-*.env` file after upload.

Do not attach both env groups to one service. Do not paste production service-role keys into the dev group or dev service-role keys into the prod group.

## Render Env Group Migration

Phase A: deploy the config-capable worker while the current Render env group still contains old non-secret runtime variables. Env vars intentionally override config, so this should not break the current deployment.

Phase B: verify queue worker behavior, `/health`, `/wake`, metadata preview, and at least one controlled queue job in the target environment.

Phase C: remove deprecated non-secret config variables from the Render env group, using [docs/m22d-worker-env-variable-map.md](m22d-worker-env-variable-map.md) as the checklist. Do not remove secrets or cookie/private auth values.

Phase D: keep only env-only values in Render: `APP_ENV`, `LOG_LEVEL`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `GEMINI_API_KEY`, `WORKER_API_SECRET`, and any required `*_COOKIES_B64` private auth values.

Do not remove non-secret Render env variables before Phase B succeeds.
