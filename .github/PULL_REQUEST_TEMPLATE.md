## What this changes

<!-- One paragraph. If it needs three, it is probably two pull requests. -->

## Why

<!-- The failure mode or gap this addresses. "Improves accuracy" is not a reason
     anyone can evaluate; a described wrong answer is. -->

## Checklist

- [ ] `python discipline.py --demo` still runs offline (no key, no network, no `data/`)
- [ ] `python -m pytest -q` is green
- [ ] `python scripts/check_no_secrets.py` passes
- [ ] If a scoring rule changed: `params.json` version bumped **and** `--selfcheck`
      expectations updated in this same commit
- [ ] If a new failure mode is handled: `PITFALLS_REGISTRY.json` updated and
      `check_pitfall_closure.py` still passes
- [ ] If a gate was added or changed: a red-team case injects the bad input **and**
      one asserts the gate stays quiet on healthy input
- [ ] No credentials, account details, or personal absolute paths anywhere in the diff

## Effect on verdicts

<!-- Does any existing verdict change? If yes, say which and by how much.
     "No behavioural change" is a valid and useful answer. -->

## Notes for the reviewer

<!-- Anything you are unsure about. Pointing at your own weak spot is more
     convincing than hoping nobody notices it. -->
