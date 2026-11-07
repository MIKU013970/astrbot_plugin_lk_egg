# -*- coding: utf-8 -*-
"""用官方资源接口补异色精灵图：有 has_shiny=true 但本地没图的，全部补上。

  GET /api/v1/games/rocom/wiki/pets?q=名字   → pet_id / has_shiny
  GET /api/v1/resources/wiki/assets/pets/<pet_id>/shiny.png
存成 icons/shiny/shiny<pet_id>.png（和原有命名一致），登记 pet_shiny[名字]。
"""
from __future__ import annotations

import io
import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

BASE = "https://wegame.shallow.ink"
HDR = {"X-API-Key": "sk-d14d6fcb35e5aa83b28e282ec24d0473",
       "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
V4 = Path(r"C:\Users\MIKU\Desktop\v4")
PLUGIN = V4 / "astrbot_plugin_lk_egg"
ICONS = PLUGIN / "icons"


def api(path: str, params: dict):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=25) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return {}


def raw(url: str):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                    timeout=30) as r:
            return r.read()
    except Exception:
        return None


def save(raw_bytes: bytes, dst: Path, size: int = 96) -> bool:
    try:
        im = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
        im.thumbnail((size, size), Image.LANCZOS)
        c = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        c.paste(im, ((size - im.width) // 2, (size - im.height) // 2), im)
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
    chain = {}
    for rec in doc["species"]:
        parts = [x.strip() for x in str(rec.get("进化链") or "").replace("→", "->").split("->")
                 if x.strip()]
        if len(parts) >= 2:
            for p in parts[1:]:
                chain.setdefault(p, parts[0])
    print(f"检查 {len(names)} 只", flush=True)

    def work(nm):
        if nm in idx["pet_shiny"]:
            return nm, "已有"
        d = api("/api/v1/games/rocom/wiki/pets", {"q": nm, "page": 1, "page_size": 5})
        it = next((x for x in ((d.get("data") or {}).get("items") or [])
                   if str(x.get("name")) == nm), None)
        if not it:
            return nm, "接口没有"
        if not it.get("has_shiny"):
            return nm, "游戏无异色"
        pid = it.get("pet_id")
        data = raw(f"{BASE}/api/v1/resources/wiki/assets/pets/{pid}/shiny.png")
        if not data:
            return nm, "图下载失败"
        if save(data, ICONS / "shiny" / f"shiny{pid}.png"):
            idx["pet_shiny"][nm] = f"shiny{pid}"
            return nm, f"补上 shiny{pid}"
        return nm, "存盘失败"

    stats = {"已有": [], "游戏无异色": [], "接口没有": [], "图下载失败": [], "存盘失败": []}
    added = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for nm, res in ex.map(work, names):
            stats.setdefault(res, []).append(nm)
            if res.startswith("补上"):
                added.append((nm, res))

    # 一阶段也挂上异色（异色一阶段）
    for nm, base in chain.items():
        s = idx["pet_shiny"].get(base)
        if s and nm not in idx["pet_shiny"]:
            idx["pet_shiny"][nm] = s

    idx_fp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n新补异色图 {len(added)} 只：")
    for nm, res in added[:40]:
        print(f"  {nm:<10} {res}")
    print("\n统计：")
    for k, v in stats.items():
        if v:
            print(f"  {k}: {len(v)}")
    print(f"pet_shiny 总数 {len(idx['pet_shiny'])} · shiny 文件数 "
          f"{len(list((ICONS / 'shiny').glob('*.png')))}")


if __name__ == "__main__":
    main()
