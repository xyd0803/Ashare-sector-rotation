"""Shared pytest setup.

Two things are needed for the suites to run from a bare clone:

1. The repository root on ``sys.path``. The red-team scripts live at the root
   (they are referenced *by filename* from ``PITFALLS_REGISTRY.json``, so moving
   them would silently break the pitfall-closure checker -- a good reason to
   leave them where they are and adapt the test harness instead).
2. A stable working directory. Several scripts shell out to sibling modules, so
   they must be executed with the repo root as cwd.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
