from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class DockerRuntimeConfigTests(unittest.TestCase):
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
