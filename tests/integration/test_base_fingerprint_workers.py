import asyncio
from contextlib import closing
import fcntl
import os
import select
import sys
import unittest

from common.base_fingerprints.artifacts import MODEL_DIR, ROOT
from common.base_fingerprints.runtime import InferenceWorker
from common.base_fingerprints.types import FingerprintError
from tests._relative_geometry_fixture import layout, points


class TestFingerprintWorkers(unittest.IsolatedAsyncioTestCase):
    async def test_cpu_worker_starts_and_reaps_on_shutdown(self):
        worker = InferenceWorker("cpu", "test-lifecycle")
        try:
            await worker.start()
            process = worker.process
            self.assertIsNone(process.returncode)
            query = layout(points(53))
            partial = layout([*points(53)[:-1], ("cannon", .99, .79)])
            payload = {"query": query.model_dump(mode="json"),
                       "candidates": [query.model_dump(mode="json"), partial.model_dump(mode="json")]}
            ranked = await worker.request(payload | {"ranking": True})
            self.assertEqual([score["matched"] for score in ranked["scores"]], [53, 52])
            strict = await worker.request(payload)
            self.assertEqual(strict["scores"][0]["similarity"], 100)
            self.assertEqual(strict["scores"][1]["status"], "insufficient")
            self.assertIsNone(strict["scores"][1]["similarity"])
        finally:
            await worker.close()
        self.assertIsNotNone(process.returncode)
        self.assertIsNone(worker.process)

    async def test_main_beta_gpu_lock_contention_before_loading(self):
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        with (MODEL_DIR / "cuda.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            worker = InferenceWorker("cuda:0", "test-beta")
            with self.assertRaisesRegex(FingerprintError, "gpu_busy"):
                await worker.start()
            self.assertIsNone(worker.process)

    async def test_parent_death_terminates_worker(self):
        code = ("import asyncio; from common.base_fingerprints.runtime import InferenceWorker\n"
                "async def main():\n"
                " w=InferenceWorker('cpu','test-parent'); await w.start(); "
                "print(w.process.pid,flush=True)\n"
                " async with asyncio.timeout(30): await asyncio.Event().wait()\n"
                "asyncio.run(main())")
        parent = await asyncio.create_subprocess_exec(sys.executable, "-c", code, cwd=ROOT,
                                                      stdout=asyncio.subprocess.PIPE)
        try:
            pid = int(await asyncio.wait_for(parent.stdout.readline(), 30))
            if hasattr(os, "pidfd_open"):
                descriptor = os.pidfd_open(pid)
                try:
                    parent.terminate()
                    await asyncio.wait_for(parent.wait(), 5)
                    exited = await asyncio.wait_for(asyncio.to_thread(select.select, [descriptor], [], [], 5), 6)
                    self.assertTrue(exited[0], "Worker remained alive after its parent died")
                finally:
                    os.close(descriptor)
            else:
                with closing(select.kqueue()) as events:
                    events.control([select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                                 flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                                 fflags=select.KQ_NOTE_EXIT)], 0, 0)
                    parent.terminate()
                    await asyncio.wait_for(parent.wait(), 5)
                    exited = await asyncio.wait_for(asyncio.to_thread(events.control, [], 1, 5), 6)
                    self.assertTrue(exited, "Worker remained alive after its parent died")
        finally:
            if parent.returncode is None:
                parent.kill()
                await asyncio.wait_for(parent.wait(), 5)
