# -*- coding: utf-8 -*-
"""从官方 wiki 接口**全量**拉精灵表（708 只），生成：

* `pets.json`          —— 官方原表（名字/图鉴号/赛季/阶段/异色/同乘/蛋组/系别）
* `season_roster.json` —— 按赛季分组（老格式，异色名单和网格图都用它）
* `elements.json`      —— 系别表（官方 type_names）+ 官方系别配色（wiki CSS）

用法：
    python tools/pull_pets_api.py            # 全量重拉
    python tools/pull_pets_api.py --no-color # 只更新精灵表，不动配色

接口：`/api/v1/games/rocom/wiki/pets?page_no=N&page_size=100`
老版本 build_season_roster.py 只取了 page=1（每筛选项 100 条），所以会漏精灵
（例如 S5 的「音波提塔」）。现在改成翻页拉全量。
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://wegame.shallow.ink"
HDR = {"X-API-Key": "sk-d14d6fcb35e5aa83b28e282ec24d0473",
       "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
PLUGIN = Path(__file__).resolve().parent.parent
V4 = PLUGIN.parent
CACHE = V4 / "_lk_verify" / "cache"

PETS = PLUGIN / "pets.json"
ROSTER = PLUGIN / "season_roster.json"
ELEMENTS = PLUGIN / "elements.json"

#: 拿不到官方配色的系（官方系名 → 兜底色），正常都从 wiki CSS 取
FALLBACK_ACCENT = {
    "普通": "#9aa0a6", "草": "#4caf50", "火": "#f4633a", "水": "#3ea0f7",
    "电": "#e4c800", "冰": "#66afd8", "地": "#977c48", "翼": "#8fd0e8",
    "虫": "#7cb342", "龙": "#e64c62", "幽": "#8e6bbf", "恶": "#5b4257",
    "幻": "#b388eb", "机械": "#8d99ae", "武": "#c46a2f", "毒": "#9b59b6",
    "萌": "#f48fb1", "光": "#f2c14e",
}


def get(path: str, params: dict) -> dict:
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers=HDR)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            if i == 2:
                print(f"  ! {url[:110]} → {type(e).__name__}: {e}")
            time.sleep(0.8)
    return {}


def pull_all() -> dict:
    """翻页拉全量：{pet_id: 记录}。

    注意：接口按 **名字** 会重（多形态是同名不同 pet_id，比如鸭吉吉 6 个形态），
    所以这里按 pet_id 收，别按名字去重。
    """
    out: dict = {}
    page = 1
    total_pages = 1
    while page <= total_pages:
        d = get("/api/v1/games/rocom/wiki/pets",
                {"page_no": page, "page_size": 100})
        data = d.get("data") or {}
        total_pages = int(data.get("total_pages") or 1)
        total = data.get("total")
        items = data.get("items") or []
        for it in items:
            pid = str(it.get("pet_id") or "")
            if pid and pid not in out:
                out[pid] = it
        print(f"  page {page}/{total_pages} → 本页 {len(items)} · 累计 {len(out)} / 官方 {total}")
        if not data.get("has_more"):
            break
        page += 1
    return out


def wiki_colors(force: bool = False) -> dict:
    """官方系别配色：从 wiki 精灵页 CSS 里取（--sd-class-tone / --sd-class-accent）。"""
    fp = CACHE / "nrc_小翼龙.html"
    h = ""
    if not force and fp.is_file() and fp.stat().st_size > 50_000:
        h = fp.read_text(encoding="utf-8", errors="replace")
    if not h:
        url = "https://wiki.biligame.com/nrc/" + urllib.parse.quote("小翼龙")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": HDR["User-Agent"]})
            with urllib.request.urlopen(req, timeout=25) as r:
                h = r.read().decode("utf-8", "replace")
            CACHE.mkdir(parents=True, exist_ok=True)
            fp.write_text(h, encoding="utf-8")
        except Exception as e:
            print(f"  ! 配色抓取失败 {type(e).__name__}: {e}")
    out = {}
    for el, tone, accent in re.findall(
            r'data-sd-element="([^"]+)"\]\s*\{\s*--sd-class-tone:\s*(#[0-9a-fA-F]{6});\s*'
            r'--sd-class-accent:\s*(#[0-9a-fA-F]{6})', h or ""):
        el = el.strip()
        if el.endswith("系"):
            out[el[:-1]] = {"tone": tone.lower(), "accent": accent.lower()}
    return out


def main() -> int:
    print("=== 拉官方精灵表 ===")
    table = pull_all()
    if len(table) < 700:
        print(f"!! 只拿到 {len(table)} 只，先别覆盖旧数据")
        return 1

    rows = []
    for _pid, it in table.items():
        nm = str(it.get("name") or "").strip()
        if not nm:
            continue
        try:
            si = int(it.get("season_id") or 0)
        except (TypeError, ValueError):
            si = 0
        try:
            stage = int(it.get("stage") or 0)
        except (TypeError, ValueError):
            stage = 0
        rows.append({
            "name": nm,
            "pet_id_u": _pid,
            "pet_id": it.get("pet_id"),
            "no": str(it.get("handbook_no") or ""),
            "season": si,
            "stage": stage,
            "has_shiny": bool(it.get("has_shiny")),
            "ride": bool(it.get("has_ride_talent")),
            "groups": [g for g in (it.get("egg_group_names") or []) if g],
            "types": [t for t in (it.get("type_names") or []) if t],
            "quality": it.get("quality"),
        })
    rows.sort(key=lambda r: (r["season"], str(r["no"]), r["name"]))
    PETS.write_text(json.dumps({"source": "wegame.shallow.ink /wiki/pets",
                                "count": len(rows), "pets": rows},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {PETS.name}: {len(rows)} 只")

    # ---------------- 赛季花名册（老格式）----------------
    # 同一只精灵的多个形态同名不同 pet_id —— 按 **(赛季, 名字)** 去重，取 stage 最小、pet_id 最小的
    # （不同赛季可以同名：缇塔 在 S0 是 464 号、在 S5 是 229 号，那是两只，不能合）
    best: dict = {}
    for r in rows:
        key = (r["stage"] or 9, str(r["pet_id"]))
        gk = (r["season"], r["name"])
        cur = best.get(gk)
        if cur is None or key < cur[0]:
            best[gk] = (key, r)
    roster: dict = {}
    for (_s, _nm), (_k, r) in best.items():
        sid = f"S{r['season']}" if r["season"] >= 1 else "S0"
        roster.setdefault(sid, {})[r["name"]] = {
            "pet_id": r["pet_id"],
            "has_shiny": r["has_shiny"],
            "groups": r["groups"],
            "no": r["no"],
            "stage": r["stage"],
            "ride": r["ride"],
            "types": r["types"],
        }
        if r["stage"] >= 4:
            roster[sid][r["name"]]["leader"] = True
    old = {}
    try:
        old = json.loads(ROSTER.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    # 接口里没有、但老花名册有（手动补过的）→ 保留，别丢
    kept = 0
    for sid, mem in (old or {}).items():
        for nm, info in (mem or {}).items():
            if nm not in roster.get(sid, {}):
                roster.setdefault(sid, {})[nm] = info
                kept += 1
    out = {s: dict(sorted(v.items(), key=lambda kv: (str(kv[1].get("no") or "9999"), kv[0])))
           for s, v in sorted(roster.items())}
    ROSTER.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {ROSTER.name}: " + " · ".join(
        f"{s} {len(v)}只(异色{sum(1 for x in v.values() if x.get('has_shiny'))})"
        for s, v in out.items()) + f" · 保留老条目 {kept}")

    # ---------------- 系别表 ----------------
    colors = {} if "--no-color" in sys.argv else wiki_colors()
    have = set()
    types_used = {}
    for r in rows:
        for t in r["types"]:
            types_used[t] = types_used.get(t, 0) + 1
    for t in sorted(types_used):
        if t in colors:
            have.add(t)
        else:
            colors[t] = {"tone": "#6b6b6b", "accent": FALLBACK_ACCENT.get(t, "#9aa0a6")}
    pets_el = {}
    for (_s, nm), (_k, rr) in best.items():
        if rr["types"]:
            pets_el.setdefault(nm, rr["types"])
    ELEMENTS.write_text(json.dumps({
        "updated": time.strftime("%Y-%m-%d"),
        "source": "官方 wiki 接口 type_names + wiki 页面 CSS 系别配色",
        "colors": dict(sorted(colors.items())),
        "pets": dict(sorted(pets_el.items())),
        "chain": {},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {ELEMENTS.name}: 系别 {len(types_used)} 种 / 精灵 {len(pets_el)} 只"
          f"（配色有官方值 {len(have)} 种，其余兜底）")
    print(f"   系别分布：{types_used}")
    for probe in ("声波缇塔", "音波提塔", "翼龙", "地银链", "缇塔"):
        hit = next((r for r in rows if r["name"] == probe), None)
        if hit:
            print(f"   {probe}: 图鉴{hit['no']} S{hit['season']} stage{hit['stage']} "
                  f"异色={hit['has_shiny']} 同乘={hit['ride']} 组={hit['groups']} 系={hit['types']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
