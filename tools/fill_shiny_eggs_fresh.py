# -*- coding: utf-8 -*-
"""这些「有异色图但没异色蛋」的精灵，重新**不用缓存**抓一遍，把 Item 15xxxx 全找出来。

用户指出：伊雷龙的异色蛋是 文件:Item 150411.png（wiki 上确实有）。
"""
from __future__ import annotations

import io
import json
import re
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
CACHE = V4 / "_lk_verify" / "cache"

TARGETS = ["伊雷龙", "可爱猿", "吸泥鸥", "小翼龙", "深蓝鲸", "火红尾",
           "长江豚", "云梦豚", "加灵", "小冥龙", "缇塔"]


def http(url: str, timeout: int = 40):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": WIKI})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def fresh_page(name: str) -> str:
    raw = http(WIKI + urllib.parse.quote(name))
    if not raw:
        return ""
    h = raw.decode("utf-8", "replace")
    if "请求已被" in h:
        return ""
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / f"nrc_{name}.html").write_text(h, encoding="utf-8")
    return h


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


def save_raw(raw: bytes, dst: Path) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(raw)
    return dst.stat().st_size > 200


idx_fp = ICONS / "index.json"
idx = json.loads(idx_fp.read_text(encoding="utf-8"))
for k in ("pet", "egg", "pet_shiny", "pet_stage1"):
    idx.setdefault(k, {})

for nm in TARGETS:
    h = fresh_page(nm)
    if not h:
        print(f"✗ {nm}: 页面抓不到")
        continue
    files = []
    for f in (urllib.parse.unquote(x) for x in re.findall(r"文件:([^\"'&]+?\.png)", h)):
        if f not in files:
            files.append(f)
    shiny_eggs = [f for f in files if re.fullmatch(r"Item 15\d{4}\.png", f)]
    eggs = [f for f in files if re.fullmatch(r"Item (107|108|149)\d{3}\.png", f)]
    shiny_portrait = [f for f in files if f.startswith("PetPortrait") and "yise" in f]
    num = re.search(r'data-sd-p-pet="pet_(\d+)"', h)
    tag = f"nrc{int(num.group(1))}" if num else ""

    got = []
    # 异色立绘
    if shiny_portrait and tag and nm not in idx["pet_shiny"]:
        raw = http(img_url(shiny_portrait[0]))
        if raw and save_pet(raw, ICONS / "shiny" / f"{tag}.png"):
            idx["pet_shiny"][nm] = tag
            got.append(f"异色图={tag}")
    # 普通蛋
    if eggs and f"{nm}的蛋" not in idx["egg"]:
        m = re.match(r"Item (\d+)\.png", eggs[0])
        if m:
            stem = f"nrc-item_Item_{m.group(1)}"
            raw = http(img_url(eggs[0]))
            if raw and save_raw(raw, ICONS / "egg" / f"{stem}.png"):
                idx["egg"][f"{nm}的蛋"] = stem
                got.append(f"蛋图={stem}")
    # 异色蛋（可能有多张，全登记，主图取第一张）
    for i, f in enumerate(shiny_eggs):
        key = f"异色{nm}的蛋" if i == 0 else f"异色{nm}的蛋#{i+1}"
        if key in idx["egg"]:
            continue
        m = re.match(r"Item (\d+)\.png", f)
        if not m:
            continue
        stem = f"nrc-item_Item_{m.group(1)}"
        raw = http(img_url(f))
        if raw and save_raw(raw, ICONS / "egg" / f"{stem}.png"):
            idx["egg"][key] = stem
            got.append(f"异色蛋图={stem}")

    print(f"{'✓' if got else '·'} {nm:<8} 文件{len(files)} 立绘异色={shiny_portrait[:1] or '无'} "
          f"蛋={eggs[:2]} 异色蛋={shiny_eggs[:3]} → {', '.join(got) or '无新增'}")

idx_fp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\nindex: egg {len(idx['egg'])} · shiny {len(idx['pet_shiny'])}")
