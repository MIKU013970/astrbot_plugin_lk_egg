# -*- coding: utf-8 -*-
"""补「一阶段图（pet_stage1）」和「蛋图（egg）」的缺口，并核对异色。

一阶段图规则：进化链第一段的名字 → 用它的精灵图；链上其它阶段都指向它。
蛋图规则：每个精灵的「X的蛋」都要有 egg 图。
"""
from __future__ import annotations

import json
from pathlib import Path

V4 = Path(r"C:\Users\MIKU\Desktop\v4")
PLUGIN = V4 / "astrbot_plugin_lk_egg"
ICONS = PLUGIN / "icons"

idx = json.loads((ICONS / "index.json").read_text(encoding="utf-8"))
for k in ("pet", "egg", "pet_shiny", "pet_stage1"):
    idx.setdefault(k, {})
doc = json.loads((PLUGIN / "ranges.json").read_text(encoding="utf-8"))
seasons = json.loads((PLUGIN / "seasons.json").read_text(encoding="utf-8"))["season"]

# 1) 一阶段图：按进化链
fixed_stage1 = []
for rec in doc["species"]:
    parts = [x.strip() for x in str(rec.get("进化链") or "").replace("→", "->").split("->")
             if x.strip()]
    if not parts:
        parts = [str(rec.get("精灵") or "")]
    base = parts[0]
    tag = idx["pet"].get(base) or idx["pet_stage1"].get(base)
    if not tag:
        continue
    for stage in parts:
        if idx["pet_stage1"].get(stage) != tag:
            idx["pet_stage1"][stage] = tag
            fixed_stage1.append((stage, base, tag))
    # 一阶段自己的异色也挂上（异色一阶段）
    s = idx["pet_shiny"].get(base)
    if s and idx["pet_shiny"].get(parts[0]) != s:
        idx["pet_shiny"].setdefault(parts[0], s)

# 2) 蛋图：缺的用「同进化链上一阶段的蛋图」补（同蛋到底）
fixed_egg = []
for rec in doc["species"]:
    nm = str(rec.get("精灵") or "")
    egg_name = str(rec.get("蛋名") or "")
    if not nm or not egg_name:
        continue
    if egg_name in idx["egg"]:
        continue
    parts = [x.strip() for x in str(rec.get("进化链") or "").replace("→", "->").split("->")
             if x.strip()]
    for other in [f"{p}的蛋" for p in parts] + [f"{p}蛋" for p in parts]:
        if other in idx["egg"]:
            idx["egg"][egg_name] = idx["egg"][other]
            fixed_egg.append((egg_name, other, idx["egg"][other]))
            break

(ICONS / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
print(f"一阶段图补/改 {len(fixed_stage1)} 条：")
for s, b, t in fixed_stage1[:20]:
    print(f"  {s} → {b}（{t}）")
print(f"蛋图按进化链补 {len(fixed_egg)} 条：")
for a, b, t in fixed_egg[:20]:
    print(f"  {a} ← {b}（{t}）")

# 3) 统计各赛季的异色覆盖
have_shiny = set(idx["pet_shiny"])
have_egg = {k[2:] for k in idx["egg"] if str(k).startswith("异色")}
print("\n各赛季异色覆盖：")
for sid in ("S1", "S2", "S3", "S4", "S5", "S0"):
    names = [n for n, s in seasons.items()
             if s == sid and not n.endswith("的蛋")]
    sh = [n for n in names if n in have_shiny]
    se = [n for n in names if f"{n}的蛋" in have_egg]
    print(f"  {sid}: {len(names)} 只 · 异色图 {len(sh)} · 异色蛋 {len(se)}")
    if sid == "S5":
        print(f"      有异色蛋的：{'、'.join(se)}")
        print(f"      只有异色图的：{'、'.join(n for n in sh if n not in se)}")
