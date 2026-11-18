"""Check caller types and async Protocol compatibility with Pyright."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_scoped_method_typing():
    pytest.importorskip("pyright", reason="Requires the dev type checker")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyright",
            "--pythonpath",
            sys.executable,
            "--outputjson",
            "tests/typing/scoped_methods.py",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["summary"]["filesAnalyzed"] == 1
