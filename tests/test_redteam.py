"""Run the project's red-team suites under pytest.

Each ``_test_*_20261025.py`` file is a standalone script with its own PASS/FAIL
accounting and a non-zero exit code on failure. They were never pytest tests and
renaming them to ``test_*.py`` would not have made them into any: they define no
``test_*`` functions. More importantly, their filenames are the "evidence" side
of the pitfall-closure registry -- ``check_pitfall_closure.py`` verifies that
every documented pitfall points at a test file that actually exists and passes,
so renaming would quietly break that guarantee.

Wrapping them is the honest option: pytest discovers and reports them
individually, each keeps its own output, and a failure surfaces the tail of the
child process output instead of a bare exit code.

The suites are offline by design: they inject fixed price feeds rather than
calling a vendor. One case (D-7/D-8's T10) drives a full pipeline and therefore
needs fresh local data; it is skipped unless run with ``--with-integration``.
"""
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (filename, what it guards) -- grouped for readable test ids.
SUITES = [
    ("_test_b03_trade_day_20261025.py", "trading-calendar point alignment"),
    ("_test_card_null_20261025.py", "verdict card renders null fields"),
    ("_test_closure_gate_20261025.py", "pitfall-closure checker really fires"),
    ("_test_d7_d8_20261025.py", "dual-source price cross-check + data health states"),
    ("_test_ifind_budget_gate_20261025.py", "iFinD budget gate and ledger"),
    ("_test_merge_regress_20261025.py", "valuation-gap merge stays bit-identical"),
    ("_test_s16_batch_resilience_20261025.py", "batch survives empty/failing connector"),
    ("_test_scope_gate_20261104.py", "freshness gates honour --codes instead of flooding"),
]


def _suite_id(name: str) -> str:
    """`_test_foo_20261025.py` -> `foo`. Dates vary per suite, so strip generically.

    Pinning the suffix to `_20261025.py` silently mis-ids any suite added on a
    later date (the id would keep the whole filename); a regex keeps the id
    stable as suites are added.
    """
    m = re.fullmatch(r"_test_(.+?)_\d{8}\.py", name)
    return m.group(1) if m else name.removeprefix("_test_").removesuffix(".py")


IDS = [_suite_id(name) for name, _ in SUITES]


def _child_env():
    """子进程输出强制 UTF-8。

    这些脚本会打印中文断言名。Windows 英文版 runner 的控制台默认 cp1252，
    直接 print 中文会抛 UnicodeEncodeError——与被测逻辑毫无关系的假红。
    判据是「断言对不对」，不是「终端能不能显示中文」，所以在这里一次钉住编码。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


@pytest.mark.parametrize("filename,purpose", SUITES, ids=IDS)
def test_redteam_suite(filename, purpose):
    path = os.path.join(ROOT, filename)
    assert os.path.isfile(path), "red-team script missing: %s" % filename

    proc = subprocess.run(
        [sys.executable, filename],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600, env=_child_env(),
    )

    if proc.returncode != 0:
        # Show the tail: these scripts print per-assertion lines, and the failing
        # one is almost always near the end.
        tail = "\n".join((proc.stdout or "").splitlines()[-40:])
        err = "\n".join((proc.stderr or "").splitlines()[-20:])
        pytest.fail(
            "%s (%s) exited %d\n--- stdout (tail) ---\n%s\n--- stderr (tail) ---\n%s"
            % (filename, purpose, proc.returncode, tail, err)
        )
