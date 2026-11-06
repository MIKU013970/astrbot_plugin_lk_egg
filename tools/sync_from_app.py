# -*- coding: utf-8 -*-
"""用「蛋神助手」的两份权威表校对 ranges.json：

  · state/species_ranges.json   区间数 / 身高区间 / 体重区间 / 过线值 / 极限值 / 档位 / 孵蛋时间
  · state/egg_bins.json         { 蛋名: 区间数 }（用户说的「图鉴精灵蛋里的区间数」）

蛋名归一化：`小剪影的蛋` 和 `小剪影蛋` 视为同一个。

用法：
  python sync_from_app.py                       # 自动找 app 目录，只报告
  python sync_from_app.py --app "D:\\蛋神助手"    # 指定 app 目录
  python sync_from_app.py --apply               # 写盘（先备份）
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

V4 = Path(r"C:\Users\MIKU\Desktop\v4")
OURS = V4 / "astrbot_plugin_lk_egg" / "ranges.json"
FIELDS = ("区间数", "身高区间", "体重区间", "小块头过线值", "小块头极限值",
          "大块头过线值", "大块头极限值", "档位")


def norm(s: str) -> str:
    return str(s or "").replace("的蛋", "蛋").strip()


def find_app(hint: str = "") -> Path | None:
    tests = []
    if hint:
        tests.append(Path(hint))
    tests += [Path(r"C:\Users\MIKU\Desktop\蛋神助手-自动买单"),
              Path(r"C:\Users\MIKU\Desktop\v4\蛋神助手-自动买单"),
              Path.home() / "Desktop" / "蛋神助手-自动买单"]
    for t in tests:
        if (t / "state").is_dir():
            return t
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    app = find_app(args.app)
    if not app:
        print("没找到 app 目录（里面有 state/species_ranges.json 的那个）")
        return 1
    sp_fp = app / "state" / "species_ranges.json"
    bins_fp = app / "state" / "egg_bins.json"
    print(f"app 目录: {app}")
    for fp in (sp_fp, bins_fp):
        print(f"  {fp.name}: {'有' if fp.is_file() else '没有'}"
              + (f" · {fp.stat().st_size:,} B · {time.strftime('%Y-%m-%d %H:%M', time.localtime(fp.stat().st_mtime))}"
                 if fp.is_file() else ""))

    ours = json.loads(OURS.read_text(encoding="utf-8"))
    by = {str(x.get("精灵")): x for x in ours["species"]}

    plan = []
    if sp_fp.is_file():
        appd = json.loads(sp_fp.read_text(encoding="utf-8", errors="replace"))
        for arec in appd.get("species") or []:
            nm = str(arec.get("精灵") or "")
            orec = by.get(nm)
            if not orec:
                continue
            ovars = orec.get("variants") or []
            if len(ovars) != 1:
                continue
            v = ovars[0]
            ch = [(f, v.get(f), arec.get(f)) for f in FIELDS
                  if arec.get(f) is not None and str(arec.get(f)) != str(v.get(f))]
            if ch:
                plan.append((nm, ch))

    print(f"\nspecies_ranges 与我们的字段差异：{len(plan)} 只")
    for nm, ch in plan:
        print(f"  {nm:<12} " + "；".join(f"{f} {o!r}→{a!r}" for f, o, a in ch))

    bins_plan = []
    if bins_fp.is_file():
        bins = (json.loads(bins_fp.read_text(encoding="utf-8", errors="replace")).get("bins") or {})
        idx = {}
        for rec in ours["species"]:
            for v in rec.get("variants") or []:
                idx[norm(v.get("蛋名"))] = (rec, v)
        for egg, want in bins.items():
            hit = idx.get(norm(egg))
            if not hit:
                continue
            rec, v = hit
            if str(v.get("区间数")) != str(want):
                bins_plan.append((rec.get("精灵"), egg, v.get("区间数"), want))
        print(f"\negg_bins 与我们的区间数差异：{len(bins_plan)} 个")
        for nm, egg, cur, want in bins_plan:
            print(f"  {nm:<12} {egg:<12} 我们={cur!r} app={want!r}")

    if not args.apply or not (plan or bins_plan):
        print("\n（演练模式，没改）" if (plan or bins_plan) else "\n（完全一致）")
        return 0

    bak = OURS.with_suffix(f".json.bak{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(OURS, bak)
    for nm, ch in plan:
        v = (by[nm].get("variants") or [{}])[0]
        for f, o, a in ch:
            v[f] = a
        v["数据来源"] = "蛋神助手 species_ranges 校对"
    for nm, egg, cur, want in bins_plan:
        hit = [(rec, v) for rec in ours["species"] for v in (rec.get("variants") or [])
               if norm(v.get("蛋名")) == norm(egg)]
        for rec, v in hit:
            v["区间数"] = want
            v["数据来源"] = "蛋神助手 egg_bins 校对"
    ours["updated"] = int(time.time())
    OURS.write_text(json.dumps(ours, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n改了 {len(plan) + len(bins_plan)} 处 · 备份 {bak.name}")


if __name__ == "__main__":
    raise SystemExit(main())
