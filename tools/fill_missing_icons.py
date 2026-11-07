# -*- coding: utf-8 -*-
"""补图标：官方接口里有的就下载（异色立绘 / 精灵蛋 / 异色蛋），没有的跳过。

用法：python tools/fill_missing_icons.py [--apply]
"""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

PLUGIN = Path(__file__).resolve().parent.parent
ICONS = PLUGIN / "icons"
BASE = "https://wegame.shallow.ink"
HDR = {"X-API-Key": "sk-d14d6fcb35e5aa83b28e282ec24d0473",
       "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

#: 需要补的（人工列出来的缺口）
NEED_SHINY = ["呆呆铃", "跳跳铃", "铃兰夫人"]
NEED_EGG = ["伊兰龙", "呆呆铃", "梦游", "淤泥乌泽", "跳跳铃", "铃兰夫人"]
NEED_SHINY_EGG = ["雅丹鬃", "声波缇塔", "长江豚", "呆呆铃", "跳跳铃", "铃兰夫人", "伊里斯"]


def get_json(path: str, params: dict) -> dict:
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=25) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return {}


def get_png(url: str):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=30) as r:
            raw = r.read()
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
        return im, raw
    except Exception:
        return None, None


def save(im, dst: Path) -> bool:
    try:
        im.thumbnail((96, 96), Image.LANCZOS)
        c = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
        c.paste(im, ((96 - im.width) // 2, (96 - im.height) // 2), im)
        dst.parent.mkdir(parents=True, exist_ok=True)
        c.save(dst, optimize=True)
        return True
    except Exception:
        return False


def main() -> int:
    apply = "--apply" in sys.argv
    idx_fp = ICONS / "index.json"
    idx = json.loads(idx_fp.read_text(encoding="utf-8"))
    for k in ("pet", "egg", "pet_shiny", "pet_stage1"):
        idx.setdefault(k, {})

    pets = {}
    doc = json.loads((PLUGIN / "pets.json").read_text(encoding="utf-8-sig"))
    for r in doc.get("pets") or []:
        nm = str(r.get("name") or "")
        if nm and nm not in pets:
            pets[nm] = r
    # 名字 → 全部 pet_id（同名多形态）
    all_ids: dict = {}
    for r in doc.get("pets") or []:
        nm = str(r.get("name") or "")
        pid = r.get("pet_id")
        if nm and pid:
            all_ids.setdefault(nm, []).append(pid)

    done = {"shiny": [], "egg": [], "shiny_egg": [], "miss": []}

    # ---- 异色立绘 ----
    for nm in NEED_SHINY:
        got = None
        for pid in all_ids.get(nm, []):
            im, _raw = get_png(f"{BASE}/api/v1/resources/wiki/assets/pets/{pid}/shiny.png")
            if im is not None and save(im, ICONS / "shiny" / f"shiny{pid}.png"):
                got = f"shiny{pid}"
                break
        if got:
            idx["pet_shiny"][nm] = got
            done["shiny"].append((nm, got))
        else:
            done["miss"].append(("shiny", nm))

    # ---- 精灵蛋 / 异色蛋：按蛋名去 wiki 物品里搜图标 ----
    def find_egg_icon(egg_name: str):
        d = get_json("/api/v1/games/rocom/wiki/items",
                     {"q": egg_name, "page_no": 1, "page_size": 20})
        items = ((d.get("data") or {}).get("items") or [])
        for it in items:
            nmv = str(it.get("name") or "")
            if nmv == egg_name or nmv.replace("精灵蛋", "") == egg_name.replace("的蛋", ""):
                url = it.get("icon") or ""
                if url:
                    return url, it.get("item_id")
        # 退一步：名字包含
        for it in items:
            if egg_name.replace("的蛋", "") in str(it.get("name") or ""):
                url = it.get("icon") or ""
                if url:
                    return url, it.get("item_id")
        return None, None

    for nm in NEED_EGG:
        egg = f"{nm}的蛋"
        url, iid = find_egg_icon(egg)
        im, _ = get_png(url) if url else (None, None)
        if im is not None and save(im, ICONS / "egg" / f"egg{iid}.png"):
            idx["egg"][egg] = f"egg{iid}"
            idx["egg"].setdefault(nm + "蛋", f"egg{iid}")
            done["egg"].append((nm, f"egg{iid}"))
        else:
            done["miss"].append(("egg", nm))

    for nm in NEED_SHINY_EGG:
        for cand in (f"异色{nm}的蛋", f"异色{nm}蛋"):
            url, iid = find_egg_icon(cand)
            im, _ = get_png(url) if url else (None, None)
            if im is not None and save(im, ICONS / "egg" / f"egg{iid}.png"):
                idx["egg"][cand] = f"egg{iid}"
                done["shiny_egg"].append((nm, f"egg{iid}"))
                break
        else:
            done["miss"].append(("shiny_egg", nm))

    print("补上异色立绘:", done["shiny"])
    print("补上精灵蛋:", done["egg"])
    print("补上异色蛋:", done["shiny_egg"])
    print("官方也没有（跳过）:", done["miss"])
    if apply:
        idx_fp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
        print("→ 已写 index.json")
    else:
        print("（预览模式，加 --apply 才写）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
