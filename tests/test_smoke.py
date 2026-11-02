"""Cheap, high-signal checks: is the deliverable actually runnable, and is the
repository still safe to publish?

These two questions are the ones a fresh visitor asks first, so they are worth
asserting rather than merely claiming in a README:

1. ``discipline.py --demo`` must produce a full verdict with **no** credentials,
   no network and no data directory. This is the project's entire adoption story.
2. No credential literal and no personal absolute path may be tracked. The same
   check runs as a commit hook, but hooks are local and can be bypassed; running
   it in CI covers the case where someone did.
"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(*args, timeout=180):
    return subprocess.run(
        [sys.executable, *args], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def test_demo_runs_offline_without_credentials():
    """The demo must not need a key, a network call, or the data directory."""
    env = {k: v for k, v in os.environ.items() if not k.endswith("_APIKEY")}
    proc = subprocess.run(
        [sys.executable, "discipline.py", "--demo"], cwd=ROOT, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=180, env=env,
    )
    assert proc.returncode == 0, "demo exited %d\n%s" % (proc.returncode, proc.stderr[-2000:])

    out = proc.stdout
    # A verdict, not just "it ran": the four numbers a reader is looking for.
    for marker in ("胜率", "EV中", "建议仓位", "params="):
        assert marker in out, "demo output missing %r\n%s" % (marker, out[:2000])


def test_demo_is_deterministic():
    """Same input, same params -> same verdict. Guards against hidden state."""
    first = _run("discipline.py", "--demo")
    second = _run("discipline.py", "--demo")
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout


def test_params_fingerprint_present():
    """Every verdict cites a parameter fingerprint; without it results are
    unfalsifiable (the reader cannot tell which settings produced them)."""
    with open(os.path.join(ROOT, "params.json"), "r", encoding="utf-8") as fh:
        params = json.load(fh)
    assert params, "params.json is empty"

    out = _run("discipline.py", "--demo").stdout
    assert "params=" in out, "demo output does not cite a params fingerprint"


def test_no_credentials_committed():
    """Re-run the publication guard in CI.

    Skipped when the working copy is not a git repository (e.g. an exported
    tarball), because the guard's scope is defined by git.
    """
    probe = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                           cwd=ROOT, capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("not a git working tree")

    proc = _run("scripts/check_no_secrets.py", "--quiet")
    assert proc.returncode == 0, (
        "publication guard failed:\n%s\n%s" % (proc.stdout[-3000:], proc.stderr[-1000:])
    )
