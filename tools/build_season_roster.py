# -*- coding: utf-8 -*-
"""生成赛季花名册 season_roster.json（官方全表，含所有阶段），并把异色图按 pet_id 补齐。

  · season_roster.json：{ "S4": {"黑猫巫师": {"pet_id":3641,"has_shiny":true,"groups":[...]}} , ...}
  · 异色图：has_shiny 且本地没图的 → assets/pets/<pet_id>/shiny.png → icons/shiny/shiny<id>.png
"""
from __future__ import annotations

import io
import json
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

BASE = "https://wegame.shallow.ink"
HDR = {"X-API-Key": "sk-d14d6fcb35e5aa83b28e282ec24d0473",
       "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
V4 = Path(r"C:\Users\MIKU\Desktop\v4")
PLUGIN = V4 / "astrbot_plugin_lk_egg"
ICONS = PLUGIN / "icons"
ROSTER = PLUGIN / "season_roster.json"

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


def raw(url: str):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                    timeout=30) as r:
            return r.read()
    except Exception:
        return None


def save(raw_bytes: bytes, dst: Path) -> bool:
    try:
        im = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
        im.thumbnail((96, 96), Image.LANCZOS)
        c = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
        c.paste(im, ((96 - im.width) // 2, (96 - im.height) // 2), im)
        dst.parent.mkdir(parents=True, exist_ok=True)
        c.save(dst, optimize=True)
        return True
    except Exception:
        return False


def one(arg):
    k, n = arg
    d = get("/api/v1/games/rocom/wiki/pets", {k: n, "page": 1, "page_size": 100})
    return [it for it in ((d.get("data") or {}).get("items") or []) if isinstance(it, dict)]


def main():
    table: dict = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for got in ex.map(one, FILTERS):
            for it in got:
                nm = str(it.get("name") or "")
                if nm and nm not in table:
                    table[nm] = it
    print(f"官方全表 {len(table)} 只", flush=True)

    roster: dict = defaultdict(dict)
    for nm, it in table.items():
        try:
            si = int(it.get("season_id") or 0)
        except (TypeError, ValueError):
            si = 0
        sid = f"S{si}" if si >= 1 else "S0"
        roster[sid][nm] = {
            "pet_id": it.get("pet_id"),
            "has_shiny": bool(it.get("has_shiny")),
            "groups": [g for g in (it.get("egg_group_names") or [])],
            "no": it.get("handbook_no"),
            "stage": it.get("stage"),
        }

    # S5 里官方表没收的（按 ranges 的 S5 名单补进去）
    seasons = json.loads((PLUGIN / "seasons.json").read_text(encoding="utf-8"))["season"]
    doc = json.loads((PLUGIN / "ranges.json").read_text(encoding="utf-8"))
    grp = {str(r.get("精灵")): str(r.get("蛋组") or "") for r in doc["species"]}
    for nm, sid in seasons.items():
        if sid == "S5" and nm not in roster.get("S5", {}):
            roster.setdefault("S5", {})[nm] = {"pet_id": None, "has_shiny": True,
                                               "groups": [grp.get(nm, "")], "no": "", "stage": None}

    out = {s: dict(v) for s, v in roster.items()}
    ROSTER.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("各赛季节数：")
    for s in sorted(out):
        print(f"  {s}: {len(out[s])} 只 · 有异色 {sum(1 for v in out[s].values() if v['has_shiny'])}")

    # 补异色图
    idx_fp = ICONS / "index.json"
    idx = json.loads(idx_fp.read_text(encoding="utf-8"))
    for k in ("pet", "egg", "pet_shiny", "pet_stage1"):
        idx.setdefault(k, {})
    todo = []
    for s in ("S1", "S2", "S3", "S4", "S5"):
        for nm, v in out[s].items():
            if not v.get("has_shiny") or not v.get("pet_id"):
                continue
            if nm in idx["pet_shiny"]:
                continue
            todo.append((nm, v["pet_id"]))
    print(f"\n需要补异色图 {len(todo)} 只", flush=True)

    def work(item):
        nm, pid = item
        data = raw(f"{BASE}/api/v1/resources/wiki/assets/pets/{pid}/shiny.png")
        if not data:
            return nm, None
        if save(data, ICONS / "shiny" / f"shiny{pid}.png"):
            return nm, f"shiny{pid}"
        return nm, None

    added = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for nm, tag in ex.map(work, todo):
            if tag:
                idx["pet_shiny"][nm] = tag
                added.append((nm, tag))
    idx_fp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"补上 {len(added)} 只：")
    for nm, tag in added[:60]:
        print(f"  {nm:<10} {tag}")
    print(f"pet_shiny 总数 {len(idx['pet_shiny'])}")


if __name__ == "__main__":
    main()
