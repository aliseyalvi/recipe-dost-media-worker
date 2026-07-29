from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from app import job_runner, queue_worker
from app.worker_runtime import job_helpers


class WorkerRuntimeJobHelperTests(unittest.TestCase):
    def test_queue_and_legacy_imports_share_neutral_helper_identities(self) -> None:
        symbols = (
            "PipelineFailure",
            "_caption_result_to_recipe_result",
            "_extract_json_object",
            "_validate_parsed_recipe",
            "platform_for_job",
            "save_mock_recipe_card_for_job",
        )
        for symbol in symbols:
            with self.subTest(symbol=symbol):
                active = getattr(job_helpers, symbol)
                self.assertIs(getattr(queue_worker, symbol), active)
                self.assertIs(getattr(job_runner, symbol), active)

    def test_fresh_queue_import_does_not_load_legacy_job_runner(self) -> None:
        code = (
            "import sys;import app.queue_worker;"
            "assert 'app.job_runner' not in sys.modules"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            env={"APP_ENV": "production", "PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_neutral_helpers_do_not_reverse_import_legacy_runner(self) -> None:
        code = (
            "import sys;import app.worker_runtime.job_helpers;"
            "assert 'app.job_runner' not in sys.modules"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            env={"APP_ENV": "production", "PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
