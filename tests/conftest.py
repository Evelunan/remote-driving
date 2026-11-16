"""Isolate legacy applications until they have distinct package namespaces."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def legacy_python(tmp_path):
    def run(application, source):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / application)
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(source)],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    return run
