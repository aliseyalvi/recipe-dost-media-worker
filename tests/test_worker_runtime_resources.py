from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import unittest
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable

from app.pipeline import resources as legacy_resources
from app.worker_runtime import resources

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SlotFactory = Callable[[], AbstractContextManager[None]]
ActiveReader = Callable[[], int]


class ResourceFailure(RuntimeError):
    pass


class WorkerRuntimeResourceTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        resources._configure_ffmpeg_slots(1)
        resources._configure_gemini_upload_slots(1)

    def test_neutral_module_exposes_active_symbols_with_original_defaults(
        self,
    ) -> None:
        self.assertTrue(callable(resources.ffmpeg_process_slot))
        self.assertTrue(callable(resources.gemini_upload_slot))
        self.assertIsInstance(resources._ffmpeg_slots, threading.BoundedSemaphore)
        self.assertIsInstance(
            resources._gemini_upload_slots,
            threading.BoundedSemaphore,
        )
        self.assertEqual(resources._ffmpeg_slots._initial_value, 1)
        self.assertEqual(resources._gemini_upload_slots._initial_value, 1)

    def test_legacy_active_symbols_are_identical_reexports(self) -> None:
        self.assertIs(
            legacy_resources.ffmpeg_process_slot,
            resources.ffmpeg_process_slot,
        )
        self.assertIs(
            legacy_resources.gemini_upload_slot,
            resources.gemini_upload_slot,
        )

    async def test_ffmpeg_operations_are_serialized_and_release_after_success(
        self,
    ) -> None:
        await self._assert_serialized(
            resources.ffmpeg_process_slot,
            resources._active_ffmpeg_processes,
        )
        self._assert_released_after_success(
            resources.ffmpeg_process_slot,
            resources._active_ffmpeg_processes,
        )

    async def test_ffmpeg_releases_after_exception(self) -> None:
        self._assert_released_after_exception(
            resources.ffmpeg_process_slot,
            resources._active_ffmpeg_processes,
        )

    async def test_ffmpeg_releases_after_task_cancellation(self) -> None:
        await self._assert_released_after_cancellation(
            resources.ffmpeg_process_slot,
            resources._active_ffmpeg_processes,
        )

    async def test_gemini_uploads_are_serialized_and_release_after_success(
        self,
    ) -> None:
        await self._assert_serialized(
            resources.gemini_upload_slot,
            resources._active_gemini_uploads,
        )
        self._assert_released_after_success(
            resources.gemini_upload_slot,
            resources._active_gemini_uploads,
        )

    async def test_gemini_upload_releases_after_exception(self) -> None:
        self._assert_released_after_exception(
            resources.gemini_upload_slot,
            resources._active_gemini_uploads,
        )

    async def test_gemini_upload_releases_after_task_cancellation(self) -> None:
        await self._assert_released_after_cancellation(
            resources.gemini_upload_slot,
            resources._active_gemini_uploads,
        )

    def test_active_importers_use_only_neutral_resource_path(self) -> None:
        importers = {
            "app/queue_worker.py": "gemini_upload_slot",
            "app/media_resolvers/media_processing.py": "ffmpeg_process_slot",
            "app/social/stream_download.py": "ffmpeg_process_slot",
            "app/social/thumbnail_upload.py": "ffmpeg_process_slot",
        }
        for relative_path, symbol in importers.items():
            with self.subTest(importer=relative_path):
                source = REPOSITORY_ROOT.joinpath(relative_path).read_text(
                    encoding="utf-8"
                )
                self.assertIn(
                    f"from app.worker_runtime.resources import {symbol}",
                    source,
                )
                self.assertNotIn("app.pipeline.resources", source)

    def test_fresh_active_imports_do_not_load_w7_modules_through_resources(
        self,
    ) -> None:
        code = (
            "import importlib,sys;"
            "modules=('app.queue_worker',"
            "'app.media_resolvers.media_processing',"
            "'app.social.stream_download','app.social.thumbnail_upload');"
            "[importlib.import_module(name) for name in modules];"
            "forbidden={'app.pipeline.resources','app.pipeline.runtime',"
            "'app.pipeline.transport','app.pipeline.services',"
            "'app.pipeline.contracts','app.pipeline.dispatch',"
            "'app.pipeline.ingest','app.pipeline.analyzer',"
            "'app.pipeline.finalizer','app.qa.staged',"
            "'app.workers.ingest_worker','app.workers.analyzer_worker',"
            "'app.workers.finalizer_worker'};"
            "assert forbidden.isdisjoint(sys.modules)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPOSITORY_ROOT,
            env={"APP_ENV": "production", "PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_neutral_resource_import_has_no_external_side_effect_modules(
        self,
    ) -> None:
        code = (
            "import sys;import app.worker_runtime.resources;"
            "forbidden={'app.pipeline.resources','app.pipeline.runtime',"
            "'app.queue_worker','app.supabase_client','app.ai.gemini_recipe',"
            "'app.media_resolvers.registry',"
            "'app.media_resolvers.instagram_unified_v1',"
            "'app.media_resolvers.instagram_reels13',"
            "'app.media_resolvers.tiktok_akng'};"
            "assert forbidden.isdisjoint(sys.modules)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPOSITORY_ROOT,
            env={"APP_ENV": "production", "PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    async def _assert_serialized(
        self,
        slot: SlotFactory,
        active_reader: ActiveReader,
    ) -> None:
        first_entered = threading.Event()
        release_first = threading.Event()
        second_attempted = threading.Event()
        state_lock = threading.Lock()
        order: list[str] = []
        active = 0
        maximum_active = 0

        def operation(
            name: str,
            entered: threading.Event,
            release: threading.Event | None,
        ) -> None:
            nonlocal active, maximum_active
            if name == "second":
                second_attempted.set()
            with slot():
                with state_lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                    order.append(f"{name}_entered")
                entered.set()
                if release is not None and not release.wait(timeout=5):
                    raise AssertionError("resource test release was not signaled")
                with state_lock:
                    active -= 1
                    order.append(f"{name}_exited")

        second_entered = threading.Event()
        first = asyncio.create_task(
            asyncio.to_thread(
                operation,
                "first",
                first_entered,
                release_first,
            )
        )
        self.assertTrue(await asyncio.to_thread(first_entered.wait, 5))
        self.assertEqual(active_reader(), 1)

        second = asyncio.create_task(
            asyncio.to_thread(
                operation,
                "second",
                second_entered,
                None,
            )
        )
        self.assertTrue(await asyncio.to_thread(second_attempted.wait, 5))
        self.assertFalse(second_entered.is_set())

        release_first.set()
        await asyncio.gather(first, second)

        self.assertEqual(maximum_active, 1)
        self.assertEqual(
            order,
            [
                "first_entered",
                "first_exited",
                "second_entered",
                "second_exited",
            ],
        )
        self.assertEqual(active_reader(), 0)

    def _assert_released_after_success(
        self,
        slot: SlotFactory,
        active_reader: ActiveReader,
    ) -> None:
        with slot():
            self.assertEqual(active_reader(), 1)
        self.assertEqual(active_reader(), 0)
        with slot():
            self.assertEqual(active_reader(), 1)
        self.assertEqual(active_reader(), 0)

    def _assert_released_after_exception(
        self,
        slot: SlotFactory,
        active_reader: ActiveReader,
    ) -> None:
        with self.assertRaises(ResourceFailure):
            with slot():
                self.assertEqual(active_reader(), 1)
                raise ResourceFailure("resource release test")
        self.assertEqual(active_reader(), 0)
        with slot():
            self.assertEqual(active_reader(), 1)
        self.assertEqual(active_reader(), 0)

    async def _assert_released_after_cancellation(
        self,
        slot: SlotFactory,
        active_reader: ActiveReader,
    ) -> None:
        entered = asyncio.Event()
        blocked = asyncio.Event()

        async def operation() -> None:
            with slot():
                entered.set()
                await blocked.wait()

        task = asyncio.create_task(operation())
        await entered.wait()
        self.assertEqual(active_reader(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(active_reader(), 0)
        with slot():
            self.assertEqual(active_reader(), 1)
        self.assertEqual(active_reader(), 0)


if __name__ == "__main__":
    unittest.main()
