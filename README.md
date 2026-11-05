# Ashare-sector-rotation

**A discipline layer for AI agents doing A-share sector analysis.** Ask whether a sector
or ETF is worth buying and you get an auditable verdict — win rate, quadrant, defensible
position size, three price scenarios — plus the complete list of reasons it refused to be
more optimistic.

Not a prediction model. A discipline layer.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Runtime deps](https://img.shields.io/badge/runtime%20deps-1%20(requests)-brightgreen)
![Demo](https://img.shields.io/badge/demo-offline%20%C2%B7%20no%20API%20key-success)
[![CI](https://github.com/xyd0803/Ashare-sector-rotation/actions/workflows/ci.yml/badge.svg)](https://github.com/xyd0803/Ashare-sector-rotation/actions/workflows/ci.yml)

[中文说明](README.zh-CN.md)

---

## The problem: an LLM will happily invent the number

Ask a capable model *"is 512800 worth buying now?"* and you get a confident, well-formatted
answer. What you cannot get out of that prose is:

- which numbers were **fetched** and which were **assumed**;
- what the conclusion does if one of them is off by 20%;
- how much confidence was lost because a data source came back empty;
- whether the framework behind it is the same one behind last month's call.

The usual "AI + quant" answer is to let the model do the arithmetic and then trust it.
This repository does the opposite.

## What this repository actually is

Two halves, deliberately separated and enforced by code:

| Half | Who owns it | What it is allowed to touch |
|---|---|---|
| Narrative judgement — demand regime, industry-cycle position, forward read, catalysts | **the LLM / agent** | free-text fields only |
| Every number — percentile, win rate, EV, P\*, position size, three price scenarios | **deterministic Python** | computed and *locked*; the LLM cannot write them |

The LLM proposes; the gates dispose. If the agent wants to override a locked number it must
attach `provenance`, and the override is recorded in the verdict rather than silently
replacing the computation. A missing input produces a **blocked verdict**, never a default
value.

That is why this ships as a *skill* rather than a library: `SKILL.md` is the operating manual
an agent loads (when to call it, what the input contract is, what it must not do), the Python
is the execution body, and the gates are the boundary between them.

## What you can actually ask it

Installed as a skill, these are literal prompts — the agent reads `SKILL.md`, runs the
pipeline, and returns a verdict:

> **"Is 512800 worth buying right now?"**
> → PB percentile, quadrant, win rate, position size, three price scenarios, and every
> blocker that fired.

> **"Scan these six ETFs — banks, brokers, semis — and only surface the ones worth a look."**
> → batch mode; one process amortises the fixed cost, and a single instrument failing does
> not kill the batch.

> **"Last time you said securities was a buy. Why is it neutral now?"**
> → the previous verdict cites a parameter fingerprint, so the change is attributable to
> inputs or to a framework revision — not to the model's mood.

## Quickstart

### A. 30 seconds, no API key, no network, no data directory

Only `requests` is required.

```bash
git clone https://github.com/xyd0803/Ashare-sector-rotation.git
cd Ashare-sector-rotation
pip install -r requirements.txt
python discipline.py --demo
```

### B. As a skill for your agent

Drop the repository into your agent's skills directory and point it at `SKILL.md`:

```bash
# Claude Code / compatible harness
git clone https://github.com/xyd0803/Ashare-sector-rotation.git \
  ~/.claude/skills/ashare-sector-rotation
```

Then ask in natural language. The agent reads the skill contract, decides which entry point
to call, and is bound by the same gates a human operator would be.

## Sample output

Real, unedited:

```
【证券】as_of=2026-11-02  status=OK  params=1.11.0#d390ff51
  需求: 结构性增长  量价: 量升价升 (5星)  前瞻: ↑
  [景气环比·§三bis] 买点侧别=未标注  ΔG=0.0  ✅  — 未提供景气环比数据
  估值主锚: PB
  PB: 1.36  分位 27.5% [5y]  校验: 双源一致(差0.7%)+手算校验✓
  有效分位(eff): 27.5%  赔率 极佳
  胜率: 90%  象限: 黄金买入区  建议仓位: 25.2%
  胜率模式: 六档表(回退)（⚠️ 非量化路径·结论须带此水印）
  [O10] 风险预算 conviction=0.227（=仓位×胜率/100，跨板块加总须≤组合上限 60%）
  [EV] 三档价(相对现价=100): 悲观79.41 / 中性110.29 / 乐观138.97 / 极端69.85
  [EV] 上行空间 u_mid=+10.29% u_opt=+38.97%  下行损失 d_bear=20.59% d_ext=30.15%
  [EV] EV中=+7.2%  EV乐=+33.01%  EV尾=+6.25%
  [EV·O8] 持有期=24月  年化: 中+3.54% / 乐+15.33% / 尾+3.08%（仅可比口径）
  [尾部] ⚠️ 尾部风险高（极端下行 30.1%>30%）：抄底须分批+严控单笔下注
  [尾部] 极端赔率比<1 → 尾部得不偿失，须显著偏保守
  [执行] 极端赔率比 0.34<1 → 显著偏保守，仓位 ×0.7
  [执行] 极端下行>30% → 建仓方式强制『分批+严控单笔』，禁止一次打满
  硬信号触发: 无
  §十三自检: 全通过
```

Read it again and notice what it does *not* do: it does not tell you to buy. It reports that
the risk budget is 0.227 against a 0.60 ceiling, that the tail scenario loses 30.1%, and that
the position was cut by 30% because of it. The tradeoff is visible; the decision is yours.

### Verify the arithmetic yourself

Every number in the demo is hand-computable, and `--selfcheck` asserts the intermediate steps
rather than the final answer:

```bash
python discipline.py --selfcheck
```

```
[PASS] status=OK
[PASS] 胜率=90（80+5+5+5 封顶90）
[PASS] 象限=黄金买入区
[PASS] 仓位=25.2%（36%×0.7 极端赔率比偏保守）
[PASS] 尾部标红已触发
[PASS] §十三自检全通过
```

80 points for "volume up, price up", +5 forward-looking, +5 cheap on PB, +5 catalyst, capped
at 90. Position 30% × 1.2 (forward) = 36%, then × 0.7 for the tail ratio, so 25.2%. If you
disagree with the framework you can point at the exact line you disagree with — which is the
entire point.

## How a verdict is produced

```
  inputs (supply/demand read + valuation percentile + cash-flow proxy)
     │
     ├─ gate_industry_cycle ── industry-cycle volume/price basis (NOT the ETF's own price/volume)
     ├─ gate_dual_source ───── two independent PB sources must agree within tolerance
     ├─ data_health ────────── four states: OK / DEGRADED / MISSING / INVALID
     │
     ├─ quant_winrate ──────── calibrated six-band table, or the quant path once validated
     ├─ valuation_anchor ───── PB / PE / blended anchor, chosen by sector type
     ├─ sector_pstar ───────── P* = anchor midpoint, reverse-derived from the EV block
     ├─ EV block ───────────── bear / mid / bull / extreme price scenarios
     │
     └─ gate_final ─────────── tail ratio, position cap, execution form
                                → verdict + blockers + warnings + self-check
```

A gate that cannot evaluate its input returns `MISSING`, and `MISSING` blocks the verdict.
Nothing falls through to a default.

The volume/price basis is hard-checked too: "volume" means *industry* shipment / orders /
capacity utilisation, "price" means *product* price / ASP — never the ETF's own turnover,
never the index level. An agent that tries to reason from the ETF's chart is rejected at the
input contract, before any number is computed.

## What makes this repo unusual

### 1. Every claim has to survive being wrong on purpose

`PITFALLS_REGISTRY.json` records 22 failure modes this framework has actually hit, each
pointing at a test that **exists and actually fails when its guard is removed**. A documented
pitfall with no enforcing test is a comment, and comments rot.

It also records the ideas that were **falsified** — the paths that looked reasonable and were
killed by evidence. Most projects publish only what worked; the list of what did not is the
more useful half.

```bash
python check_pitfall_closure.py
```

### 2. The tests try to break the guards

The `_test_*.py` suites are red-team suites, not unit tests. They inject the specific bad data
each guard exists to catch — a 1% disagreement between two price sources, a valuation series
that silently changed methodology mid-window, a connector that returns an empty payload, a
freshness gate that floods thirty unrelated warnings into a single-instrument check — and
assert that the guard fires *and* that the response is proportionate. Several cases assert the
guard **stays quiet** when it should: a guard that fires on everything is as useless as one
that never fires.

```bash
python -m pytest -q      # 12 checks: 8 red-team suites + 4 offline smoke checks
```

### 3. Parameters are frozen and fingerprinted

`params.json` carries a version, a freeze timestamp and a hash. Every verdict quotes the
fingerprint (`params=1.11.0#d390ff51`), so a result produced under different tuning is
identifiable rather than quietly incomparable.

### 4. Missing data is a first-class state, not a boolean

`DEGRADED` carries the confidence actually lost, **in percentage points**, and the verdict is
stamped with it. The point is that a reader can tell a strong conclusion from a weak one
without opening the code.

### 5. The agent cannot quietly change a number

Locked mechanical fields reject writes unless the override carries provenance, and the
override is printed. That is the difference between "an AI wrote this analysis" and "an AI
wrote this analysis, and here is the one place it disagreed with the model".

## Honest limitations

Stated up front, because a framework that hides them is not worth reading.

- **The win-rate layer is partly heuristic.** When the calibrated model is not validated for a
  given sector, the engine falls back to a six-band lookup table and prints
  `胜率模式: 六档表(回退)（⚠️ 非量化路径·结论须带此水印）`. It tells you which path produced
  the number instead of presenting both as equivalent.
- **Single-market.** Built around A-share sector structure, PB as the dominant anchor, and a
  5-year percentile window. Point it at another market and the anchors are wrong, not merely
  uncalibrated.
- **Percentiles need history.** A window shorter than five years cannot support a percentile
  conclusion, and the engine refuses to pretend otherwise rather than extrapolating.
- **No execution, no brokerage, no advice.** It emits text and JSON. It does not place orders.
- **The repository ships no market data.** Deliberate — see below.

## Data, credentials, and why the repo contains no data

Derived market data is not redistributable by anyone except the vendor that produced it, so
`data/` is git-ignored and the repository contains only code plus one small, explicitly
labelled sample series for the offline demo. `docs/DATA_SOURCES.md` documents the credential
model (environment variables only, never literals) and lists open alternatives — AKShare,
Tushare, Baostock, Qlib, exchange publications — for anyone who would rather not use a
commercial endpoint.

Two checks enforce this, and both run in CI because local hooks can be bypassed:

```bash
python scripts/check_no_secrets.py      # credential literals, personal paths, forbidden dirs
python scripts/check_public_ready.py    # the whole history: identities, blobs, sizes, offline demo
```

Commit identities in this repository use the GitHub `noreply` address, so history carries no
contact details.

## Repository layout

```
discipline.py              verdict engine; --demo runs offline
run_sector_pipeline.py     batch pipeline (M1-M4)
gate_*.py                  gates: industry cycle, dual-source price, final
data_health.py             four-state data health assessment
quant_winrate.py           win-rate table / quant path
valuation_anchor.py        anchor selection by sector type
sector_pstar.py            P* derivation
preflight_etf.py           pre-run health check (deps, contracts, freshness, budget)
params.json                frozen parameters + fingerprint
PITFALLS_REGISTRY.json     pitfall -> guarding test registry
check_pitfall_closure.py   verifies every pitfall is actually enforced
_test_*.py                 red-team suites (standalone; also collected by pytest)
tests/                     pytest harness + offline smoke checks
scripts/                   data preparation, publication guard, private backup
docs/                      data sources, credential model, publishing runbook
```

## Documentation

| Document | Contents |
|---|---|
| [SKILL.md](SKILL.md) | The full framework: scoring, gates, anchor selection, EV construction — this is what an agent loads |
| [PITFALLS_SDS.md](PITFALLS_SDS.md) | Every known failure mode, its guard, and the reasoning behind it |
| [CHANGELOG.md](CHANGELOG.md) | Version history and why each change was made |
| [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) | Credentials, open alternatives, compliance |
| [docs/PUBLISHING.md](docs/PUBLISHING.md) | How the public mirror is kept free of data and credentials |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to run the suite, and what a change must not break |

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

This software is a research tool for studying decision discipline under uncertainty. It is
**not** investment advice, it does **not** constitute a recommendation to buy or sell any
security, and it comes with no warranty of any kind. Markets can and will invalidate any
model. If you use this, you are responsible for what you do with it.
