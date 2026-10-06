"""The optional command that runs after each finished run (for example the Vercel publisher)."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from itleads import afterrun, config  # noqa: E402


class AfterRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._saved = (config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH)
        config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH = root, root / "data", root / "logs", root / "config.json"
        config.ensure_dirs()
        self.root = root
        afterrun._running, afterrun._again = False, False

    def tearDown(self):
        self.wait()
        config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH = self._saved
        self._tmp.cleanup()

    def wait(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end and any(t.name == "after-run" and t.is_alive() for t in threading.enumerate()):
            time.sleep(0.02)

    def set_command(self, *parts):
        import shlex
        config.update(lambda c: c.__setitem__("after_run", " ".join(shlex.quote(p) for p in parts)))

    def test_nothing_runs_by_default(self):
        self.assertEqual(config.load()["after_run"], "")
        with mock.patch.object(afterrun.subprocess, "run") as run:
            afterrun.listener("schedule", "ok", "", {})
            self.wait()
        run.assert_not_called()

    def test_a_run_that_refreshed_the_list_starts_the_command_and_a_failed_one_does_not(self):
        marker = self.root / "ran"
        self.set_command(sys.executable, "-c", f"open({str(marker)!r}, 'a').write('x')")
        for status in ("failed", "offline"):
            afterrun.listener("schedule", status, "", {})
            self.wait()
        self.assertFalse(marker.exists())
        for status in ("ok", "partial"):
            afterrun.listener("manual", status, "", {})
            self.wait()
        self.assertEqual(marker.read_text(), "xx")

    def test_a_command_that_fails_or_is_missing_is_logged_and_never_raises(self):
        self.set_command(sys.executable, "-c", "import sys; print('boom'); sys.exit(3)")
        afterrun.listener("schedule", "ok", "", {})
        self.wait()
        log = (config.LOGS / "after-run.log").read_text()
        self.assertIn("exit 3", log)
        self.assertIn("boom", log)
        self.set_command("/no/such/program-xyz")
        afterrun.listener("schedule", "ok", "", {})
        self.wait()
        self.assertIn("could not start", (config.LOGS / "after-run.log").read_text())

    def test_only_one_command_runs_at_a_time_and_a_burst_is_folded_into_one_more_pass(self):
        active, peak, calls = [0], [0], [0]
        lock = threading.Lock()

        def slow(cmd, **kw):
            with lock:
                active[0] += 1
                calls[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.15)
            with lock:
                active[0] -= 1
            return mock.Mock(returncode=0, stdout="", stderr="")

        self.set_command("deploy.sh")
        with mock.patch.object(afterrun.subprocess, "run", side_effect=slow):
            for _ in range(5):
                afterrun.listener("schedule", "ok", "", {})
            self.wait()
        self.assertEqual(peak[0], 1)
        self.assertEqual(calls[0], 2)                                   # the first, and one more for everything that came meanwhile

    def test_the_command_gets_a_path_that_finds_node_tools_in_a_background_service(self):
        env = afterrun.environment()
        self.assertIn(str(Path.home() / ".npm-global" / "bin"), env["PATH"].split(":"))
        self.assertEqual(afterrun.command({"after_run": "a b 'c d'"}), ["a", "b", "c d"])
        self.assertEqual(afterrun.command({"after_run": ["x", 1]}), ["x", "1"])


if __name__ == "__main__":
    unittest.main()
