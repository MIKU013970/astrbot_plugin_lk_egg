# -*- coding: utf-8 -*-
"""从 wiki 抓每只精灵的**系别**，并顺手抓官方系别配色，生成 `elements.json`。

用法：
    python tools/pull_wiki_elements.py            # 只补缺的（有本地缓存就不再请求）
    python tools/pull_wiki_elements.py --force    # 全部重抓

产出 `astrbot_plugin_lk_egg/elements.json`：
```json
{
  "updated": "2026-11-07",
  "source": "https://wiki.biligame.com/nrc",
  "colors": {"龙系": {"tone": "#76475d", "accent": "#e64c62"}, ...},
  "pets":   {"小翼龙": ["龙系", "翼系"], ...},
  "chain":  {"刺轮砣": ["海洋系"], ...}       # 一阶段名 → 系别（兜底用）
}
```

页面结构（wiki 的精灵页）：
```html
<div class="sd-p-header-types">
  <span class="sd-type"><img alt="龙系" ...><span>龙系</span></span>
  <span class="sd-type"><img alt="翼系" ...><span>翼系</span></span>
</div>
```
配色（同一份 CSS，每页都有）：
```css
.sd-page[data-sd-element="龙系"] { --sd-class-tone:#76475d; --sd-class-accent:#e64c62; }
```
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
V4 = PLUGIN.parent
CACHE = V4 / "_lk_verify" / "cache"
OUT = PLUGIN / "elements.json"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
SLEEP = 0.35          # 别把 wiki 打疼


def fetch(name: str, force: bool = False) -> str:
    """取精灵页（优先本地缓存，<50 KB 的当失效）。"""
    fp = CACHE / f"nrc_{name}.html"
    if not force and fp.is_file() and fp.stat().st_size > 50_000:
        return fp.read_text(encoding="utf-8", errors="replace")
    url = "https://wiki.biligame.com/nrc/" + urllib.parse.quote(name)
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            h = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return ""            # 404：这个名字没有独立页面
    except Exception as e:
        print(f"  ! {name} 请求失败 {type(e).__name__}: {e}")
        return ""
    time.sleep(SLEEP)
    CACHE.mkdir(parents=True, exist_ok=True)
    fp.write_text(h, encoding="utf-8")
    return h


def body_of(h: str) -> str:
    body = h[h.find("<body"):] if "<body" in h else h
    body = re.sub(r"<style[\s\S]*?</style>", "", body)
    body = re.sub(r"<script[\s\S]*?</script>", "", body)
    return body


def parse_types(h: str) -> list:
    """精灵自己的系别（sd-p-header-types 块里的 sd-type）。"""
    body = body_of(h)
    m = re.search(r'class="sd-p-header-types"([\s\S]{0,3000}?)sd-p-', body)
    if not m:
        return []
    seg = m.group(1)
    out = []
    for _alt, txt in re.findall(
            r'<span class="sd-type"[^>]*>[\s\S]*?alt="([^"]+)"[\s\S]*?<span>([^<]*)</span>', seg):
        t = re.sub(r"\s+", "", txt or "")
        if t.endswith("系") and t not in out:
            out.append(t)
    return out


def wiki_search(name: str, limit: int = 4) -> list:
    """MediaWiki 搜索：多形态精灵的主页面是 404（图都在「名字（形态）」页上），
    所以先搜出真实页面标题，再逐页找带系别块的。"""
    url = ("https://wiki.biligame.com/nrc/api.php?action=query&list=search&format=json"
           "&srlimit=%d&srsearch=%s" % (limit, urllib.parse.quote(name)))
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return []
    titles = [x.get("title") or "" for x in
              (((d.get("query") or {}).get("search")) or [])]
    time.sleep(SLEEP)
    return [t for t in titles if name in t]


def parse_colors(h: str) -> dict:
    """官方系别配色：--sd-class-tone / --sd-class-accent。

    配色写在页面的 `<style>` 里（body_of() 会把 style 去掉，所以这里用原文扫）：
        .sd-page[data-sd-element="龙系"] { --sd-class-tone:#76475d; --sd-class-accent:#e64c62; }
    """
    out = {}
    for el, tone, accent in re.findall(
            r'data-sd-element="([^"]+)"\]\s*\{\s*--sd-class-tone:\s*(#[0-9a-fA-F]{6});\s*'
            r'--sd-class-accent:\s*(#[0-9a-fA-F]{6})', str(h)):
        el = el.strip()
        if el.endswith("系") and el not in out:
            out[el] = {"tone": tone.lower(), "accent": accent.lower()}
    return out


def main() -> int:
    force = "--force" in sys.argv
    ranges = json.loads((PLUGIN / "ranges.json").read_text(encoding="utf-8-sig"))
    roster = json.loads((PLUGIN / "season_roster.json").read_text(encoding="utf-8-sig"))

    # 1) 要查的名字：图鉴里的精灵 + 花名册里的名字（含二/三阶段、首领化）
    names = []
    for rec in (ranges.get("species") or []):
        nm = str(rec.get("精灵") or "").strip()
        if nm:
            names.append(nm)
    for _season, mem in (roster or {}).items():
        for nm in (mem or {}):
            nm = str(nm).strip()
            if nm and nm not in names:
                names.append(nm)
    # 2) 一阶段名（进化链首段）也顺手抓一份，做兜底
    heads = {}
    for rec in (ranges.get("species") or []):
        nm = str(rec.get("精灵") or "").strip()
        chain = str(rec.get("进化链") or "").strip()
        if not nm or not chain:
            continue
        head = re.split(r"[→>]", chain)[0].strip()
        head = re.sub(r"[（(].*?[)）]", "", head).strip()
        if head:
            heads.setdefault(nm, head)
            if head not in names:
                names.append(head)
    print(f"要查 {len(names)} 个名字（图鉴 + 花名册 + 一阶段名）")

    pets, colors, miss = {}, {}, []
    for i, nm in enumerate(names, 1):
        h = fetch(nm, force=force)
        if not h:
            miss.append(nm)
            continue
        if not colors:
            colors = parse_colors(h)
        types = parse_types(h)
        if types:
            pets[nm] = types
        else:
            miss.append(nm)
        if i % 25 == 0:
            print(f"  … {i}/{len(names)}  已取 {len(pets)} 只")

    # 3) 多形态精灵主页面 404 → 搜「名字（形态）」页，取第一张带系别块的
    searched = 0
    for nm in [x for x in miss if x not in pets]:
        for title in wiki_search(nm):
            if title == nm:
                continue
            h = fetch(title, force=False)
            if not h:
                continue
            if not colors:
                colors = parse_colors(h)
            types = parse_types(h)
            if types:
                pets[nm] = types
                searched += 1
                break
    if searched:
        print(f"  搜索兜底补了 {searched} 只（多形态页 404 的那种）")

    # 4) 进化链兜底：还拿不到的，用一阶段那只的系别
    chain = {}
    for nm, head in heads.items():
        got = pets.get(head) or pets.get(nm)
        if got:
            chain[nm] = got
    filled = 0
    still = []
    for nm in miss:
        if nm in pets:
            continue
        src = pets.get(heads.get(nm, "")) or chain.get(nm) or []
        if src:
            pets[nm] = src
            filled += 1
        else:
            still.append(nm)

    if not colors:
        print("!! 没抓到系别配色表，检查页面结构是不是变了")
        return 1

    out = {
        "updated": time.strftime("%Y-%m-%d"),
        "source": "https://wiki.biligame.com/nrc",
        "note": "系别取自 wiki 精灵页 sd-p-header-types；配色取自页面 CSS 的 --sd-class-accent",
        "colors": dict(sorted(colors.items())),
        "pets": dict(sorted(pets.items())),
        "chain": dict(sorted(chain.items())),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n系别配色 {len(colors)} 种：{', '.join(sorted(colors))}")
    print(f"精灵系别 {len(pets)} 只（进化链兜底补了 {filled} 只）· 还差 {len(still)} 只："
          f"{still[:12]}")
    print(f"→ {OUT}  {OUT.stat().st_size:,} B")
    return 0


if __name__ == "__main__":
    sys.exit(main())
