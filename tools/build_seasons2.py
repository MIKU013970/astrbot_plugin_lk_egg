# -*- coding: utf-8 -*-
"""重建 seasons.json：用**筛选穷举**把官方 667 只全拉一遍，拿准 season_id。

之前的做法是「按名字查，查不到就归 S5」—— 名字对不上（别名/形态名）时会误判成 S5
（守夜烛、稻草人 就是这么被错标成 S5 的）。
现在改成：先用 type_id / egg_group_id / season_id 等筛选把全表拉出来
（name → season_id），再拿 ranges 里的名字去对；对不上的才是真 S5。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = "https://wegame.shallow.ink"
HDR = {"X-API-Key": "sk-d14d6fcb35e5aa83b28e282ec24d0473",
       "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
V4 = Path(r"C:\Users\MIKU\Desktop\v4")
PLUGIN = V4 / "astrbot_plugin_lk_egg"
RANGES = PLUGIN / "ranges.json"
OUT = PLUGIN / "seasons.json"

FILTERS = ([(k, n) for k in ("type_id",) for n in range(1, 26)]
           + [(k, n) for k in ("egg_group_id",) for n in range(1, 21)]
           + [("season_id", n) for n in range(0, 8)]
           + [("stage", n) for n in (1, 2, 3)]
           + [("ride_talent", 0), ("ride_talent", 1)])


def get(path: str, params: dict):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    for _ in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HDR),
                                        timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            time.sleep(0.8)
    return {}


def one(arg):
    k, n = arg
    d = get("/api/v1/games/rocom/wiki/pets", {k: n, "page": 1, "page_size": 100})
    out = []
    for it in ((d.get("data") or {}).get("items") or []):
        if isinstance(it, dict) and it.get("name"):
            out.append((str(it["name"]), it.get("season_id"),
                        [g for g in (it.get("egg_group_names") or [])],
                        bool(it.get("has_shiny"))))
    return out


def main():
    table: dict = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for got in ex.map(one, FILTERS):
            for nm, sid, groups, shiny in got:
                if nm not in table:
                    table[nm] = {"season_id": sid, "groups": groups, "has_shiny": shiny}
    print(f"官方全表拉到 {len(table)} 只")

    doc = json.loads(RANGES.read_text(encoding="utf-8"))
    names = [str(r.get("精灵")) for r in doc["species"] if r.get("精灵")]
    season, detail, missing = {}, {}, []
    for nm in names:
        row = table.get(nm)
        if row is None:
            # ranges 里有的别名/形态名，再试一次包含匹配
            cand = [k for k in table if nm in k or k in nm]
            if len(cand) == 1:
                row = table[cand[0]]
                nm_key = cand[0]
            else:
                missing.append(nm)
                season[nm] = "S5"          # 官方表里没有 → 新赛季
                continue
        sid = row.get("season_id")
        try:
            si = int(sid)
        except (TypeError, ValueError):
            si = 0
        season[nm] = f"S{si}" if si >= 1 else "S0"
        detail[nm] = {"season": season[nm], "groups": row.get("groups") or [],
                      "has_shiny": row.get("has_shiny")}

    from collections import Counter
    c = Counter(season.values())
    print("赛季分布:", dict(sorted(c.items())))
    print(f"官方表里查不到（判为 S5）{len(missing)} 只：{missing[:40]}")
    for s in ("S1", "S2", "S3", "S4", "S5", "S0"):
        print(f"  {s}: {c.get(s, 0)}")
    OUT.write_text(json.dumps({"season": season, "detail": detail}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"→ {OUT}")
    for nm in ("守夜烛", "稻草人", "小剪影", "西瓜虫", "花怨鳗", "锤头鹳", "绒绒", "加灵"):
        print(f"  {nm}: {season.get(nm)}")


if __name__ == "__main__":
    main()
