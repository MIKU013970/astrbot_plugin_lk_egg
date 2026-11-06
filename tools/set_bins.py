# -*- coding: utf-8 -*-
"""按「市集卡片上的区间数」批量修正 ranges.json。

用法（命令行直接给，一行一个也行）：
    python set_bins.py 豆粒鹅=9 小剪影=? ...
    python set_bins.py --file bins.txt        # 每行「名字 数字」
    python set_bins.py 豆粒鹅=9 --apply

名字可以是精灵名或蛋名（小剪影 / 小剪影的蛋 / 小剪影蛋 都认）。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from pathlib import Path

RANGES = Path(r"C:\Users\MIKU\Desktop\v4\astrbot_plugin_lk_egg\ranges.json")


def norm(s: str) -> str:
    return str(s or "").replace("的蛋", "蛋").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pairs", nargs="*", help="名字=数字")
    ap.add_argument("--file", default="")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    items: list[tuple[str, int]] = []
    for p in args.pairs:
        m = re.match(r"^(.+?)[=＝:：]\s*(\d+)$", p.strip())
        if m:
            items.append((m.group(1).strip(), int(m.group(2))))
    if args.file:
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*(\S+)\s+(\d+)\s*$", line)
            if m:
                items.append((m.group(1), int(m.group(2))))
    if not items:
        print("用法： python set_bins.py 豆粒鹅=9 小剪影=6 ...")
        return 1

    doc = json.loads(RANGES.read_text(encoding="utf-8"))
    idx = {}
    for rec in doc["species"]:
        for v in rec.get("variants") or []:
            idx.setdefault(norm(v.get("蛋名")), (rec, v))
            idx.setdefault(norm(rec.get("精灵")), (rec, v))
            for x in (v.get("形态"), v.get("形态简称")):
                if x:
                    idx.setdefault(norm(x), (rec, v))

    plan = []
    for nm, want in items:
        hit = idx.get(norm(nm))
        if not hit:
            print(f"  ✗ {nm}: ranges 里没有")
            continue
        rec, v = hit
        cur = v.get("区间数")
        if str(cur) != str(want):
            plan.append((rec.get("精灵"), v.get("蛋名"), cur, want))
        else:
            print(f"  = {nm}: 已经是 {want}")

    print(f"\n要改 {len(plan)} 条")
    for sp, egg, cur, want in plan:
        print(f"  {sp:<10} {egg:<12} {cur!r} → {want}")

    if not args.apply or not plan:
        print("\n（演练模式，加 --apply 写盘）" if plan else "")
        return 0

    bak = RANGES.with_suffix(f".json.bak{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(RANGES, bak)
    for sp, egg, cur, want in plan:
        rec, v = idx[norm(egg)]
        v["区间数"] = want
        v["数据来源"] = "小程序市集卡片（人工核对）"
    doc["updated"] = int(time.time())
    RANGES.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已写入，备份 {bak.name}")


if __name__ == "__main__":
    main()
