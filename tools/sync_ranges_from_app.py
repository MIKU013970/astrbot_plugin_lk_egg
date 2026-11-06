# -*- coding: utf-8 -*-
"""用 app 的 state/species_ranges.json（192 条，市集实测）校对我们的 ranges.json。

app 那边是「档位 / 区间数 / 身高区间 / 体重区间 / 过线值 / 极限值」的权威来源
（用户给的真值：海枝枝 31、果冻 15、机械方方 33 —— 我们和它一致）。

用法：
  python sync_ranges_from_app.py            # 只报告
  python sync_ranges_from_app.py --apply    # 把不一致的按 app 改（先备份）
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

APP = Path(r"C:\Users\MIKU\Desktop\蛋神助手-自动买单\state\species_ranges.json")
OURS = Path(r"C:\Users\MIKU\Desktop\v4\astrbot_plugin_lk_egg\ranges.json")
FIELDS = ("区间数", "身高区间", "体重区间", "小块头过线值", "小块头极限值",
          "大块头过线值", "大块头极限值")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    app = json.loads(APP.read_text(encoding="utf-8", errors="replace"))
    ours = json.loads(OURS.read_text(encoding="utf-8"))
    app_by = {str(x.get("精灵")): x for x in app["species"]}
    ours_by = {str(x.get("精灵")): x for x in ours["species"]}
    print(f"app {len(app_by)} 只 · 我们 {len(ours_by)} 只\n")

    only_app = [k for k in app_by if k not in ours_by]
    only_ours = [k for k in ours_by if k not in app_by]
    print(f"只在 app 里有：{len(only_app)} 个 {only_app[:12]}")
    print(f"只在我们这里有：{len(only_ours)} 个 {only_ours[:12]}\n")

    plan = []
    for nm, arec in app_by.items():
        orec = ours_by.get(nm)
        if not orec:
            continue
        ovars = orec.get("variants") or []
        if len(ovars) != 1:
            continue          # 多形态的先不动，单独处理
        v = ovars[0]
        changes = []
        for f in FIELDS:
            a, o = arec.get(f), v.get(f)
            if a is None:
                continue
            if str(a) != str(o):
                changes.append((f, o, a))
        if changes:
            plan.append((nm, changes))

    print(f"字段不一致的 {len(plan)} 只：")
    for nm, ch in plan:
        print(f"  {nm:<12}")
        for f, o, a in ch:
            print(f"     {f:<8} 我们={o!r:<22} app={a!r}")
    if not args.apply or not plan:
        print("\n（演练模式，没改）" if plan else "\n（完全一致）")
        return

    bak = OURS.with_suffix(f".json.bak{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(OURS, bak)
    for nm, ch in plan:
        v = (ours_by[nm].get("variants") or [{}])[0]
        for f, o, a in ch:
            v[f] = a
        v["数据来源"] = "app species_ranges（市集实测）校对"
    ours["updated"] = int(time.time())
    OURS.write_text(json.dumps(ours, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n改了 {len(plan)} 只 · 备份 {bak.name}")


if __name__ == "__main__":
    main()
