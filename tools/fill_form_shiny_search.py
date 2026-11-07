# -*- coding: utf-8 -*-
"""多形态精灵的形态页只能靠搜索找到（主页面直接 404）：

  月亮砣           → 404
  月亮砣（上弦的样子） → 200 ✅

所以先用 MediaWiki 搜索接口列出「以名字开头」的所有页面，再逐页扫异色蛋 / 异色立绘。
"""
from __future__ import annotations

import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/144.0.0.0 Safari/537.36")
WIKI = "https://wiki.biligame.com/nrc/"
API = WIKI + "api.php"
V4 = Path(r"C:\Users\MIKU\Desktop\v4")
PLUGIN = V4 / "astrbot_plugin_lk_egg"
ICONS = PLUGIN / "icons"

TARGETS = sys.argv[1:] or ["加油蟹", "伊里斯", "阿瓦鲨", "月亮砣", "满月砣",
                           "遁鼠", "遁地鼠", "地鼠", "刺轮砣"]


def api_json(params: dict):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(API + "?" + q, headers={"User-Agent": UA, "Referer": WIKI})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            time.sleep(1.2)
    return {}


def search_titles(name: str):
    d = api_json({"action": "query", "list": "search", "srsearch": name,
                  "srlimit": 30, "format": "json", "formatversion": "2"})
    hits = [x.get("title", "") for x in ((d.get("query") or {}).get("search") or [])]
    return [t for t in hits if t.replace(" ", "").startswith(name)]


def get_page(title: str) -> str:
    try:
        req = urllib.request.Request(WIKI + urllib.parse.quote(title),
                                     headers={"User-Agent": UA, "Referer": WIKI})
        with urllib.request.urlopen(req, timeout=35) as r:
            h = r.read().decode("utf-8", "replace")
        return "" if "请求已被" in h else h
    except Exception:
        return ""


def img_url(title: str) -> str:
    d = api_json({"action": "query", "titles": "文件:" + title, "prop": "imageinfo",
                  "iiprop": "url", "format": "json", "formatversion": "2"})
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


def scan(title: str):
    h = get_page(title)
    if not h:
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


idx_fp = ICONS / "index.json"
idx = json.loads(idx_fp.read_text(encoding="utf-8"))
for k in ("pet", "egg", "pet_shiny", "pet_stage1"):
    idx.setdefault(k, {})

for nm in TARGETS:
    titles = search_titles(nm)
    if not titles:
        print(f"\n✗ {nm}: 搜索不到页面")
        continue
    print(f"\n=== {nm} · 找到 {len(titles)} 个页面 ===")
    print("   ", titles[:8])
    got = {}
    for t in titles[:8]:
        info = scan(t)
        if not info:
            continue
        flags = []
        if info["egg_shiny"]:
            flags.append(f"异色蛋={info['egg_shiny']}")
        if info["egg"]:
            flags.append(f"蛋={info['egg']}")
        if info["shiny_portrait"]:
            flags.append("异色立绘")
        print(f"   {t:<24} {' · '.join(flags) or '无'}")
        if "shiny" not in got and info["shiny_portrait"] and info["tag"]:
            data = http_img = img_url(info["shiny_portrait"])
            if data:
                raw = urllib.request.urlopen(urllib.request.Request(
                    data, headers={"User-Agent": UA}), timeout=30).read()
                if save_pet(raw, ICONS / "shiny" / f"{info['tag']}.png"):
                    got["shiny"] = info["tag"]
        if "egg" not in got and info["egg_shiny"]:
            m = re.match(r"Item (\d+)\.png", info["egg_shiny"])
            if m:
                stem = f"nrc-item_Item_{m.group(1)}"
                url = img_url(info["egg_shiny"])
                if url:
                    raw = urllib.request.urlopen(urllib.request.Request(
                        url, headers={"User-Agent": UA}), timeout=30).read()
                    if save_raw(raw, ICONS / "egg" / f"{stem}.png"):
                        got["egg"] = stem
    if got:
        if "shiny" in got:
            idx["pet_shiny"][nm] = got["shiny"]
        if "egg" in got:
            idx["egg"][f"异色{nm}的蛋"] = got["egg"]
        print(f"  ✓ 补上 {got}")
    else:
        print("  · 形态页里也没有异色蛋/异色立绘")

idx_fp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\nindex egg {len(idx['egg'])} · shiny {len(idx['pet_shiny'])}")
