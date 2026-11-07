# -*- coding: utf-8 -*-
"""全量补：所有「还没有异色蛋图」的精灵，**不用缓存**重抓页面，把 Item 15xxxx 找出来。

上次就是因为用了过期/不完整的缓存页面，把 伊雷龙 的异色蛋（Item 150411）漏了。
"""
from __future__ import annotations

import io
import json
import re
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
CACHE = V4 / "_lk_verify" / "cache"
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


def main():
    idx_fp = ICONS / "index.json"
    idx = json.loads(idx_fp.read_text(encoding="utf-8"))
    for k in ("pet", "egg", "pet_shiny", "pet_stage1"):
        idx.setdefault(k, {})
    doc = json.loads((PLUGIN / "ranges.json").read_text(encoding="utf-8"))
    names = [str(r.get("精灵")) for r in doc["species"]
             if r.get("精灵") and not str(r.get("精灵")).endswith("的蛋")]
    todo = [n for n in names if f"异色{n}的蛋" not in idx["egg"]]
    print(f"待补异色蛋 {len(todo)} 只", flush=True)

    added = []
    missing = []

    def work(nm):
        raw = http(WIKI + urllib.parse.quote(nm))
        if not raw:
            return nm, "抓不到", None
        h = raw.decode("utf-8", "replace")
        if "请求已被" in h:
            return nm, "WAF", None
        files = []
        for f in (urllib.parse.unquote(x) for x in re.findall(r"文件:([^\"'&]+?\.png)", h)):
            if f not in files:
                files.append(f)
        shiny_eggs = [f for f in files if re.fullmatch(r"Item 15\d{4}\.png", f)]
        shiny_portrait = [f for f in files if f.startswith("PetPortrait") and "yise" in f]
        num = re.search(r'data-sd-p-pet="pet_(\d+)"', h)
        tag = f"nrc{int(num.group(1))}" if num else ""
        got = []
        if shiny_portrait and tag and nm not in idx["pet_shiny"]:
            data = http(img_url(shiny_portrait[0]))
            if data and save_pet(data, ICONS / "shiny" / f"{tag}.png"):
                with _lock:
                    idx["pet_shiny"][nm] = tag
                got.append(f"异色图={tag}")
        if shiny_eggs:
            f = shiny_eggs[0]
            m = re.match(r"Item (\d+)\.png", f)
            if m:
                stem = f"nrc-item_Item_{m.group(1)}"
                data = http(img_url(f))
                if data and save_raw(data, ICONS / "egg" / f"{stem}.png"):
                    with _lock:
                        idx["egg"][f"异色{nm}的蛋"] = stem
                    got.append(f"异色蛋={stem}")
        return nm, ", ".join(got) if got else "无", files

    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for nm, res, files in ex.map(work, todo):
            done += 1
            if res not in ("无", "抓不到", "WAF"):
                added.append((nm, res))
                print(f"  [{done}/{len(todo)}] ✓ {nm:<10} +{res}", flush=True)
            elif res == "无":
                missing.append(nm)
            if done % 25 == 0:
                print(f"  [{done}/{len(todo)}] …", flush=True)

    idx_fp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    with (V4 / "_shiny_egg_missing.txt").open("w", encoding="utf-8") as f:
        f.write("确实没有异色蛋的：\n" + "、".join(missing))
    print(f"\n补上 {len(added)} 只：{[a[0] for a in added]}")
    print(f"确实没有 {len(missing)} 只（写进 _shiny_egg_missing.txt）")
    print(f"index egg 总数 {len(idx['egg'])} · pet_shiny {len(idx['pet_shiny'])}")


if __name__ == "__main__":
    main()
