"""What the runner's tests share: unio-inbox loaded as a module, and a
throwaway home for each test.

Stdlib only, like the runner, so the tests run on any Python 3.9 or later
on Linux, macOS and Windows: python -m unittest discover -s runner/tests
"""
import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# unio-inbox has no .py suffix, so a cache file would land as runner/__pycache__/unio-inbox.*.
sys.dont_write_bytecode = True

RUNNER_DIR = Path(__file__).resolve().parent.parent
RUNNER = RUNNER_DIR / "unio-inbox"


def load_runner():
    loader = importlib.machinery.SourceFileLoader("unio_inbox", str(RUNNER))
    spec = importlib.util.spec_from_loader("unio_inbox", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


inbox = load_runner()


class HomeCase(unittest.TestCase):
    """A test with a home of its own: HOME, USERPROFILE and the XDG and
    Windows folders point inside it, and CLAUDE_CONFIG_DIR too, so nothing
    reads or writes the real ones."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="unio-inbox-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "APPDATA": str(self.home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(self.home / "AppData" / "Local"),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "UNIO_RUNNER_AUTOUPDATE": "off",
        }
        cleared = {k: "" for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
        patcher = mock.patch.dict(os.environ, {**cleared, **self.env})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in cleared:
            os.environ.pop(key, None)
