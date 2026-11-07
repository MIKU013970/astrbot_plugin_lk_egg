# -*- coding: utf-8 -*-
"""看官方全表里每个赛季到底有多少只，我们缺了哪些（重点 S4）。"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = "https://wegame.shallow.ink"
HDR = {"X-API-Key": "sk-d14d6fcb35e5aa83b28e282ec24d0473",
       "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
V4 = Path(r"C:\Users\MIKU\Desktop\v4")
PLUGIN = V4 / "astrbot_plugin_lk_egg"

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
            out.append(it)
    return out


def main():
    table: dict = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for got in ex.map(one, FILTERS):
            for it in got:
                nm = str(it["name"])
                if nm not in table:
                    table[nm] = it
    print(f"官方全表 {len(table)} 只")

    by_season = defaultdict(list)
    for nm, it in table.items():
        try:
            si = int(it.get("season_id") or 0)
        except (TypeError, ValueError):
            si = 0
        by_season[f"S{si}" if si >= 1 else "S0"].append(
            (nm, bool(it.get("has_shiny")), it.get("handbook_no"), it.get("pet_id")))
    print("各赛季总数（含所有阶段）:")
    for s in ("S1", "S2", "S3", "S4", "S5", "S0"):
        rows = by_season[s]
        print(f"  {s}: {len(rows)} 只 · 有异色 {sum(1 for r in rows if r[1])}")

    doc = json.loads((PLUGIN / "ranges.json").read_text(encoding="utf-8"))
    have = {str(r.get("精灵")) for r in doc["species"]}
    print("\n我们缺的（按赛季）：")
    missing = {}
    for s in ("S1", "S2", "S3", "S4", "S5"):
        miss = [(nm, sh, hb, pid) for nm, sh, hb, pid in by_season[s] if nm not in have]
        missing[s] = miss
        print(f"  {s}: 缺 {len(miss)} 只 → {[m[0] for m in miss][:20]}")
    (V4 / "_season_missing.json").write_text(
        json.dumps({k: [{"name": n, "has_shiny": s, "no": h, "pet_id": p}
                        for n, s, h, p in v] for k, v in missing.items()},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n→ _season_missing.json")


if __name__ == "__main__":
    main()
