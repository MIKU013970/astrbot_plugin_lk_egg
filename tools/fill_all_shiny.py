# -*- coding: utf-8 -*-
"""把所有精灵的「异色图 / 异色蛋」都补一遍（按 wiki 页面），并顺手校正赛季。

对 ranges 里的每只精灵：
  1. 读 wiki 页面（有缓存用缓存）
  2. 取 data-nrc-pets-ready（赛季）/ pet 编号 / PetPortrait(yise) / Item 蛋 / Item 15xxxx 异色蛋
  3. 缺图就下载 → 写 index.json；赛季更新进 seasons.json
只补「还没有的」，不会覆盖已有的。
"""
from __future__ import annotations

import io
import json
import re
import time
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
CACHE = V4 / "_lk_verify" / "cache"
RANGES = PLUGIN / "ranges.json"
SEASONS = PLUGIN / "seasons.json"


def http(url: str, timeout: int = 40, retry: int = 2):
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": WIKI})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception:
            if i == retry - 1:
                return None
            time.sleep(1.5)
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
    CACHE.mkdir(parents=True, exist_ok=True)
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


def parse(h: str) -> dict:
    files = []
    for f in (urllib.parse.unquote(x) for x in re.findall(r"文件:([^\"'&]+?\.png)", h)):
        if f not in files:
            files.append(f)
    num = re.search(r'data-sd-p-pet="pet_(\d+)"', h)
    season = re.search(r'data-nrc-pets-ready="([^"]*)"', h)
    return {
        "tag": f"nrc{int(num.group(1))}" if num else "",
        "season": (season.group(1) or "").upper() if season else "",
        "pet": next((f for f in files if f.startswith("PetPortrait") and "yise" not in f), ""),
        "shiny": next((f for f in files if f.startswith("PetPortrait") and "yise" in f), ""),
        "egg": next((f for f in files if re.fullmatch(r"Item (107|108|149)\d{3}\.png", f)), ""),
        "egg_shiny": next((f for f in files if re.fullmatch(r"Item 15\d{4}\.png", f)), ""),
    }


def save_pet(raw: bytes, dst: Path, size: int = 96) -> bool:
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
        im.thumbnail((size, size), Image.LANCZOS)
        c = Image.new("RGBA", (size, size), (0, 0, 0, 0))
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


def main():
    doc = json.loads(RANGES.read_text(encoding="utf-8"))
    names = [str(r.get("精灵")) for r in doc["species"]
             if r.get("精灵") and not str(r.get("精灵")).endswith("的蛋")]
    print(f"处理 {len(names)} 只", flush=True)

    idx_fp = ICONS / "index.json"
    idx = json.loads(idx_fp.read_text(encoding="utf-8"))
    for k in ("pet", "egg", "pet_shiny", "pet_stage1"):
        idx.setdefault(k, {})
    sd = json.loads(SEASONS.read_text(encoding="utf-8")) if SEASONS.is_file() else \
        {"season": {}, "detail": {}}
    sd.setdefault("season", {})

    lock_added = {"shiny": [], "shiny_egg": [], "pet": [], "egg": [], "season": []}

    def work(nm):
        h = page(nm)
        if not h:
            return nm, None
        info = parse(h)
        if not info["tag"]:
            return nm, None
        tag = info["tag"]
        got = []
        if info["pet"] and nm not in idx["pet"]:
            raw = http(img_url(info["pet"]))
            if raw and save_pet(raw, ICONS / "pet" / f"{tag}.png"):
                idx["pet"][nm] = tag
                idx["pet_stage1"].setdefault(nm, tag)
                got.append("pet")
        if info["shiny"] and nm not in idx["pet_shiny"]:
            raw = http(img_url(info["shiny"]))
            if raw and save_pet(raw, ICONS / "shiny" / f"{tag}.png"):
                idx["pet_shiny"][nm] = tag
                got.append("shiny")
        for key, egg_key, label in ((info["egg"], f"{nm}的蛋", "egg"),
                                    (info["egg_shiny"], f"异色{nm}的蛋", "shiny_egg")):
            if not key or egg_key in idx["egg"]:
                continue
            m = re.match(r"Item (\d+)\.png", key)
            if not m:
                continue
            stem = f"nrc-item_Item_{m.group(1)}"
            raw = http(img_url(key))
            if raw and save_raw(raw, ICONS / "egg" / f"{stem}.png"):
                idx["egg"][egg_key] = stem
                got.append(label)
        if info["season"] and info["season"].startswith("S"):
            old = sd["season"].get(nm)
            if old != info["season"]:
                sd["season"][nm] = info["season"]
                sd.setdefault("detail", {}).setdefault(nm, {})["season"] = info["season"]
                got.append(f"season:{old}->{info['season']}")
        return nm, got

    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for nm, got in ex.map(work, names):
            done += 1
            if got:
                for g in got:
                    key = g.split(":")[0]
                    lock_added.setdefault(key, []).append(nm)
                print(f"  [{done}/{len(names)}] {nm:<10} +{', '.join(got)}", flush=True)
            elif done % 25 == 0:
                print(f"  [{done}/{len(names)}] …", flush=True)

    idx_fp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    SEASONS.write_text(json.dumps(sd, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n=== 补充汇总 ===")
    for k, v in lock_added.items():
        if v:
            print(f"  {k}: {len(v)} 只 → {v[:20]}")
    print(f"index: pet {len(idx['pet'])} · egg {len(idx['egg'])} · shiny {len(idx['pet_shiny'])}")


if __name__ == "__main__":
    main()
