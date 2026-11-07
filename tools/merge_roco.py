# -*- coding: utf-8 -*-
"""把 roco.world 抓到的精灵并进 `pets.json`（只补我们缺的，不覆盖官方字段）。

规则：
* 名字已在官方表 / 图鉴里的 → 什么都不动（官方为准）
* roco 独有的 → 追加一条 `source=roco` 的记录（season 0 / has_shiny False），
  带上图鉴号、属性、成体身高体重；**不写蛋组**（roco 的蛋组叫法和我们不是一套，
  硬塞会出现「陆上组 / 人型组」这种错组）
* 顺手补空字段：官方记录里 `no` 为空、`types` 为空的，用 roco 的补

用法：python tools/merge_roco.py [--apply]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
PETS = PLUGIN / "pets.json"
ROCO = PLUGIN / "roco_pets.json"


def main() -> int:
    apply = "--apply" in sys.argv
    roco = json.loads(ROCO.read_text(encoding="utf-8"))["pets"]
    doc = json.loads(PETS.read_text(encoding="utf-8"))
    rows = doc.get("pets") or []
    have = {str(r.get("name") or "") for r in rows}
    print(f"官方表 {len(rows)} 条 / {len(have)} 个名字 · roco {len(roco)} 只")

    added, filled = [], []
    for nm, info in roco.items():
        if nm in have:
            # 补空字段
            for r in rows:
                if r.get("name") != nm:
                    continue
                changed = False
                if not r.get("no") and info.get("dex"):
                    r["no"] = str(info["dex"])
                    changed = True
                if not (r.get("types") or []) and info.get("types"):
                    r["types"] = info["types"]
                    changed = True
                if not r.get("height") and info.get("height"):
                    r["height"] = info["height"]
                    r["weight"] = info.get("weight") or ""
                    changed = True
                if changed:
                    filled.append(nm)
                break
            continue
        rows.append({
            "name": nm,
            "pet_id": None,
            "no": str(info.get("dex") or ""),
            "season": 0,
            "stage": 0,
            "has_shiny": False,
            "ride": False,
            "groups": [],
            "types": info.get("types") or [],
            "quality": None,
            "height": info.get("height") or "",
            "weight": info.get("weight") or "",
            "source": "roco",
        })
        have.add(nm)
        added.append(nm)

    print(f"新增 {len(added)} 只：{added[:20]}{' …' if len(added) > 20 else ''}")
    print(f"补空字段 {len(filled)} 只：{filled[:15]}{' …' if len(filled) > 15 else ''}")
    print(f"合并后总条数 {len(rows)}（唯一名 {len(have)}）")

    if apply:
        doc["pets"] = rows
        doc["count"] = len(rows)
        doc["rocoMerged"] = time.strftime("%Y-%m-%d")
        PETS.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"→ 已写入 {PETS.name}（{PETS.stat().st_size / 1024:.0f} KB）")
    else:
        print("（只预览，加 --apply 才写）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
