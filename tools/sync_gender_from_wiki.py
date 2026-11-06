# -*- coding: utf-8 -*-
"""用 wiki「孵蛋组别查询」的公母标记校对 ranges.json，并列差异。

公母归一化：
  仅雄 = "只有雄性（100% ♂）" ≡ "雄性 100% / 雌性 0%"
  仅雌 = "只有雌性（100% ♀）" ≡ "雄性 0% / 雌性 100%"
  两性 = "雄性 50% / 雌性 50%"（wiki 上没标记的即为两性）

用法：
  python sync_gender_from_wiki.py            # 只看差异
  python sync_gender_from_wiki.py --apply     # 落盘（先备份）
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

V4 = Path(r"C:\Users\MIKU\Desktop\v4")
RANGES = V4 / "astrbot_plugin_lk_egg" / "ranges.json"
WG = V4 / "_wiki_gender.json"
SNAP = V4 / "lk_market" / "state" / "market_snapshot.json"


def norm(s: str) -> str:
    """把各种写法归一成 3 类：only_m / only_f / both / unknown。"""
    t = str(s or "").replace(" ", "")
    if not t:
        return "unknown"
    if "100%♂" in t or "雄性100%/雌性0%" in t or "只有雄性" in t:
        return "only_m"
    if "100%♀" in t or "雄性0%/雌性100%" in t or "只有雌性" in t:
        return "only_f"
    if "雄性50%/雌性50%" in t or "50%/50%" in t:
        return "both"
    return "other"


TEXT = {"only_m": "只有雄性（100% ♂）", "only_f": "只有雌性（100% ♀）",
        "both": "雄性 50% / 雌性 50%"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    wiki = json.loads(WG.read_text(encoding="utf-8"))
    doc = json.loads(RANGES.read_text(encoding="utf-8"))

    plan = []
    for rec in doc.get("species") or []:
        nm = str(rec.get("精灵") or "")
        w = wiki.get(nm)
        if not w:
            continue
        want_kind = norm(w.get("公母")) if w.get("公母") else "both"
        cur = str(rec.get("公母") or "")
        cur_kind = norm(cur)
        if want_kind == "other" or cur_kind == "other":
            continue
        if cur_kind == want_kind:
            continue
        plan.append((nm, cur, TEXT.get(want_kind, w.get("公母")), cur_kind, want_kind,
                     w.get("编号"), w.get("蛋组")))

    print(f"wiki 有公母信息的蛋 {len(wiki)} 个 · 和 ranges 对不上的 {len(plan)} 条\n")
    for nm, cur, want, ck, wk, num, grp in plan:
        print(f"  {nm:<12} 现在={cur!r:<30} wiki={want!r:<22} "
              f"(编号 {num} · {grp})")

    if not plan:
        print("  （全部一致）")
    if not args.apply or not plan:
        print("\n（没改。加 --apply 才写盘）" if plan else "")
        return

    bak = RANGES.with_suffix(f".json.bak{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(RANGES, bak)
    by_name = {str(r.get("精灵") or ""): r for r in doc["species"]}
    for nm, cur, want, *_ in plan:
        rec = by_name[nm]
        rec["公母"] = want
        for v in (rec.get("variants") or []):
            v["公母"] = want
            v["数据来源"] = "wiki（孵蛋组别查询）校对公母"
    doc["updated"] = int(time.time())
    RANGES.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n改了 {len(plan)} 条 · 已写入\n备份 {bak.name}")


if __name__ == "__main__":
    main()
