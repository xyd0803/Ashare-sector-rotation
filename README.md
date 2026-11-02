# xyd-supply-demand-sector

**A decision-discipline engine for A-share sector rotation.** It turns a supply/demand
read plus a valuation percentile into a verdict you can audit: a win rate, a quadrant,
a position size, three price scenarios — and the list of reasons it refused to be more
optimistic.

Not a prediction model. A discipline layer.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Runtime deps](https://img.shields.io/badge/runtime%20deps-1%20(requests)-brightgreen)
![Demo](https://img.shields.io/badge/demo-offline%20%C2%B7%20no%20API%20key-success)

[中文说明](README.zh-CN.md)

---

## The problem this solves

Most sector-rotation scripts answer "what should I buy". That question is easy to
answer badly, because the output hides everything that matters: which numbers were
assumed, which were fetched, what was missing, and how much the answer would move if
one of them were wrong.

This project answers a different question: **given what I actually know, what is the
defensible position size — and what would make me wrong?**

Three consequences show up everywhere in the code:

| Principle | What it looks like |
|---|---|
| Refuse to guess | A missing input produces a blocked verdict, not a default value. |
| Everything is traceable | Every verdict cites a parameter fingerprint (`params=1.11.0#d390ff51`). |
| Degradation is quantified | A degraded data state reports *how much* confidence was lost, in percentage points. |

---

## Quickstart

No API key. No network. No data directory. Requires only `requests`.

```bash
git clone https://github.com/<your-account>/xyd-supply-demand-sector.git
cd xyd-supply-demand-sector
pip install -r requirements.txt
python discipline.py --demo
```

Real output, unedited:

```text
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

Read that output again and notice what it does *not* do: it does not tell you to buy.
It reports that the risk budget is 0.227 of a 0.60 ceiling, that the tail scenario
loses 30.1%, and that the position was cut by 30% because of it. The tradeoff is
visible; the conclusion is yours.

### Verify the arithmetic yourself

Every number in the demo is hand-computable, and `--selfcheck` asserts the
intermediate steps rather than the final answer:

```bash
python discipline.py --selfcheck
```

```text
[PASS] status=OK
[PASS] 胜率=90（80+5+5+5 封顶90）
[PASS] 象限=黄金买入区
[PASS] 仓位=25.2%（36%×0.7 极端赔率比偏保守）
[PASS] 尾部标红已触发
[PASS] §十三自检全通过
```

80 points for "volume up, price up", +5 forward-looking, +5 cheap on PB, +5 catalyst,
capped at 90. Position 30% × 1.2 (forward) = 36%, then × 0.7 for the tail ratio, so
25.2%. If you disagree with the framework, you can point at the exact line you
disagree with — which is the whole point.

---

## How a verdict is produced

```text
  inputs (supply/demand read + valuation percentile + cash-flow proxy)
     │
     ├─ gate_industry_cycle ── industry-cycle volume/price basis (not company basis)
     ├─ gate_dual_source ───── two independent PB sources must agree within tolerance
     ├─ data_health ────────── four states: OK / DEGRADED / MISSING / INVALID
     │
     ├─ quant_winrate ──────── calibrated six-band table, or the quant path if validated
     ├─ valuation_anchor ───── PB / PE / blended anchor, chosen by sector type
     ├─ sector_pstar ───────── P* = anchor midpoint reverse-derived from the EV block
     ├─ EV block ───────────── bear / mid / bull / extreme price scenarios
     │
     └─ gate_final ─────────── final gate: tail ratio, position cap, execution form
                                → verdict + blockers + warnings + self-check
```

A gate that cannot evaluate its input returns `MISSING`, and `MISSING` blocks the
verdict. It never falls through to a default.

### Batch mode

```bash
python run_sector_pipeline.py --etfs 512800,512880 --out ./drafts
```

One process amortises the fixed cost across ETFs, and a single instrument failing
does not kill the batch.

---

## What makes this repo unusual

### 1. The pitfalls are a registry, not prose

`PITFALLS_REGISTRY.json` records every failure mode the framework has hit, and
`check_pitfall_closure.py` **verifies that each entry points at a test which actually
exists and actually fails when the guard is removed**. A documented pitfall with no
enforcing test is a comment, and comments rot.

```bash
python check_pitfall_closure.py
```

### 2. The tests try to break the guards

`_test_*_20261025.py` are red-team suites, not unit tests. They inject the specific
bad data each guard exists to catch — a 1% disagreement between two price sources, a
valuation series that silently changed methodology mid-window, a connector that
returns an empty payload — and assert that the guard fires *and* that the response is
proportionate. A guard that fires on everything is as useless as one that never fires,
so several cases assert that the guard stays quiet when it should.

```bash
python -m pytest -q      # 11 tests: 7 red-team suites + offline-demo smoke checks
```

### 3. Parameters are frozen and fingerprinted

`params.json` carries a version, a freeze timestamp and a hash. Verdicts quote the
fingerprint, so a result produced under different tuning is identifiable rather than
quietly incomparable.

### 4. Missing data is a first-class state

The four data states are not booleans. `DEGRADED` carries the confidence actually lost,
in percentage points, and the verdict is stamped with it. The goal is that a reader can
tell a strong conclusion from a weak one without opening the code.

---

## Honest limitations

These are stated up front because a framework that hides them is not worth reading.

- **The win-rate layer is partly heuristic.** When the calibrated model is not
  validated for a given sector, the engine falls back to a six-band lookup table and
  prints `胜率模式: 六档表(回退)（⚠️ 非量化路径·结论须带此水印）` on the verdict.
  It tells you which path produced the number instead of presenting both as equivalent.
- **This is a single-market framework.** It is built around A-share sector structure,
  PB as the dominant anchor, and a 5-year percentile window. Point it at another market
  and the anchors are wrong, not merely uncalibrated.
- **Percentiles need history.** A window shorter than five years cannot support a
  percentile conclusion, and the engine refuses to pretend otherwise rather than
  extrapolating.
- **No execution, no brokerage, no advice.** It emits text and JSON. It does not place
  orders, and it is not investment advice.
- **The repository ships no market data.** That is deliberate — see below.

---

## Data, credentials and why the repo is empty of data

Derived market data is not redistributable by anyone but the vendor that produced it,
so `data/` is git-ignored and the repository contains only code. `docs/DATA_SOURCES.md`
documents the credential model (environment variables only, never literals) and lists
open alternatives — AKShare, Tushare, Baostock, Qlib, exchange publications — for anyone
who would rather not use a commercial endpoint.

A commit hook enforces this:

```bash
python scripts/check_no_secrets.py     # credential literals, personal paths, forbidden dirs
```

The same check runs in CI, because hooks are local and can be bypassed.

---

## Repository layout

```text
discipline.py              verdict engine; --demo runs offline
run_sector_pipeline.py     batch pipeline (M1-M4)
gate_*.py                  gates: industry cycle, dual-source price, final
data_health.py             four-state data health assessment
quant_winrate.py           win-rate table / quant path
valuation_anchor.py        anchor selection by sector type
sector_pstar.py            P* derivation
params.json                frozen parameters + fingerprint
PITFALLS_REGISTRY.json     pitfall -> guarding test registry
check_pitfall_closure.py   verifies every pitfall is actually enforced
_test_*_20261025.py        red-team suites (standalone; also run under pytest)
tests/                     pytest harness + smoke checks
scripts/                   data preparation, publication guard, private backup
docs/                      data sources, credential model
```

---

## Documentation

| Document | Contents |
|---|---|
| [SKILL.md](SKILL.md) | The full framework: scoring, gates, anchor selection, EV construction |
| [PITFALLS_SDS.md](PITFALLS_SDS.md) | Every known failure mode and its guard |
| [CHANGELOG.md](CHANGELOG.md) | Version history and the reasoning behind each change |
| [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) | Credentials, open alternatives, compliance |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to run the suite, and what a change must not break |

---

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

This software is a research tool for studying decision discipline under uncertainty.
It is **not** investment advice, it does **not** constitute a recommendation to buy or
sell any security, and it comes with no warranty of any kind. Markets can and will
invalidate any model. If you use this, you are responsible for what you do with it.
