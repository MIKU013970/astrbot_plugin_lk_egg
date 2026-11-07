# -*- coding: utf-8 -*-
"""专门补 S5（含常驻）里「有异色图但没异色蛋」的那些：异色蛋图 + 一阶段 + 蛋图。"""
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

TARGETS = ["柴渣虫", "守夜烛", "猴麦仔", "幽影树", "贝瑟", "嘟嘟煲", "炫光迪迪",
           "小独角兽", "火红尾", "稻草人", "月牙雪熊", "恶魔狼", "立方人", "大耳帽兜",
           "小冥龙", "加灵", "长江豚", "云梦豚"]


def http(url: str, timeout: int = 40):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": WIKI})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def page(name: str) -> str:
    fp = CACHE / f"nrc_{name}.html"
    if fp.is_file() and fp.stat().st_size > 10000:
        return fp.read_text(encoding="utf-8", errors="replace")
    raw = http(WIKI + urllib.parse.quote(name))
    if not raw:
        return ""
    h = raw.decode("utf-8", "replace")
    if "请求已被" in h:
        return ""
    fp.write_text(h, encoding="utf-8")
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
doc = json.loads((PLUGIN / "ranges.json").read_text(encoding="utf-8"))
chain = {}
for rec in doc["species"]:
    parts = [x.strip() for x in str(rec.get("进化链") or "").replace("→", "->").split("->")
             if x.strip()]
    if len(parts) >= 2:
        for p in parts[1:]:
            chain.setdefault(p, parts[0])

for nm in TARGETS:
    h = page(nm)
    if not h:
        print(f"✗ {nm}: 页面抓不到")
        continue
    files = []
    for f in (urllib.parse.unquote(x) for x in re.findall(r"文件:([^\"'&]+?\.png)", h)):
        if f not in files:
            files.append(f)
    num = re.search(r'data-sd-p-pet="pet_(\d+)"', h)
    tag = f"nrc{int(num.group(1))}" if num else ""
    shiny = next((f for f in files if f.startswith("PetPortrait") and "yise" in f), "")
    egg = next((f for f in files if re.fullmatch(r"Item (107|108|149)\d{3}\.png", f)), "")
    egg_shiny = next((f for f in files if re.fullmatch(r"Item 15\d{4}\.png", f)), "")
    got = []

    if shiny and tag and nm not in idx["pet_shiny"]:
        raw = http(img_url(shiny))
        if raw and save_pet(raw, ICONS / "shiny" / f"{tag}.png"):
            idx["pet_shiny"][nm] = tag
            got.append(f"异色图={tag}")
    if egg and f"{nm}的蛋" not in idx["egg"]:
        m = re.match(r"Item (\d+)\.png", egg)
        if m:
            stem = f"nrc-item_Item_{m.group(1)}"
            raw = http(img_url(egg))
            if raw and save_raw(raw, ICONS / "egg" / f"{stem}.png"):
                idx["egg"][f"{nm}的蛋"] = stem
                got.append(f"蛋图={stem}")
    if egg_shiny and f"异色{nm}的蛋" not in idx["egg"]:
        m = re.match(r"Item (\d+)\.png", egg_shiny)
        if m:
            stem = f"nrc-item_Item_{m.group(1)}"
            raw = http(img_url(egg_shiny))
            if raw and save_raw(raw, ICONS / "egg" / f"{stem}.png"):
                idx["egg"][f"异色{nm}的蛋"] = stem
                got.append(f"异色蛋图={stem}")

    # 一阶段
    base = chain.get(nm, "")
    if base:
        t = idx["pet"].get(base) or idx["pet_stage1"].get(base)
        if t and idx["pet_stage1"].get(nm) != t:
            idx["pet_stage1"][nm] = t
            got.append(f"一阶段={t}")
        s = idx["pet_shiny"].get(base)
        if s and nm not in idx["pet_shiny"]:
            idx["pet_shiny"][nm] = s
            got.append(f"异色(承一阶段)={s}")

    print(f"{'✓' if got else '·'} {nm:<8} 文件{len(files)} 异色立绘={'有' if shiny else '无'} "
          f"蛋={'有' if egg else '无'} 异色蛋={'有' if egg_shiny else '无'} → {', '.join(got) or '无新增'}")

idx_fp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\nindex: pet {len(idx['pet'])} · egg {len(idx['egg'])} · shiny {len(idx['pet_shiny'])} "
      f"· stage1 {len(idx['pet_stage1'])}")
