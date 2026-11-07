# -*- coding: utf-8 -*-
"""补异色蛋：按 **赛季花名册**（不只是图鉴 206 种）扫 wiki，缺的异色蛋/异色图全补。

重点 S4：官方花名册 S4 有 47 只（44 只有异色），而我们图鉴里只有 8 只。
"""
from __future__ import annotations

import io
import json
import re
import sys
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/144.0.0.0 Safari/537.36")
WIKI = "https://wiki.biligame.com/nrc/"
API = WIKI + "api.php"
V4 = Path(r"C:\Users\MIKU\Desktop\v4")
PLUGIN = V4 / "astrbot_plugin_lk_egg"
ICONS = PLUGIN / "icons"
_lock = threading.Lock()


def http(url: str, timeout: int = 40):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": WIKI})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def img_url(title: str) -> str:
    q = urllib.parse.urlencode({"action": "query", "titles": "文件:" + title,
                                "prop": "imageinfo", "iiprop": "url",
                                "format": "json", "formatversion": "2"})
    raw = http(API + "?" + q)
    if not raw:
        return ""
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return ""
    for p in (d.get("query") or {}).get("pages") or []:
        for ii in p.get("imageinfo") or []:
            if ii.get("url"):
                return str(ii["url"])
    return ""


def save_raw(raw: bytes, dst: Path) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(raw)
    return dst.stat().st_size > 200


def save_pet(raw: bytes, dst: Path) -> bool:
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
        im.thumbnail((96, 96), Image.LANCZOS)
        c = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
        c.paste(im, ((96 - im.width) // 2, (96 - im.height) // 2), im)
        dst.parent.mkdir(parents=True, exist_ok=True)
        c.save(dst, optimize=True)
        return True
    except Exception:
        return False


def main(seasons=("S1", "S2", "S3", "S4", "S5")):
    idx_fp = ICONS / "index.json"
    idx = json.loads(idx_fp.read_text(encoding="utf-8"))
    for k in ("pet", "egg", "pet_shiny", "pet_stage1"):
        idx.setdefault(k, {})
    roster = json.loads((PLUGIN / "season_roster.json").read_text(encoding="utf-8"))

    todo = []
    for s in seasons:
        for nm, info in (roster.get(s) or {}).items():
            if not isinstance(info, dict) or not info.get("has_shiny"):
                continue
            need = []
            if f"异色{nm}的蛋" not in idx["egg"]:
                need.append("egg")
            if nm not in idx["pet_shiny"]:
                need.append("shiny")
            if need:
                todo.append((s, nm, need, info.get("pet_id")))
    print(f"待补 {len(todo)} 只（缺异色蛋或异色图）", flush=True)
    from collections import Counter
    print("  按赛季:", dict(Counter(s for s, *_ in todo)), flush=True)

    added, missing = [], []

    def work(item):
        s, nm, need, pid = item
        hands = {}
        if "shiny" in need and pid:
            data = http(f"https://wegame.shallow.ink/api/v1/resources/wiki/assets/pets/{pid}/shiny.png")
            if data and save_pet(data, ICONS / "shiny" / f"shiny{pid}.png"):
                hands["shiny"] = f"shiny{pid}"
        if "egg" in need or "shiny" in need:
            raw = http(WIKI + urllib.parse.quote(nm))
            if raw:
                h = raw.decode("utf-8", "replace")
                files = []
                for f in (urllib.parse.unquote(x) for x in re.findall(r"文件:([^\"'&]+?\.png)", h)):
                    if f not in files:
                        files.append(f)
                se = [f for f in files if re.fullmatch(r"Item 15\d{4}\.png", f)]
                sp_ = [f for f in files if f.startswith("PetPortrait") and "yise" in f]
                if "shiny" not in hands and sp_:
                    num = re.search(r'data-sd-p-pet="pet_(\d+)"', h)
                    tag = f"nrc{int(num.group(1))}" if num else ""
                    if tag:
                        data = http(img_url(sp_[0]))
                        if data and save_pet(data, ICONS / "shiny" / f"{tag}.png"):
                            hands["shiny"] = tag
                if se:
                    m = re.match(r"Item (\d+)\.png", se[0])
                    if m:
                        stem = f"nrc-item_Item_{m.group(1)}"
                        data = http(img_url(se[0]))
                        if data and save_raw(data, ICONS / "egg" / f"{stem}.png"):
                            hands["egg"] = stem
        return s, nm, hands

    done = 0
    with ThreadPoolExecutor(max_workers=5) as ex:
        for s, nm, hands in ex.map(work, todo):
            done += 1
            if hands:
                with _lock:
                    if "shiny" in hands:
                        idx["pet_shiny"][nm] = hands["shiny"]
                    if "egg" in hands:
                        idx["egg"][f"异色{nm}的蛋"] = hands["egg"]
                added.append((s, nm, hands))
                print(f"  [{done}/{len(todo)}] ✓ {s} {nm:<10} {hands}", flush=True)
            else:
                missing.append((s, nm))
            if done % 20 == 0:
                print(f"  [{done}/{len(todo)}] …", flush=True)

    idx_fp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n补上 {len(added)} 只")
    print(f"仍没有 {len(missing)} 只：{[m[1] for m in missing][:40]}")


if __name__ == "__main__":
    want = tuple(a for a in sys.argv[1:] if a.upper().startswith("S")) or ("S1", "S2", "S3", "S4", "S5")
    main(want)
