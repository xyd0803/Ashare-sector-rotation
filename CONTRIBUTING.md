# Contributing

Thanks for looking. This project has an unusual constraint that is worth stating
before anything else: **the framework's value comes from being hard to change
quietly.** Most of the rules below exist to keep a change visible.

## Before you start

```bash
pip install -r requirements-dev.txt
git config core.hooksPath .githooks   # once: commit + push guards
python discipline.py --demo        # must run offline, no key
python -m pytest -q                # must be green before and after your change
python scripts/check_public_ready.py --quiet   # full history, not just your diff
```

If `pytest` is green before your change and red after, the change is the problem —
not the test. Several of these tests encode a bug that already happened once.

The last command checks the **entire history**, which is not redundant with the
commit hook: a key added in one commit and removed in the next passes every
working-tree check while remaining readable via `git log -p`. See
[`docs/PUBLISHING.md`](docs/PUBLISHING.md) for what the gate refuses and why.

## What a change must not break

| Invariant | Why it exists | If you touch it |
|---|---|---|
| `params.json` fingerprint | Results from different tunings must be distinguishable | Bump `version`; never edit values in place silently |
| Pitfall registry closure | A documented pitfall with no enforcing test is a comment | Add the test *and* the registry entry in the same commit |
| Offline `--demo` | The adoption story: 30 seconds, no credentials | It may not gain a network or data dependency |
| Fail-closed gates | `MISSING` must block, not default | Any new gate returns a state, never a bare value |

## Common changes

### Adding a gate

1. Put the logic in its own `gate_*.py`, returning a state (`OK` / `DEGRADED` /
   `MISSING` / `INVALID`) — not a boolean. Callers need to distinguish "passed"
   from "could not evaluate".
2. Quantify degradation in percentage points. `DEGRADED` without a number is
   indistinguishable from a guess.
3. Add a red-team case that injects the input the gate exists to reject, **and one
   that asserts the gate stays quiet on a healthy input**. Over-firing gates get
   disabled by users, which is worse than no gate.
4. Register the failure mode in `PITFALLS_REGISTRY.json` and confirm
   `python check_pitfall_closure.py` still passes.

### Changing a scoring rule

Change the rule, the `params.json` entry, and the `--selfcheck` expectations in
the same commit. `--selfcheck` asserts intermediate arithmetic, so it fails loudly
when a rule moves — that is the intended alarm, not an obstacle.

### Adding a data source

Read [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) first. The short version: the
key comes from an environment variable, never a literal, and the source's terms
must permit your use. `python scripts/check_no_secrets.py` will block you otherwise,
and that is by design.

## Style

- Python 3.10+. `from __future__ import annotations` is not required, but `X | None`
  annotations are fine.
- Comments explain *why*, not *what*. A comment restating the code is noise; a
  comment recording the incident that motivated the code is the most valuable thing
  in the file.
- Do not reformat the legacy engine to satisfy a linter. A repo-wide whitespace diff
  buries every real change and makes review impossible. Lint scope is `scripts/` and
  `tests/`.

## Commits

Conventional-commit style, one idea per commit:

```text
feat(gate): reject dual-source price disagreement above 0.5%
fix(health): DEGRADED must carry a quantified impact
docs(readme): state the heuristic fallback instead of hiding it
```

If a commit needs a paragraph to justify itself, that paragraph belongs in the
commit body — it is the only place a future reader will look when wondering why
the code is shaped that way.

## Tests

The red-team suites are standalone scripts (`_test_*_20261025.py`) that pytest
wraps. They are not pytest modules and do not define `test_*` functions; their
filenames are referenced from `PITFALLS_REGISTRY.json`, so **do not rename them**
without updating the registry, or the closure checker will quietly pass on a
missing file.

New pytest-style tests belong in `tests/`.

## Reporting a bug

Please include the verdict output (or `--json`), the `params=` fingerprint, and
the data state. Without the fingerprint the result cannot be reproduced, which
usually means it cannot be diagnosed either.
