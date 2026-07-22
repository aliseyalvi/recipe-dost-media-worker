PYTHON ?= python3

.PHONY: env-check-dev env-check-prod env-check-examples render-env-dev render-env-prod env-doctor-dev env-doctor-prod resolver-config-dev resolver-config-prod resolver-config-docker tiktok-live-diagnostic check-dev check-prod

env-check-dev:
	$(PYTHON) scripts/validate_env.py development

env-check-prod:
	$(PYTHON) scripts/validate_env.py production

env-check-examples:
	$(PYTHON) scripts/validate_env.py development --example
	$(PYTHON) scripts/validate_env.py production --example

render-env-dev:
	$(PYTHON) scripts/export_render_env.py development --output /tmp/recipe-dost-worker-development.env

render-env-prod:
	$(PYTHON) scripts/export_render_env.py production --output /tmp/recipe-dost-worker-production.env

env-doctor-dev:
	$(PYTHON) scripts/check_environment.py development

env-doctor-prod:
	$(PYTHON) scripts/check_environment.py production

resolver-config-dev:
	INSTAGRAM_RAPIDAPI_KEY=dummy-development-key TIKTOK_AKNG_ACCESS_TOKEN=dummy-development-token $(PYTHON) scripts/check_external_resolver_config.py development

resolver-config-prod:
	$(PYTHON) scripts/check_external_resolver_config.py production

resolver-config-docker:
	docker build -t recipe-dost-worker-m23w3c-r .
	docker run --rm --entrypoint python -e RENDER=true -e RENDER_GIT_BRANCH=develop -e INSTAGRAM_RAPIDAPI_KEY=dummy-development-key -e TIKTOK_AKNG_ACCESS_TOKEN=dummy-development-token recipe-dost-worker-m23w3c-r scripts/check_external_resolver_config.py development
	docker run --rm --entrypoint python -e RENDER=true -e RENDER_GIT_BRANCH=main recipe-dost-worker-m23w3c-r scripts/check_external_resolver_config.py production

tiktok-live-diagnostic:
	APP_ENV=development ALLOW_LIVE_TIKTOK_DIAGNOSTIC=true $(PYTHON) scripts/check_tiktok_akng_end_to_end.py --input-kind "$(INPUT_KIND)"

check-dev:
	$(PYTHON) -m compileall app scripts
	$(PYTHON) scripts/check_runtime_config.py development
	INSTAGRAM_RAPIDAPI_KEY=dummy-development-key TIKTOK_AKNG_ACCESS_TOKEN=dummy-development-token $(PYTHON) scripts/check_external_resolver_config.py development
	$(PYTHON) scripts/check_environment.py development

check-prod:
	$(PYTHON) -m compileall app scripts
	$(PYTHON) scripts/check_runtime_config.py production
	$(PYTHON) scripts/check_external_resolver_config.py production
	$(PYTHON) scripts/check_environment.py production
