# -*- coding: utf-8 -*-
"""多形态精灵：wiki 上「异色蛋 / 异色立绘」常常挂在**形态页**上，不在主页面。

比如 月亮砣（上弦的样子） 单独一页：https://wiki.biligame.com/nrc/月亮砣（上弦的样子）
这里把 ranges 里的 形态 / 形态简称 都当候选页名，逐个现抓，找 Item 15xxxx 和 yise 立绘。
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


def scan_page(title: str):
    raw = http(WIKI + urllib.parse.quote(title))
    if not raw:
        return None
    h = raw.decode("utf-8", "replace")
    if "请求已被" in h or "维基百科并没有" in h:
        return None
    files = []
    for f in (urllib.parse.unquote(x) for x in re.findall(r"文件:([^\"'&]+?\.png)", h)):
        if f not in files:
            files.append(f)
    num = re.search(r'data-sd-p-pet="pet_(\d+)"', h)
    return {
        "files": files,
        "tag": f"nrc{int(num.group(1))}" if num else "",
        "egg": next((f for f in files if re.fullmatch(r"Item (107|108|149)\d{3}\.png", f)), ""),
        "egg_shiny": next((f for f in files if re.fullmatch(r"Item 15\d{4}\.png", f)), ""),
        "shiny_portrait": next((f for f in files if f.startswith("PetPortrait") and "yise" in f), ""),
    }


def main(only=None):
    idx_fp = ICONS / "index.json"
    idx = json.loads(idx_fp.read_text(encoding="utf-8"))
    for k in ("pet", "egg", "pet_shiny", "pet_stage1"):
        idx.setdefault(k, {})
    doc = json.loads((PLUGIN / "ranges.json").read_text(encoding="utf-8"))

    todo = []
    for rec in doc["species"]:
        nm = str(rec.get("精灵") or "")
        if not nm or nm.endswith("的蛋"):
            continue
        if only and nm not in only:
            continue
        need_egg = f"异色{nm}的蛋" not in idx["egg"]
        need_shiny = nm not in idx["pet_shiny"]
        if not (need_egg or need_shiny):
            continue
        titles = []
        for v in rec.get("variants") or []:
            for key in ("形态", "形态简称"):
                x = str(v.get(key) or "").strip()
                if not x or "异色" in x:
                    continue
                for t in (x if x.startswith(nm) else f"{nm}（{x}）", x):
                    if t not in titles:
                        titles.append(t)
        if nm not in titles:
            titles.insert(0, nm)
        todo.append((nm, titles, need_egg, need_shiny))

    print(f"多形态/缺图精灵 {len(todo)} 只", flush=True)
    added = []
    still = []

    def work(item):
        nm, titles, need_egg, need_shiny = item
        found = {}
        for t in titles[:6]:
            info = scan_page(t)
            if not info:
                continue
            if need_shiny and info["shiny_portrait"] and "shiny" not in found:
                found["shiny"] = (info["shiny_portrait"], info["tag"], t)
            if need_egg and info["egg_shiny"] and "egg" not in found:
                found["egg"] = (info["egg_shiny"], t)
            if "shiny" in found and "egg" in found:
                break
        hands = {}
        if "shiny" in found:
            f, tag, t = found["shiny"]
            data = http(img_url(f))
            if data and tag and save_pet(data, ICONS / "shiny" / f"{tag}.png"):
                hands["shiny"] = tag
        if "egg" in found:
            f, t = found["egg"]
            m = re.match(r"Item (\d+)\.png", f)
            if m:
                stem = f"nrc-item_Item_{m.group(1)}"
                data = http(img_url(f))
                if data and save_raw(data, ICONS / "egg" / f"{stem}.png"):
                    hands["egg"] = stem
        return nm, hands, [t for t in titles[:6]]

    done = 0
    with ThreadPoolExecutor(max_workers=5) as ex:
        for nm, hands, titles in ex.map(work, todo):
            done += 1
            if hands:
                with _lock:
                    if "shiny" in hands:
                        idx["pet_shiny"][nm] = hands["shiny"]
                    if "egg" in hands:
                        idx["egg"][f"异色{nm}的蛋"] = hands["egg"]
                added.append((nm, hands))
                print(f"  [{done}/{len(todo)}] ✓ {nm:<10} {hands}", flush=True)
            else:
                still.append((nm, titles))
            if done % 20 == 0:
                print(f"  [{done}/{len(todo)}] …", flush=True)

    idx_fp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n补上 {len(added)} 只：{[a[0] for a in added]}")
    print(f"仍缺 {len(still)} 只：")
    for nm, titles in still[:25]:
        print(f"  {nm:<10} 试过页名 {titles}")
    print(f"index egg {len(idx['egg'])} · shiny {len(idx['pet_shiny'])}")


if __name__ == "__main__":
    want = set(sys.argv[1:]) or None
    main(want)
