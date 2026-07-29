from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.workers import combined_worker


REPO_ROOT = Path(__file__).resolve().parents[1]


class DockerRuntimeConfigTests(unittest.TestCase):
    def test_combined_worker_remains_app_main_uvicorn_facade(self) -> None:
        with patch.dict("os.environ", {"PORT": "10000"}, clear=True), patch.object(
            combined_worker.uvicorn, "run"
        ) as run:
            combined_worker.main()
        run.assert_called_once_with(
            "app.main:app",
            host="0.0.0.0",
            port=10000,
            timeout_keep_alive=120,
        )

    def test_image_uses_python_312_and_same_requirements_file(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM python:3.12-slim", dockerfile)
        self.assertIn("COPY requirements.txt .", dockerfile)
        self.assertIn("pip install --no-cache-dir --upgrade -r requirements.txt", dockerfile)

    def test_image_contains_runtime_config_and_diagnostic(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY app ./app", dockerfile)
        self.assertIn("COPY config ./config", dockerfile)
        self.assertIn("COPY scripts ./scripts", dockerfile)
        self.assertTrue(REPO_ROOT.joinpath("config/worker.defaults.json").exists())
        self.assertTrue(REPO_ROOT.joinpath("config/worker.development.json").exists())
        self.assertTrue(REPO_ROOT.joinpath("config/worker.production.json").exists())
        self.assertTrue(
            REPO_ROOT.joinpath("scripts/check_external_resolver_config.py").exists()
        )


if __name__ == "__main__":
    unittest.main()
