import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from PIL import Image


@unittest.skipUnless(os.name == "posix", "POSIX signal integration tests")
class PlaybackSignalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        cache = root / ".cache" / "asciime" / "test"
        cache.mkdir(parents=True)
        frames = [Image.new("RGB", (4, 4), color) for color in ("red", "blue")]
        frames[0].save(
            cache / "animation.gif", save_all=True, append_images=frames[1:],
            duration=100, loop=0,
        )
        self.env = dict(os.environ, HOME=str(root))
        self.command = [
            sys.executable, "-m", "asciime", "-c", "1", "-a", "cpu",
        ]

    def assert_terminal_restored(self, output):
        self.assertIn(b"\x1b[?25h", output)
        self.assertIn(b"\x1b[?1049l", output)
        self.assertNotIn(b"Traceback", output)
        self.assertNotIn(b"Task was destroyed", output)

    def test_finite_repeat_plays_exact_loop_count(self):
        result = subprocess.run(
            [*self.command, "-n", "3"], env=self.env, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count(b"\x1b[?2026h"), 6)
        self.assert_terminal_restored(result.stdout)

    def test_screensaver_continues_until_sigterm_or_ctrl_c(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                process = subprocess.Popen(
                    [*self.command, "-s"], env=self.env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                output = bytearray()
                try:
                    deadline = time.monotonic() + 10
                    while output.count(b"\x1b[?2026h") < 6:
                        remaining = deadline - time.monotonic()
                        self.assertGreater(remaining, 0, "Playback did not start")
                        ready, _, _ = select.select([process.stdout], [], [], remaining)
                        self.assertTrue(ready, "Playback did not start")
                        chunk = os.read(process.stdout.fileno(), 65536)
                        self.assertTrue(chunk, "Playback exited prematurely")
                        output.extend(chunk)
                    self.assertIsNone(process.poll())
                    process.send_signal(signum)
                    tail, errors = process.communicate(timeout=10)
                    output.extend(tail)
                    self.assertEqual(process.returncode, 0, errors)
                    self.assert_terminal_restored(bytes(output) + errors)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()
