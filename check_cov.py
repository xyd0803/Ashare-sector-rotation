import json, os
HERE = os.path.dirname(os.path.abspath(__file__))
mp = json.load(open(os.path.join(HERE, "data/meta/etf_index_map.json"), encoding="utf-8"))
IM = os.path.join(HERE, "data", "index_monthly")
RM = os.path.join(HERE, "data", "raw", "mx")
targets = sorted({v["index_code"] for v in mp.values()})
rows = []
covered = []
for code in targets:
    imp = os.path.join(IM, f"{code}.json")
    rmp = os.path.join(RM, f"{code}.json")
    cm = cr = dp = 0
    has_close = has_pb = False
    if os.path.exists(imp):
        d = json.load(open(imp, encoding="utf-8"))
        cm = len(d.get("close") or {})
        cr = len(d.get("pb") or {})
        has_close = "close" in d and bool(d.get("close"))
        has_pb = "pb" in d and bool(d.get("pb"))
    if os.path.exists(rmp):
        r = json.load(open(rmp, encoding="utf-8"))
        dp = len(r.get("pb") or [])
    ok = has_close and has_pb and cm >= 2 and cr >= 2 and dp >= 20
    rows.append((code, cm, cr, dp, ok))
    if ok:
        covered.append(code)
print(f"TOTAL target codes: {len(targets)}")
print(f"COVERED (close>=2mo & pb>=2mo & daily_pb>=20): {len(covered)}")
print(f"MISSING: {len(targets)-len(covered)}")
print("-" * 70)
print(f"{'code':12} {'close_mo':>8} {'pb_mo':>6} {'daily_pb':>8}  status")
for code, cm, cr, dp, ok in rows:
    print(f"{code:12} {cm:8} {cr:6} {dp:8}  {'OK' if ok else 'MISSING'}")
