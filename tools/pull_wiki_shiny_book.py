# -*- coding: utf-8 -*-
"""从 wiki「异色记录本」抓**官方异色名单**（含常驻 / 各赛季奇遇），生成 `shiny_book.json`。

这页就是游戏里「异色记录本」的数据源，页面里嵌着 JSON：

```html
<div data-shiny-family="{"types":[...],"seasons":[
      {"id":"none","pets":19,"name":"常驻","eggs":8},
      {"id":"S1","pets":47,"name":"暗夜拾光","eggs":17}, … ],  }">
<div data-shiny-family="{"pets":[
      {"number":"021","id":"pet_000131","form":"","season":"S4",
       "image":"PetAvatar_3145_1.png","name":"小灵菇","mark":"sp_3145",
       "title":"小灵菇","stage":1,"types":["幽"]}, … ]}">
```

产出：
```json
{"updated": "...", "source": "…/异色记录本",
 "seasons": [{"id":"none","name":"常驻","pets":19,"eggs":8}, …],
 "pets": [{"name","season","season_name","stage","types","number","form","mark"}…]}
```

用法：`python tools/pull_wiki_shiny_book.py`
"""
from __future__ import annotations

import html as htmlmod
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
OUT = PLUGIN / "shiny_book.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def fetch(force: bool = False) -> str:
    fp = CACHE / "nrc_异色记录本.html"
    if not force and fp.is_file() and fp.stat().st_size > 100_000:
        return fp.read_text(encoding="utf-8", errors="replace")
    url = "https://wiki.biligame.com/nrc/" + urllib.parse.quote("异色记录本")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
        h = r.read().decode("utf-8", "replace")
    time.sleep(0.3)
    CACHE.mkdir(parents=True, exist_ok=True)
    fp.write_text(h, encoding="utf-8")
    return h


def main() -> int:
    force = "--force" in sys.argv
    h = fetch(force)
    dec = htmlmod.unescape(h)          # 页面里 JSON 是 HTML 实体转义的
    print(f"页面 {len(h)} 字符")

    seasons, pets = [], []

    def json_blobs(text: str):
        """把 data-shiny-family="{…}" 里的 JSON 抠出来（按大括号配对，能处理嵌套）。"""
        for m in re.finditer(r'data-shiny-family="', text):
            i = m.end()
            if i >= len(text) or text[i] != "{":
                continue
            depth = 0
            in_str = False
            esc = False
            for j in range(i, len(text)):
                ch = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        yield text[i:j + 1]
                        break

    # 赛季表不在 data-shiny-family 属性里，单独按 `"seasons":[…]` 抠
    def array_after(text: str, key: str):
        i = text.find(key)
        if i < 0:
            return None
        j = text.find("[", i)
        if j < 0:
            return None
        depth = 0
        in_str = False
        esc = False
        for k in range(j, len(text)):
            ch = text[k]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[j:k + 1])
                    except Exception:
                        return None
        return None

    got = array_after(dec, '"seasons":')
    if isinstance(got, list):
        seasons = [s for s in got if isinstance(s, dict)]

    for raw in json_blobs(dec):
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if isinstance(obj.get("seasons"), list) and not seasons:
            seasons = [s for s in obj["seasons"] if isinstance(s, dict)]
        for it in (obj.get("pets") or []):
            if isinstance(it, dict) and it.get("name"):
                pets.append(it)

    # 去重（同一个名字可能因为形态出现多次：同 season + 同名字只留 stage 最小的）
    best = {}
    for it in pets:
        key = (str(it.get("season") or "none"), str(it.get("name")))
        rank = (int(it.get("stage") or 9), str(it.get("number") or ""))
        cur = best.get(key)
        if cur is None or rank < cur[0]:
            best[key] = (rank, it)
    rows = []
    sname = {str(s.get("id")): str(s.get("name") or "") for s in seasons}
    for (_sid, _nm), (_rank, it) in best.items():
        sid = str(it.get("season") or "none")
        rows.append({
            "name": str(it.get("name")),
            "season": sid,
            "season_name": sname.get(sid, ""),
            "stage": int(it.get("stage") or 0),
            "types": [str(t) for t in (it.get("types") or []) if t],
            "number": str(it.get("number") or ""),
            "form": str(it.get("form") or ""),
            "mark": str(it.get("mark") or ""),
            "title": str(it.get("title") or it.get("name") or ""),
        })
    rows.sort(key=lambda r: (r["season"], r["number"], r["name"]))

    out = {
        "updated": time.strftime("%Y-%m-%d"),
        "source": "https://wiki.biligame.com/nrc/异色记录本",
        "note": "官方异色记录本：常驻（id=none）+ 各赛季奇遇；同赛季同名只留一阶段",
        "seasons": seasons,
        "pets": rows,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    cnt = {}
    for r in rows:
        cnt[r["season"]] = cnt.get(r["season"], 0) + 1
    print("赛季：" + " · ".join(f"{s.get('id')}={s.get('name')}({s.get('pets')}只/异色蛋{s.get('eggs')})"
                              for s in seasons))
    print("抓到的精灵：" + " · ".join(f"{k} {v} 只" for k, v in sorted(cnt.items())))
    s5 = [r["name"] for r in rows if r["season"] == "S5"]
    none = [r["name"] for r in rows if r["season"] == "none"]
    print(f"\n常驻 {len(none)} 只：{none}")
    print(f"\nS5 奇遇 {len(s5)} 只：{s5}")
    print(f"\n→ {OUT}  {OUT.stat().st_size:,} B")
    return 0


if __name__ == "__main__":
    sys.exit(main())
