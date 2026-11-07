# -*- coding: utf-8 -*-
"""首领化形态（stage=4，比如 满月砣 = 月亮砣的首领化）不自己下蛋：
把它的蛋图/异色蛋指到进化前一阶（月亮砣）的蛋上。

规则：同 handbook_no 里 stage 最小的那个才是「会下蛋」的，
首领化 / 领地试炼用 这些形态一律复用它的蛋图。
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

V4 = Path(r"C:\Users\MIKU\Desktop\v4")
PLUGIN = V4 / "astrbot_plugin_lk_egg"
ICONS = PLUGIN / "icons"
ROSTER = PLUGIN / "season_roster.json"

idx_fp = ICONS / "index.json"
idx = json.loads(idx_fp.read_text(encoding="utf-8"))
for k in ("egg", "egg_form", "pet_shiny"):
    idx.setdefault(k, {})

# 补：满月砣（上弦/下弦）用月亮砣的蛋
PAIRS = {
    "满月砣": ("月亮砣", {"上弦的样子": ("nrc-item_Item_107197", "nrc-item_Item_150848"),
                       "下弦的样子": ("nrc-item_Item_107198", "nrc-item_Item_150849")}),
}
added = 0
for nm, (base, forms) in PAIRS.items():
    for form, (egg_stem, shiny_stem) in forms.items():
        for key in (f"{nm}|{form}", f"{nm}蛋|{form}", f"{base}|{form}", f"{base}蛋|{form}"):
            if idx["egg_form"].get(key) != egg_stem:
                idx["egg_form"][key] = egg_stem
                added += 1
        for key in (f"异色{nm}|{form}", f"异色{nm}的蛋|{form}", f"{nm}的异色|{form}",
                    f"异色{base}|{form}", f"异色{base}的蛋|{form}"):
            if idx["egg_form"].get(key) != shiny_stem:
                idx["egg_form"][key] = shiny_stem
                added += 1
    # 无形态时的兜底
    if f"异色{nm}的蛋" not in idx["egg"]:
        idx["egg"][f"异色{nm}的蛋"] = "nrc-item_Item_150848"
        added += 1
    print(f"  {nm} 复用 {base} 的蛋图（上弦 107197/150848、下弦 107198/150849）")

idx_fp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"egg_form 共 {len(idx['egg_form'])} 条（本次写 {added} 条）")

# 花名册里标注首领化（stage>=4）
if ROSTER.is_file():
    roster = json.loads(ROSTER.read_text(encoding="utf-8"))
    n = 0
    for sid, members in roster.items():
        for nm, info in (members or {}).items():
            if not isinstance(info, dict):
                continue
            st = info.get("stage")
            try:
                leader = int(st) >= 4
            except (TypeError, ValueError):
                leader = False
            if leader and not info.get("leader"):
                info["leader"] = True
                n += 1
    ROSTER.write_text(json.dumps(roster, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"花名册标记首领化 {n} 只")
