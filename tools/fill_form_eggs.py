# -*- coding: utf-8 -*-
"""按「形态」分别登记异色蛋 / 精灵蛋（多形态精灵每个形态可能各有一颗蛋）。

例：
  地鼠（枯水期的样子）→ 蛋 Item 107012 / 异色蛋 Item 150794
  地鼠（储水时的样子）→ 蛋 Item 107170 / 异色蛋 Item 150811
  → index: "异色地鼠的蛋|枯水期的样子" = nrc-item_Item_150794
           "异色地鼠的蛋|储水时的样子" = nrc-item_Item_150811
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/144.0.0.0 Safari/537.36")
WIKI = "https://wiki.biligame.com/nrc/"
API = WIKI + "api.php"
V4 = Path(r"C:\Users\MIKU\Desktop\v4")
PLUGIN = V4 / "astrbot_plugin_lk_egg"
ICONS = PLUGIN / "icons"

TARGETS = ["地鼠", "遁鼠", "遁地鼠", "刺轮砣", "月亮砣", "满月砣", "加油蟹", "阿瓦鲨",
           "旋叶虫", "梦游", "晶石蜗", "波波螺"]


def api_json(params: dict):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(API + "?" + q, headers={"User-Agent": UA, "Referer": WIKI})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            time.sleep(1.0)
    return {}


def search(name: str):
    d = api_json({"action": "query", "list": "search", "srsearch": name,
                  "srlimit": 30, "format": "json", "formatversion": "2"})
    hits = [x.get("title", "") for x in ((d.get("query") or {}).get("search") or [])]
    return [t for t in hits
            if t.replace(" ", "").startswith(name) and "（" in t and "物品" not in t and "大量" not in t]


def page(title: str):
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


def stem_of(item_png: str, prefix: str = "nrc-item_Item_") -> str:
    m = re.match(r"Item (\d+)\.png", item_png or "")
    return f"{prefix}{m.group(1)}" if m else ""


def ensure_file(stem: str, item_png: str) -> bool:
    dst = ICONS / "egg" / f"{stem}.png"
    if dst.is_file():
        return True
    url = img_url(item_png)
    if not url:
        return False
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": UA}), timeout=30) as r:
            raw = r.read()
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(raw)
        return dst.stat().st_size > 200
    except Exception:
        return False


idx_fp = ICONS / "index.json"
idx = json.loads(idx_fp.read_text(encoding="utf-8"))
idx.setdefault("egg", {})
# ⚠ 形态级蛋图要写进 egg_form（键是「精灵名|形态简称」），写进 egg 是查不到的
idx.setdefault("egg_form", {})
# 清掉之前误写进 egg 的「带竖线」键
bogus = [k for k in idx["egg"] if "|" in str(k)]
for k in bogus:
    idx["egg"].pop(k, None)
if bogus:
    print(f"清掉误写进 egg 的形态键 {len(bogus)} 条")

total = 0
for nm in TARGETS:
    pages_found = search(nm)
    if not pages_found:
        print(f"{nm}: 没找到形态页")
        continue
    print(f"\n=== {nm} ===")
    for t in pages_found:
        h = page(t)
        if not h:
            continue
        files = []
        for f in (urllib.parse.unquote(x) for x in re.findall(r"文件:([^\"'&]+?\.png)", h)):
            if f not in files:
                files.append(f)
        egg = next((f for f in files if re.fullmatch(r"Item (107|108|149)\d{3}\.png", f)), "")
        egg_shiny = next((f for f in files if re.fullmatch(r"Item 15\d{4}\.png", f)), "")
        m = re.match(rf"^{re.escape(nm)}（(.+)）$", t)
        form = m.group(1) if m else ""
        if not form:
            continue
        got = []
        if egg:
            stem = stem_of(egg)
            if stem and ensure_file(stem, egg):
                for key in (f"{nm}|{form}", f"{nm}蛋|{form}"):
                    idx["egg_form"][key] = stem
                got.append(f"蛋|{form}={stem}")
        if egg_shiny:
            stem = stem_of(egg_shiny)
            if stem and ensure_file(stem, egg_shiny):
                for key in (f"异色{nm}|{form}", f"异色{nm}的蛋|{form}",
                            f"异色{nm}蛋|{form}", f"{nm}的异色|{form}",
                            f"{nm}的异色蛋|{form}"):
                    idx["egg_form"][key] = stem
                got.append(f"异色蛋|{form}={stem}")
        if got:
            total += len(got)
            print(f"  {t:<26} " + " · ".join(got))

idx_fp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\negg_form 登记 {len(idx['egg_form'])} 条（本次 +{total}）")
print(f"index egg 总数 {len(idx['egg'])}")
