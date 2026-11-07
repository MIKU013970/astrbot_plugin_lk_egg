# -*- coding: utf-8 -*-
"""生成 seasons.json：每个精灵属于哪个赛季（S1~S5）。

来源：
  · 官方 wiki API `/pets?q=名字` 里的 `season_id`（覆盖 S1~S4，667 只）
  · 查不到的（S5 新精灵）→ 记为 S5
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


def look(nm: str):
    d = get("/api/v1/games/rocom/wiki/pets", {"q": nm, "page": 1, "page_size": 10})
    for it in ((d.get("data") or {}).get("items") or []):
        if str(it.get("name")) == nm:
            return nm, it.get("season_id"), [g for g in (it.get("egg_group_names") or [])], \
                bool(it.get("has_shiny"))
    return nm, None, [], False


def main():
    doc = json.loads(RANGES.read_text(encoding="utf-8"))
    names = [str(r.get("精灵")) for r in doc["species"] if r.get("精灵")]
    print(f"要查 {len(names)} 只")

    out: dict = {}
    detail: dict = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for i, (nm, season, groups, shiny) in enumerate(ex.map(look, names), 1):
            if season is None:
                sid = "S5"            # 官方接口没有 → 新赛季
            else:
                sid = f"S{int(season)}" if int(season) >= 1 else "S0"
            out[nm] = sid
            detail[nm] = {"season": sid, "groups": groups, "has_shiny": shiny}
            if i % 50 == 0:
                print(f"  {i}/{len(names)}")

    from collections import Counter
    c = Counter(out.values())
    print("赛季分布:", dict(sorted(c.items())))
    OUT.write_text(json.dumps({"season": out, "detail": detail}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"→ {OUT}")
    for s in ("S1", "S2", "S3", "S4", "S5"):
        sample = [k for k, v in out.items() if v == s][:8]
        print(f"  {s}: {c.get(s, 0)} 只 · {sample}")


if __name__ == "__main__":
    main()
