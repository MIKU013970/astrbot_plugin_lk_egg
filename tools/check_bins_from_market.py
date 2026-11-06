# -*- coding: utf-8 -*-
"""从真实挂单的「小块头 / 大块头」徽章反推每个蛋的过线值，并和 ranges.json 对照。

原理（已用 1.48 万条挂单验证）：
    大块头过线 = 体重上限 − 区间 × 2%      ← 徽章出现的最小体重
    小块头过线 = 体重下限 + 区间 × 2%      ← 徽章出现的最大体重
实测误差 ≤ 1 g。

产出：
  _market_bins.json   每个蛋：实测过线区间 / 按 ranges 推的过线 / 样本数
用法：
  python check_bins_from_market.py            # 只报告
  python check_bins_from_market.py --fix      # 把 ranges 里明显不对的过线值改成实测值
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from collections import defaultdict
from pathlib import Path

V4 = Path(r"C:\Users\MIKU\Desktop\v4")
RANGES = V4 / "astrbot_plugin_lk_egg" / "ranges.json"
SNAP = V4 / "lk_market" / "state" / "market_snapshot.json"
OUT = V4 / "_market_bins.json"


def kg(s: str):
    """"1.267~1.84 kg" → (1267, 1840)"""
    try:
        a, b = str(s).replace("kg", "").split("~")
        return float(a) * 1000, float(b) * 1000
    except Exception:
        return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true")
    args = ap.parse_args()

    snap = json.loads(SNAP.read_text(encoding="utf-8", errors="replace"))
    rows = snap["rows"]
    doc = json.loads(RANGES.read_text(encoding="utf-8"))

    egg_index = {}
    for rec in doc["species"]:
        for v in rec.get("variants") or []:
            nm = str(v.get("蛋名") or "")
            if nm:
                egg_index[nm] = (rec, v)

    by_egg = defaultdict(list)
    for r in rows:
        if isinstance(r, dict) and r.get("name") in egg_index:
            by_egg[r["name"]].append(r)

    report = {}
    ok = bad = 0
    print(f"{'蛋名':<14}{'样本':>5} {'实测大过线':>10}{'公式':>10}{'差':>6} "
          f"{'实测小过线':>10}{'公式':>10}{'差':>6}")
    for nm, rs in sorted(by_egg.items(), key=lambda kv: -len(kv[1])):
        rec, v = egg_index[nm]
        lo, hi = kg(v.get("体重区间"))
        if not lo:
            continue
        big = [int(r["weight_g"]) for r in rs if "大块头" in (r.get("medals") or [])]
        small = [int(r["weight_g"]) for r in rs if "小块头" in (r.get("medals") or [])]
        width = hi - lo
        f_big = hi - width * 0.02
        f_sml = lo + width * 0.02
        m_big = min(big) if big else None
        m_sml = max(small) if small else None
        d_big = (m_big - f_big) if m_big is not None else None
        d_sml = (m_sml - f_sml) if m_sml is not None else None
        report[nm] = {"samples": len(rs), "big_min": m_big, "small_max": m_sml,
                      "formula_big": round(f_big), "formula_small": round(f_sml),
                      "weight_span": [round(lo), round(hi)],
                      "bins": v.get("区间数")}
        if d_big is not None and abs(d_big) <= 2:
            ok += 1
        elif d_big is not None:
            bad += 1
        if len(rs) >= 5:
            print(f"{nm:<14}{len(rs):>5} "
                  f"{(m_big if m_big is not None else '—'):>10} "
                  f"{f_big:>10.0f} "
                  f"{(f'{d_big:+.0f}' if d_big is not None else '—'):>6} "
                  f"{(m_sml if m_sml is not None else '—'):>10} "
                  f"{f_sml:>10.0f} "
                  f"{(f'{d_sml:+.0f}' if d_sml is not None else '—'):>6}")

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n公式吻合（大块头误差 ≤2g）：{ok} 个 · 对不上 {bad} 个 → {OUT}")

    # 新赛季那批有没有样本
    news = ["西瓜虫的蛋", "豆粒鹅的蛋", "小剪影的蛋", "鲸羽的蛋", "蟹蟹椰的蛋",
            "滴溜豚的蛋", "乐乐鲨的蛋", "蛋包蛋的蛋", "小冥龙的蛋", "地银链的蛋",
            "沉金兽的蛋", "长江豚的蛋"]
    print("\n新赛季在市场上的样本量：")
    for nm in news:
        n = len(by_egg.get(nm, []))
        r = report.get(nm)
        extra = ""
        if r and r.get("big_min"):
            extra = f" · 实测大块头起点 {r['big_min']} g"
        print(f"  {nm:<12} {n:>4} 条{extra}")

    if args.fix and bad:
        bak = RANGES.with_suffix(f".json.bak{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(RANGES, bak)
        fixed = 0
        for nm, r in report.items():
            rec, v = egg_index[nm]
            if r["big_min"] is None or r["formula_big"] is None:
                continue
            if abs(r["big_min"] - r["formula_big"]) <= 2:
                continue
            # 过线值以实测为准
            v["大块头过线值"] = f"≥{r['big_min'] / 1000:g} kg"
            if r["small_max"] is not None:
                v["小块头过线值"] = f"≤{r['small_max'] / 1000:g} kg"
            v["数据来源"] = "市集实测徽章反推"
            fixed += 1
        doc["updated"] = int(time.time())
        RANGES.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n按实测改了 {fixed} 条 · 备份 {bak.name}")


if __name__ == "__main__":
    main()
