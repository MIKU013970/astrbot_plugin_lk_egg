# -*- coding: utf-8 -*-
"""从 wiki 精灵页的「进化」块抓**进化链**，生成 `chains.json`。

为什么要这个：官方接口只给图鉴号和阶段，不给「谁进化成谁」。
花名册里同一只精灵的不同阶段是不同名字（缇塔 / 声波缇塔），
要合成一张卡就得知道它们是同一条链。

页面结构：
```html
<div class="sd-p-evo-node">
  <div class="sd-p-evo-card"><div class="sd-p-evo-avatar">
     <img alt="缇塔" ...></div><div class="sd-p-evo-name">初阶 · 机械</div>...
```
`alt` 就是链上每个阶段的名字，按顺序排。

产出 `chains.json`：
```json
{"updated": "2026-11-07",
 "chains": {"地银链": ["地银链", "地底银鳞", "地绡银龙"], ...},
 "members": {"地底银鳞": "地银链", ...}}
```

用法：`python tools/pull_wiki_chains.py`（带缓存，只补缺的）
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
V4 = PLUGIN.parent
CACHE = V4 / "_lk_verify" / "cache"
OUT = PLUGIN / "chains.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
SLEEP = 0.3


def fetch(name: str, force: bool = False) -> str:
    fp = CACHE / f"nrc_{name}.html"
    if not force and fp.is_file() and fp.stat().st_size > 50_000:
        return fp.read_text(encoding="utf-8", errors="replace")
    url = "https://wiki.biligame.com/nrc/" + urllib.parse.quote(name)
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            h = r.read().decode("utf-8", "replace")
    except Exception:
        return ""
    time.sleep(SLEEP)
    CACHE.mkdir(parents=True, exist_ok=True)
    fp.write_text(h, encoding="utf-8")
    return h


def parse_chain(h: str) -> list:
    """按顺序取进化块里的 alt 名字（同一段里的重复去掉）。"""
    body = re.sub(r"<style[\s\S]*?</style>", "", h)
    body = re.sub(r"<script[\s\S]*?</script>", "", body)
    out: list = []
    for seg in body.split("sd-p-evo-node")[1:]:
        m = re.search(r'alt="([^"]{1,20})"', seg[:900])
        if not m:
            continue
        nm = m.group(1).strip()
        if nm and (not out or out[-1] != nm):
            out.append(nm)
    return out


def search_pages(name: str) -> list:
    """主页面 404 时（多形态精灵）搜「名字（形态）」页。"""
    url = ("https://wiki.biligame.com/nrc/index.php?search="
           + urllib.parse.quote(name) + "&fulltext=1")
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=25) as r:
            h = r.read().decode("utf-8", "replace")
    except Exception:
        return []
    time.sleep(SLEEP)
    titles = re.findall(r'mw-search-result-heading">\s*<a href="[^"]+"[^>]*title="([^"]+)"', h)
    return [t for t in titles if name[:2] in t][:3]


def main() -> int:
    force = "--force" in sys.argv
    pets = json.loads((PLUGIN / "pets.json").read_text(encoding="utf-8-sig"))["pets"]
    names = []
    seen = set()
    for r in pets:
        nm = str(r.get("name") or "")
        if nm and nm not in seen:
            seen.add(nm)
            names.append(nm)
    print(f"要查 {len(names)} 个名字的进化链")

    chains: dict = {}
    miss = []
    for i, nm in enumerate(names, 1):
        if nm in chains:
            continue
        h = fetch(nm, force=force)
        got = parse_chain(h) if h else []
        if not got:
            for title in search_pages(nm):
                h2 = fetch(title, force=False)
                got = parse_chain(h2) if h2 else []
                if got:
                    break
        if got and nm not in got:
            got.append(nm)
        if got:
            head = got[0]
            for x in got:
                if x not in chains:
                    chains[x] = got
        else:
            miss.append(nm)
        if i % 40 == 0:
            print(f"  … {i}/{len(names)}  已拿 {len(chains)} 条")

    # 老 ranges.json 里的进化链兜底（wiki 缺页的老精灵）
    try:
        doc = json.loads((PLUGIN / "ranges.json").read_text(encoding="utf-8-sig"))
        added = 0
        for rec in (doc.get("species") or []):
            nm = str(rec.get("精灵") or "").strip()
            chain = str(rec.get("进化链") or "").strip()
            if not nm or not chain or nm in chains:
                continue
            parts = [re.sub(r"[（(].*?[)）]", "", x).strip()
                     for x in re.split(r"[→>]|/|、", chain) if x.strip()]
            parts = [x for x in parts if x]
            if len(parts) >= 2:
                chains[nm] = parts
                added += 1
        print(f"  用 ranges.json 的进化链补了 {added} 条")
    except Exception as e:
        print(f"  ! ranges 兜底失败 {type(e).__name__}: {e}")

    members = {}
    mult = {}
    for head, chain in chains.items():
        for x in chain:
            members.setdefault(x, head)
            if x != head:
                mult.setdefault(head, []).append(x)
    out = {
        "updated": time.strftime("%Y-%m-%d"),
        "source": "https://wiki.biligame.com/nrc 精灵页 sd-p-evo-node + ranges.json 兜底",
        "chains": dict(sorted(chains.items())),
        "members": dict(sorted(members.items())),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n进化链 {len(chains)} 条 · 覆盖名字 {len(members)} 个 · 多阶段链 {len(mult)} 条")
    for probe in ("地银链", "缇塔", "声波缇塔", "小翼龙", "乐乐鲨"):
        print(f"   {probe} → {chains.get(probe)}")
    print(f"还没拿到 {len(miss)} 只：{miss[:12]}")
    print(f"→ {OUT}  {OUT.stat().st_size:,} B")
    return 0


if __name__ == "__main__":
    sys.exit(main())
