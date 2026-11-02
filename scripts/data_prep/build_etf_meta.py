#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the ETF -> tracking-index metadata map.

The mapping itself (53 ETFs -> index name / index code / listing date) is the
valuable asset here: index codes are ambiguous across vendors, and vendor
prefixes differ (`.SZ` / `.CSI` / `.SH` / `.GI` / `.HI`). Keeping a curated,
human-verified table avoids re-deriving it every time a data source changes.

Input : a directory of per-ETF monthly K-line JSON files, each shaped as
        ``{"code": "515880", "monthly": [{"date": "2024-01", "close": 1.23}, ...]}``
        This is deliberately vendor-neutral -- produce it with whatever source
        you have credentials for (see docs/DATA_SOURCES.md).
Output: ``etf_index_map.json`` next to this script (or --out).

Usage
-----
    python build_etf_meta.py --kline-dir /path/to/etf_monthly --out etf_index_map.json

The kline directory may also be supplied via the ``SDS_KLINE_DIR`` env var.
If it is omitted or missing, the script still emits the mapping with
``kline_months = null`` so the table stays usable as a reference document.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "etf_index_map.json")

# code, name, index_name, index_code, list_date
ETF_LIST = [
    ["513180", "华夏恒生科技ETF", "恒生科技指数", "HSTECH.HI", "2021-05-25"],
    ["588170", "华夏上证科创板半导体材料设备ETF", "科创半导体材料设备", "950125.SH", "2025-04-08"],
    ["159781", "易方达中证科创创业50ETF", "科创创业50", "931643.CSI", "2021-07-05"],
    ["512800", "华宝中证银行ETF", "中证银行", "399986.SZ", "2017-08-03"],
    ["515220", "国泰中证煤炭ETF", "中证煤炭", "399998.SZ", "2020-03-02"],
    ["513520", "华夏野村日经225ETF", "日经225", "N225.GI", "2019-06-25"],
    ["159509", "景顺长城纳斯达克科技ETF", "纳斯达克科技市值加权", "NDXTMC.GI", "2023-08-08"],
    ["513650", "南方标普500ETF", "标普500", "SPX.GI", "2023-04-04"],
    ["516510", "易方达中证云计算与大数据主题ETF", "云计算", "930851.CSI", "2021-04-07"],
    ["159928", "汇添富中证主要消费ETF", "800消费", "000932.SH", "2013-09-16"],
    ["159869", "华夏中证动漫游戏ETF", "动漫游戏", "930901.CSI", "2021-03-05"],
    ["563360", "华泰柏瑞中证A500ETF", "中证A500", "000510.CSI", "2024-10-15"],
    ["159516", "国泰中证半导体材料设备主题ETF", "半导体材料设备", "931743.CSI", "2023-07-27"],
    ["512070", "易方达沪深300非银ETF", "300非银", "H30035.CSI", "2014-07-18"],
    ["512400", "南方中证申万有色金属ETF", "有色金属", "000819.SH", "2017-09-01"],
    ["515030", "华夏中证新能源汽车ETF", "CS新能车", "399976.SZ", "2020-03-04"],
    ["513950", "富国恒生港股通高股息低波动ETF", "恒生港股通高股息低波动指数", "HSHYLV.HI", "2023-04-28"],
    ["516150", "嘉实中证稀土产业ETF", "稀土产业", "930598.CSI", "2021-03-17"],
    ["560080", "汇添富中证中药ETF", "中证中药", "930641.CSI", "2022-10-17"],
    ["510880", "华泰柏瑞上证红利ETF", "红利指数", "000015.SH", "2007-01-18"],
    ["159570", "汇添富国证港股通创新药ETF", "港股通创新药", "987018.SZ", "2024-01-22"],
    ["512690", "鹏华中证酒ETF", "中证酒", "399987.SZ", "2019-05-06"],
    ["515790", "华泰柏瑞中证光伏产业ETF", "光伏产业", "931151.CSI", "2020-12-18"],
    ["159796", "汇添富中证电池主题ETF", "CS电池", "931719.CSI", "2022-03-14"],
    ["159206", "永赢国证商用卫星ETF", "卫星通信", "980018.SZ", "2025-03-14"],
    ["512480", "国联安半导体ETF", "半导体", "H30184.CSI", "2019-06-12"],
    ["588710", "华泰柏瑞上证科创板半导体材料设备主题ETF", "科创半导体材料设备", "950125.SH", "2025-06-04"],
    ["512170", "华宝中证医疗ETF", "中证医疗", "399989.SZ", "2019-06-17"],
    ["515880", "国泰中证全指通信设备ETF", "通信设备", "931160.CSI", "2019-09-06"],
    ["159667", "国泰中证机床ETF", "中证机床", "931866.CSI", "2022-10-26"],
    ["560280", "广发中证工程机械主题ETF", "工程机械主题", "931752.CSI", "2023-10-30"],
    ["588200", "嘉实上证科创板芯片ETF", "科创芯片", "000685.SH", "2022-10-26"],
    ["588080", "易方达上证科创板50ETF", "科创50", "000688.SH", "2020-11-16"],
    ["513090", "易方达中证香港证券投资主题ETF", "香港证券港元", "930709.CSI", "2020-03-26"],
    ["159566", "易方达国证新能源电池ETF", "新能源电池", "980027.SZ", "2024-02-08"],
    ["159870", "鹏华中证细分化工产业ETF", "细分化工", "000813.CSI", "2021-03-03"],
    ["159995", "华夏国证半导体芯片ETF", "国证芯片", "980017.SZ", "2020-02-10"],
    ["159608", "广发中证稀有金属主题ETF", "CS稀金属", "930632.CSI", "2020-02-10"],
    ["159865", "国泰中证畜牧养殖ETF", "中证畜牧", "930707.CSI", "2021-12-23"],
    ["159819", "易方达中证人工智能主题ETF", "CS人工智", "930713.CSI", "2020-09-23"],
    ["512880", "国泰中证全指证券公司ETF", "证券公司", "399975.SZ", "2016-08-08"],
    ["159201", "华夏中证自由现金流ETF", "自由现金流", "980092.SZ", "2025-02-27"],
    ["159326", "华夏中证电网设备主题ETF", "电网设备主题", "931994.CSI", "2024-09-09"],
    ["512710", "富国中证军工龙头ETF", "军工龙头", "931066.CSI", "2019-08-26"],
    ["515230", "国泰中证全指软件ETF", "软件指数", "H30202.CSI", "2021-03-02"],
    ["159792", "富国中证港股通互联网ETF", "港股通互联网", "931637.CSI", "2021-09-28"],
    ["159992", "银华中证创新药产业ETF", "CS创新药", "930722.CSI", "2020-04-10"],
    ["159530", "易方达国证机器人产业ETF", "机器人产业", "931238.CSI", "2024-01-18"],
    ["517520", "永赢中证沪深港黄金产业股票ETF", "SSH黄金股票", "931790.CSI", "2022-12-22"],
    ["513310", "华泰柏瑞中证韩交所中韩半导体ETF", "中韩半导体", "H30199.CSI", "2023-11-01"],
    ["159611", "广发中证全指电力公用事业ETF", "电力指数", "H30269.CSI", "2022-01-07"],
    ["512890", "华泰柏瑞中证红利低波动ETF", "红利低波", "930915.CSI", "2019-01-18"],
    ["513530", "华泰柏瑞中证港股通高股息投资ETF", "港股通高股息人民币", "H30269.CSI", "2022-04-25"],
]


def market_of(code):
    """Exchange suffix from the numeric ETF code."""
    return "sh" if code.startswith(("5", "6", "9")) else "sz"


def load_month_counts(kline_dir):
    """Return {code: number_of_months}, or {} when the dir is absent."""
    if not kline_dir:
        return {}
    if not os.path.isdir(kline_dir):
        sys.stderr.write("[warn] kline dir not found: %s\n" % kline_dir)
        return {}
    counts = {}
    for fn in os.listdir(kline_dir):
        if not fn.lower().endswith(".json"):
            continue
        code = re.sub(r"\D", "", os.path.splitext(fn)[0])
        if not code:
            continue
        try:
            with open(os.path.join(kline_dir, fn), "r", encoding="utf-8") as f:
                obj = json.load(f)
        except Exception as exc:
            sys.stderr.write("[warn] skip %s: %s\n" % (fn, exc))
            continue
        rows = obj.get("monthly") if isinstance(obj, dict) else obj
        if isinstance(rows, list):
            counts[code] = len(rows)
    return counts


def build(kline_dir=None):
    counts = load_month_counts(kline_dir)
    mp = {}
    for code, name, idx_name, idx_code, listed in ETF_LIST:
        mp[code] = {
            "name": name,
            "index_name": idx_name,
            "index_code": idx_code,
            "list_date": listed,
            "market": market_of(code),
            "kline_months": counts.get(code),
        }
    return mp, counts


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build ETF -> index metadata map.")
    ap.add_argument("--kline-dir", default=os.getenv("SDS_KLINE_DIR", ""),
                    help="dir of per-ETF monthly kline JSON (default: $SDS_KLINE_DIR)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output path for the map JSON")
    args = ap.parse_args(argv)

    mp, counts = build(args.kline_dir or None)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(mp, f, ensure_ascii=False, indent=1)

    print("[build_etf_meta] entries=%d -> %s" % (len(mp), args.out))
    if counts:
        missing = [c for c in mp if c not in counts]
        print("[build_etf_meta] kline files matched=%d missing=%s"
              % (len(counts), missing or "none"))
    else:
        print("[build_etf_meta] no kline dir given -- mapping written without month counts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
