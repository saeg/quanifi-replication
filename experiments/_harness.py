"""Headless harness for experiment scripts.

Installs the same nifiapi stubs the test suite uses (tests/conftest.py), so an
experiment drives the real processors in-process: identical code paths to the
canvas, no NiFi/JVM required. Import this before importing any processor.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import conftest  # noqa: F401  (side effect: nifiapi stubs + extensions path)
from conftest import MockContext, MockFlowFile, result_to_flowfile  # noqa: F401,E402

OUT_ROOT = ROOT / "experiments" / "out"
