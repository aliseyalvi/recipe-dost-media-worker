#!/usr/bin/env bash
set -euo pipefail

echo "## Git status"
git status --short

echo ""
echo "## Recent commits"
git log --oneline -5 || true

echo ""
echo "## Important docs"
ls -la docs || true

echo ""
echo "## Python compile check"
python -m compileall app
