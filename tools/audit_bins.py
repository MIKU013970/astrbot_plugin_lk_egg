# -*- coding: utf-8 -*-
"""区间数体检：按「每档 45~50 g」这条实测规律，把明显不对的挑出来。"""
from __future__ import annotations

import json
from pathlib import Path

V4 = Path(r"C:\Users\MIKU\Desktop\v4")
doc = json.loads((V4 / "astrbot_plugin_lk_egg" / "ranges.json").read_text(encoding="utf-8"))

BAND = (44.0, 51.0)      # 实测每档体重（g）
rows = []
for rec in doc["species"]:
    for v in rec.get("variants") or []:
        b = v.get("区间数")
        wr = str(v.get("体重区间") or "")
        try:
            a, c = wr.replace("kg", "").split("~")
            lo, hi = float(a) * 1000, float(c) * 1000
        except Exception:
            continue
        if not b or hi <= lo:
            continue
        per = (hi - lo) / b
        rows.append((str(v.get("蛋名") or rec.get("精灵")), b, hi - lo, per,
                     round((hi - lo) / 47), str(v.get("数据来源") or "")[:22]))

rows.sort(key=lambda r: r[3])
bad = [r for r in rows if not (BAND[0] <= r[3] <= BAND[1])]
print(f"有区间数的蛋 {len(rows)} 个 · 每档不在 {BAND[0]}~{BAND[1]} g 的 {len(bad)} 个"
      f"（{len(bad) / len(rows) * 100:.0f}%）\n")
print(f"{'蛋名':<16}{'区间数':>6}{'体重宽g':>9}{'每档g':>8}{'按47推算':>9}  来源")
for nm, b, w, per, est, src in bad:
    print(f"{nm:<16}{b:>6}{w:>9.0f}{per:>8.1f}{est:>9}  {src}")

print("\n=== 新赛季那批的每档体重（应该都在 45~50）===")
for nm, b, w, per, est, src in rows:
    if any(k in nm for k in ("西瓜虫", "豆粒鹅", "鲸羽", "蟹蟹椰", "滴溜豚", "乐乐鲨",
                             "蛋包蛋", "小冥龙", "地银链", "沉金兽", "长江豚", "小剪影")):
        print(f"  {nm:<12} 区间数={b:<5} 宽={w:>5.0f}g 每档={per:>5.1f}g")
