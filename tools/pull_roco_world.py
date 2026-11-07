# -*- coding: utf-8 -*-
"""从 roco.world 抓精灵图鉴（496 只 + 形态页），补进插件。

roco.world 是另一个《洛克王国：世界》wiki，收录比 biligame 全（有超能阿布这类图鉴页），
字段：名字、图鉴号、属性、蛋组、身高、体重、进化链、形态。

产出 `roco_pets.json`：
```json
{"updated":"...","count":496,
 "pets":{"铃兰夫人":{"id":"496","dex":"496","types":["萌","草"],
        "groups":["植物组"],"height":"1.75 - 2.6","weight":"16.4 - 22.6",
        "bins":132,"chain":["呆呆铃","跳跳铃","铃兰夫人"]}}}
```
用法：python tools/pull_roco_world.py            # 全量（带缓存，已抓的跳过）
      python tools/pull_roco_world.py --limit 20  # 先试 20 只
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
V4 = PLUGIN.parent
CACHE = V4 / "_lk_verify" / "roco_cache"
OUT = PLUGIN / "roco_pets.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
SLEEP = 0.35


def get(url: str, force: bool = False) -> str:
    key = re.sub(r"[^\w]+", "_", url.replace("https://roco.world/zh/jini/", "jini_"))
    fp = CACHE / (key + ".html")
    if not force and fp.is_file() and fp.stat().st_size > 5000:
        return fp.read_text(encoding="utf-8", errors="replace")
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                h = r.read().decode("utf-8", "replace")
            time.sleep(SLEEP)
            CACHE.mkdir(parents=True, exist_ok=True)
            fp.write_text(h, encoding="utf-8")
            return h
        except Exception as e:
            if i == 2:
                print(f"  ! {url} {type(e).__name__}: {e}")
            time.sleep(1.0)
    return ""


def text_of(html: str) -> str:
    body = re.sub(r"<script[\s\S]*?</script>", " ", html)
    body = re.sub(r"<style[\s\S]*?</style>", " ", body)
    body = re.sub(r"<svg[\s\S]*?</svg>", " ", body)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", body)


def parse(html: str) -> dict:
    out: dict = {}
    m = re.search(r"<title>([^<（(]+)[（(]#?(\d+)[)）]", html)
    if m:
        out["name"] = m.group(1).strip()
        out["dex"] = m.group(2)
    desc = re.search(r'name="description" content="([^"]+)"', html)
    if desc:
        d = desc.group(1)
        t = re.search(r"的\s*([^，。]*?)属性", d)
        if t:
            out["types"] = [x for x in re.split(r"[/、 ]+", t.group(1)) if x]
    tx = text_of(html)
    # 蛋组：页面上是「植物蛋组」
    g = re.findall(r"([\u4e00-\u9fa5]{2,4})蛋组", tx)
    if g:
        out["groups"] = [x + "组" for x in dict.fromkeys(g)]
    # 身高 / 体重（页面上是「身高 1.75 - 2.6米」「体重 16.4 - 22.6kg」这种）
    hm = re.search(r"身高\s*([\d.]+)\s*[-~]\s*([\d.]+)", tx)
    wm = re.search(r"体重\s*([\d.]+)\s*[-~]\s*([\d.]+)", tx)
    if hm:
        out["height"] = f"{hm.group(1)} - {hm.group(2)}"
    if wm:
        out["weight"] = f"{wm.group(1)} - {wm.group(2)}"
        try:
            span = float(wm.group(2)) - float(wm.group(1))
            out["bins"] = int(round(span / 0.05)) + 1
        except (TypeError, ValueError):
            pass
    # 进化链：页面里 形态/进化 区块的精灵名链接
    chain = re.findall(r'/zh/jini/\d+"[^>]*>([^<]{1,12})<', html)
    if chain:
        out["chain"] = list(dict.fromkeys(x.strip() for x in chain if x.strip()))[:6]
    return out


def main() -> int:
    limit = 0
    for a in sys.argv[1:]:
        if a.startswith("--limit"):
            limit = int(a.split("=")[-1]) if "=" in a else 20
    sm = get("https://roco.world/sitemap-zh-hans-jini.xml")
    ids = sorted({int(x) for x in re.findall(r"/zh/jini/(\d+)/?<", sm) or re.findall(r"/zh/jini/(\d+)<", sm)})
    if not ids:
        ids = sorted({int(x) for x in re.findall(r"/zh/jini/(\d+)", sm)})
    print(f"sitemap 里 {len(ids)} 只（{ids[0]}~{ids[-1]}）")
    if limit:
        ids = ids[:limit]

    pets: dict = {}
    for i, pid in enumerate(ids, 1):
        h = get(f"https://roco.world/zh/jini/{pid}")
        if not h:
            continue
        d = parse(h)
        nm = d.get("name")
        if not nm:
            continue
        d["id"] = str(pid)
        pets[nm] = d
        if i % 50 == 0:
            print(f"  … {i}/{len(ids)} 已解析 {len(pets)}")

    OUT.write_text(json.dumps({
        "updated": time.strftime("%Y-%m-%d"),
        "source": "https://roco.world/zh/jini (sitemap-zh-hans-jini.xml)",
        "count": len(pets),
        "pets": dict(sorted(pets.items())),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {OUT.name}: {len(pets)} 只 · {OUT.stat().st_size / 1024:.0f} KB")
    for kw in ("超能阿布", "呆呆铃", "铃兰夫人", "伊里斯"):
        print(f"   {kw}: {json.dumps(pets.get(kw), ensure_ascii=False)[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
