PYTHON ?= python3

.PHONY: env-check-dev env-check-prod env-check-examples render-env-dev render-env-prod

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
