# -*- coding: utf-8 -*-
"""用 app 自己 11/6 的市场快照（25461 条，含新赛季挂单）核对新赛季蛋的体重区间 + 过线值。

注意名称差异：市场叫「小剪影蛋」，ranges 里叫「小剪影的蛋」，要先归一化。
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

APP = Path(r"C:\Users\MIKU\Desktop\蛋神助手-自动买单\state\market_snapshot.json")
V4 = Path(r"C:\Users\MIKU\Desktop\v4")

d = json.loads(APP.read_text(encoding="utf-8", errors="replace"))
rows = d["rows"]

ranges = json.loads((V4 / "astrbot_plugin_lk_egg" / "ranges.json").read_text(encoding="utf-8"))
egg_info = {}
for rec in ranges["species"]:
    for v in rec.get("variants") or []:
        nm = str(v.get("蛋名") or "")
        if not nm:
            continue
        egg_info[nm.replace("的蛋", "蛋")] = v


def norm(s: str) -> str:
    """统一成「X蛋」：小剪影的蛋 / 小剪影蛋 都变 小剪影蛋。"""
    return str(s).replace("的蛋", "蛋")


obs = defaultdict(list)
medals = defaultdict(lambda: {"small": [], "big": []})
for r in rows:
    if not isinstance(r, dict):
        continue
    nm = str(r.get("name") or "")
    w = r.get("weight_g")
    if not nm or not isinstance(w, (int, float)):
        continue
    obs[norm(nm)].append(int(w))
    md = r.get("medals") or []
    if "小块头" in md:
        medals[norm(nm)]["small"].append(int(w))
    if "大块头" in md:
        medals[norm(nm)]["big"].append(int(w))

S5 = ["小剪影", "西瓜虫", "豆粒鹅", "鲸羽", "蟹蟹椰", "滴溜豚", "乐乐鲨", "蛋包蛋",
      "长江豚", "云梦豚", "小帕尔", "绒绒"]

print(f"市场快照 {len(rows)} 条（app 11/6）\n")
print(f"{'蛋':<10}{'样本':>4}{'实测体重g':>16}{'我们的区间kg':>16}{'身高cm':>11}"
      f"{'大块头实测':>11}{'公式':>9}  结论")
allok = True
for sp in S5:
    key = f"{sp}蛋"
    v = egg_info.get(key)
    ws = sorted(obs.get(key, []))
    if not v or not ws:
        print(f"{key:<10}{len(ws):>4}{'—':>16}{'（无数据）':>16}")
        continue
    wr = str(v.get("体重区间") or "")
    try:
        a, b = wr.replace("kg", "").split("~")
        lo, hi = float(a) * 1000, float(b) * 1000
    except Exception:
        lo = hi = None
    verdict = ""
    if lo and hi:
        if ws[0] < lo - 30 or ws[-1] > hi + 30:
            verdict = f"❌ 越界"
            allok = False
        else:
            verdict = "✅ 在区间内"
    big = sorted(medals[key]["big"])
    f_big = (hi - (hi - lo) * 0.02) if lo else None
    print(f"{key:<10}{len(ws):>4}{f'{ws[0]}~{ws[-1]}':>16}{wr:>16}"
          f"{str(v.get('身高区间')):>11}"
          f"{(str(big[0]) if big else '—'):>11}"
          f"{(f'{f_big:.0f}' if f_big else '—'):>9}  {verdict}")

print(f"\n新赛季蛋体重：{'全部落在我们给的区间内 ✅' if allok else '有越界 ❌'}")
