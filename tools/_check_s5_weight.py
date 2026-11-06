# -*- coding: utf-8 -*-
"""新赛季精灵蛋的体重：用 1.48 万条真实挂单核查我们的区间是否靠谱。

思路：市场挂单的体重一定落在真实区间内。所以
  · 实测 min 应 ≥ 我们的下限（略高）
  · 实测 max 应 ≤ 我们的上限（略低）
  · 若实测越界 → 我们的区间错了
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

V4 = Path(r"C:\Users\MIKU\Desktop\v4")
ranges = json.loads((V4 / "astrbot_plugin_lk_egg" / "ranges.json").read_text(encoding="utf-8"))
snap = json.loads((V4 / "lk_market" / "state" / "market_snapshot.json")
                  .read_text(encoding="utf-8", errors="replace"))

S5 = ["小剪影", "西瓜虫", "豆粒鹅", "鲸羽", "蟹蟹椰", "滴溜豚", "乐乐鲨", "蛋包蛋",
      "小冥龙", "地银链", "沉金兽", "长江豚", "刨雪鹅"]

egg_info = {}
for rec in ranges["species"]:
    for v in rec.get("variants") or []:
        nm = str(v.get("蛋名") or "")
        if nm:
            egg_info[nm] = v

obs = defaultdict(list)
for r in snap["rows"]:
    if isinstance(r, dict) and r.get("weight_g"):
        obs[str(r.get("name"))].append(int(r["weight_g"]))

print(f"{'蛋名':<12}{'样本':>5}{'实测体重g':>16}{'我们的区间kg':>18}{'身高cm':>12}  结论")
for sp in S5:
    egg = f"{sp}的蛋"
    v = egg_info.get(egg)
    if not v:
        print(f"{egg:<12} — ranges 里没有")
        continue
    ws = sorted(obs.get(egg, []))
    wr = str(v.get("体重区间") or "")
    try:
        a, b = wr.replace("kg", "").split("~")
        lo, hi = float(a) * 1000, float(b) * 1000
    except Exception:
        lo = hi = None
    if not ws:
        print(f"{egg:<12}{0:>5}{'（市场上没挂单）':>16}{wr:>18}{str(v.get('身高区间')):>12}  —")
        continue
    verdict = "✅ 在区间内"
    if lo and hi:
        if ws[0] < lo - 2:
            verdict = f"❌ 实测比下限还小 {ws[0]:.0f} < {lo:.0f}"
        elif ws[-1] > hi + 2:
            verdict = f"❌ 实测超上限 {ws[-1]:.0f} > {hi:.0f}"
        elif (ws[-1] - ws[0]) > (hi - lo):
            verdict = "⚠ 实测跨度比区间还大"
    print(f"{egg:<12}{len(ws):>5}{f'{ws[0]}~{ws[-1]}':>16}{wr:>18}"
          f"{str(v.get('身高区间')):>12}  {verdict}")
