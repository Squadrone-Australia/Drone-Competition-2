"""Runs the node test suite for the cockpit's frontend modules.

The frontend has no npm toolchain by design, so its pure logic (the arena wall
maths, the 3D scene builder) is tested under plain node with the browser and
three.js stubbed out. See the files in tests/js/.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
JS_DIR = ROOT / "tests" / "js"


def test_frontend_js_suite():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    tests = sorted(JS_DIR.glob("*.test.js"))
    assert tests, "no node tests found in tests/js"
    proc = subprocess.run(
        [node, "--test", *[str(t) for t in tests]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
