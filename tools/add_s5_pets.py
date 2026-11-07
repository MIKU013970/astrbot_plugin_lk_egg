# -*- coding: utf-8 -*-
"""花怨鳗 / 锤头鹳：wiki 页面 → 图标 / 精灵蛋 / 异色蛋 + 编号/蛋组/本体尺寸。

同时把它们补进 ranges.json（如果还没有）。
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
RANGES = PLUGIN / "ranges.json"

NAMES = ["花怨鳗", "锤头鹳"]


def http(url: str, timeout: int = 30):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": WIKI})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def page(name: str) -> str:
    fp = CACHE / f"nrc_{name}.html"
    raw = http(WIKI + urllib.parse.quote(name))
    if not raw:
        if fp.is_file():
            return fp.read_text(encoding="utf-8", errors="replace")
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


def to_text(h: str) -> str:
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</(div|p|tr|td|th|li|h[1-6]|table|section)>", "\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


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


def main():
    idx_fp = ICONS / "index.json"
    idx = json.loads(idx_fp.read_text(encoding="utf-8"))
    for k in ("pet", "egg", "pet_shiny", "pet_stage1"):
        idx.setdefault(k, {})
    doc = json.loads(RANGES.read_text(encoding="utf-8"))
    by = {str(x.get("精灵")): x for x in doc["species"]}

    for nm in NAMES:
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
        season = re.search(r'data-nrc-pets-ready="([^"]*)"', h)
        codex = re.search(r'data-nrc-codex="([^"]*)"', h)
        t = to_text(h)
        egg_group = re.search(r"蛋组\s*\n?\s*([\u4e00-\u9fa5]+组(?:\s*/\s*[\u4e00-\u9fa5]+组)*)", t)
        height = re.search(r"身高\s*\n?\s*([\d.]+)\s*[~～]\s*([\d.]+)\s*([Mm])", t)
        weight = re.search(r"体重\s*\n?\s*([\d.]+)\s*[~～]\s*([\d.]+)\s*[Kk][Gg]", t)
        dex = re.search(r'sd-p-number"[^>]*>\s*(\d{1,3})\s*<', h)

        print(f"\n=== {nm} · tag={tag} · 赛季={season.group(1) if season else '?'} "
              f"codex={codex.group(1) if codex else '?'}")
        print(f"    编号={dex.group(1) if dex else '?'} 蛋组={egg_group.group(1) if egg_group else '?'} "
              f"身高={height.group(1) + '~' + height.group(2) if height else '?'} "
              f"体重={weight.group(1) + '~' + weight.group(2) if weight else '?'}")
        print(f"    文件: {files[:8]}")

        pet_f = next((f for f in files if f.startswith("PetPortrait") and "yise" not in f), "")
        shiny_f = next((f for f in files if f.startswith("PetPortrait") and "yise" in f), "")
        egg_f = next((f for f in files if re.fullmatch(r"Item (107|108|149)\d{3}\.png", f)), "")
        egg_shiny_f = next((f for f in files if re.fullmatch(r"Item 15\d{4}\.png", f)), "")

        got = []
        if pet_f and tag:
            raw = http(img_url(pet_f))
            if raw and save_pet(raw, ICONS / "pet" / f"{tag}.png"):
                idx["pet"][nm] = tag
                idx["pet_stage1"].setdefault(nm, tag)
                got.append(f"pet/{tag}")
        if shiny_f and tag:
            raw = http(img_url(shiny_f))
            if raw and save_pet(raw, ICONS / "shiny" / f"{tag}.png"):
                idx["pet_shiny"][nm] = tag
                got.append(f"shiny/{tag}")
        for f, key, label in ((egg_f, f"{nm}的蛋", "蛋"),
                              (egg_shiny_f, f"异色{nm}的蛋", "异色蛋")):
            if not f:
                continue
            m = re.match(r"Item (\d+)\.png", f)
            if not m:
                continue
            stem = f"nrc-item_Item_{m.group(1)}"
            raw = http(img_url(f))
            if raw and save_raw(raw, ICONS / "egg" / f"{stem}.png"):
                idx["egg"][key] = stem
                idx["egg"][f"{nm}蛋"] = stem if label == "蛋" else idx["egg"].get(f"{nm}蛋")
                got.append(f"{label}={stem}")
        print(f"    抓到: {got or '无'}")

        # ranges 里没有就补一条
        if nm not in by:
            rec = {
                "精灵": nm, "蛋名": f"{nm}的蛋",
                "图鉴编号": dex.group(1).zfill(3) if dex else "",
                "蛋组": egg_group.group(1) if egg_group else "",
                "公母": "雄性 50% / 雌性 50%",
                "进化链": nm, "形态数": 1, "同乘": False,
                "分类": f"新赛季（{'S' + season.group(1).upper().replace('S', '') if season else 'S5'}）",
                "variants": [{
                    "形态": nm, "形态简称": "", "蛋名": f"{nm}的蛋",
                    "图鉴编号": dex.group(1).zfill(3) if dex else "",
                    "身高区间": "", "体重区间": "",
                    "公母": "雄性 50% / 雌性 50%",
                    "数据来源": "wiki（/nrc 页面）",
                    "系别": "",
                }],
            }
            if height:
                rec["variants"][0]["精灵身高"] = f"{height.group(1)}~{height.group(2)} cm"
            if weight:
                rec["variants"][0]["精灵体重"] = f"{weight.group(1)}~{weight.group(2)} kg"
            doc["species"].append(rec)
            by[nm] = rec
            print(f"    ranges 里补了一条：编号 {rec['图鉴编号']} 蛋组 {rec['蛋组']}")

    idx_fp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    RANGES.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nindex: pet {len(idx['pet'])} · egg {len(idx['egg'])} · shiny {len(idx['pet_shiny'])}")
    print(f"ranges species: {len(doc['species'])}")


if __name__ == "__main__":
    main()
