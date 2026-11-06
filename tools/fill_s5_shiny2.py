# -*- coding: utf-8 -*-
"""把新赛季所有精灵的「异色立绘 / 异色蛋」用**全文扫描**再补一遍（上次只扫了卡片前 2500 字）。"""
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
ICONS = V4 / "astrbot_plugin_lk_egg" / "icons"
CACHE = V4 / "_lk_verify" / "cache"

S5 = ["小剪影", "影子剪纸", "西瓜虫", "冬虫夏瓜", "豆粒鹅", "刨雪鹅", "鲸羽", "圆环鲸羽",
      "蟹蟹椰", "滴溜豚", "乐乐鲨", "蛋包蛋", "鸡麻麻", "小冥龙", "地银链", "沉金兽",
      "长江豚", "云梦豚", "加灵", "小帕尔", "绒绒", "犀角鸟", "小雪人", "爆焰仔",
      "咬咬小子", "足尖元件"]


def http(url: str, timeout: int = 30):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": WIKI})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def page(name: str, force: bool = False) -> str:
    fp = CACHE / f"nrc_{name}.html"
    if fp.is_file() and not force and fp.stat().st_size > 10000:
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


def save_pet(raw: bytes, dst: Path, size: int = 96) -> bool:
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
        im.thumbnail((size, size), Image.LANCZOS)
        c = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        c.paste(im, ((size - im.width) // 2, (size - im.height) // 2), im)
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

changed = 0
for nm in S5:
    h = page(nm, force=True)
    if not h:
        print(f"  {nm:<8} 页面抓不到")
        continue
    files = []
    for f in (urllib.parse.unquote(x) for x in re.findall(r"文件:([^\"'&]+?\.png)", h)):
        if f not in files:
            files.append(f)
    shiny = next((f for f in files if f.startswith("PetPortrait") and "yise" in f), "")
    egg = next((f for f in files if re.fullmatch(r"Item (107|108|149)\d{3}\.png", f)), "")
    egg_shiny = next((f for f in files if re.fullmatch(r"Item 15\d{4}\.png", f)), "")
    num = re.search(r'data-sd-p-pet="pet_(\d+)"', h)
    tag = f"nrc{int(num.group(1))}" if num else ""
    got = []
    if shiny and tag and nm not in idx["pet_shiny"]:
        raw = http(img_url(shiny))
        if raw and save_pet(raw, ICONS / "shiny" / f"{tag}.png"):
            idx["pet_shiny"][nm] = tag
            got.append(f"异色图→{tag}")
    if egg:
        m = re.match(r"Item (\d+)\.png", egg)
        stem = f"nrc-item_Item_{m.group(1)}" if m else ""
        if stem and f"{nm}的蛋" not in idx["egg"]:
            raw = http(img_url(egg))
            if raw and save_raw(raw, ICONS / "egg" / f"{stem}.png"):
                idx["egg"][f"{nm}的蛋"] = stem
                got.append(f"蛋→{stem}")
    if egg_shiny:
        m = re.match(r"Item (\d+)\.png", egg_shiny)
        stem = f"nrc-item_Item_{m.group(1)}" if m else ""
        if stem and f"异色{nm}的蛋" not in idx["egg"]:
            raw = http(img_url(egg_shiny))
            if raw and save_raw(raw, ICONS / "egg" / f"{stem}.png"):
                idx["egg"][f"异色{nm}的蛋"] = stem
                got.append(f"异色蛋→{stem}")
    if got:
        changed += 1
        print(f"  {nm:<8} +{', '.join(got)}")
    else:
        print(f"  {nm:<8} （无新增）异色立绘={shiny or '无'} 蛋={egg or '无'} 异色蛋={egg_shiny or '无'}")

idx_fp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\n补了 {changed} 只 · index: pet {len(idx['pet'])} · egg {len(idx['egg'])} · "
      f"shiny {len(idx['pet_shiny'])}")
