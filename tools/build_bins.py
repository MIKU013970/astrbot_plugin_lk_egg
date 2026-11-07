# -*- coding: utf-8 -*-
"""把「区间数」补全：从蛋神助手的 state/egg_bins.json 生成插件自带的 `bins.json`。

`egg_bins.json` 有三个表：
  bins     : 蛋名 → 区间数（权威，292 条）
  families : 蛋名 → [蛋名, 一阶段, 二阶段, …]（同一条进化链的全部名字）
  groups   : 蛋名 → 蛋组

图鉴 ranges.json 里有 20 多只精灵是按**蛋名**收录的（玳龟 → 玳龟的蛋、星星眼 → 星星眼的蛋），
按精灵名查不到 → 卡片上就没区间。这里把「精灵名 / 形态名 → 区间数」全部摊平成一张表，
运行时 `_bins_of()` 兜底查它。

用法：python tools/build_bins.py
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

V4 = Path(__file__).resolve().parents[2]
PLUGIN = V4 / "astrbot_plugin_lk_egg"
APP = Path.home() / "Desktop" / "蛋神助手-自动买单" / "state" / "egg_bins.json"
OUT = PLUGIN / "bins.json"

#: 官方数据里偶发零宽字符（\u200b 一类），统一清掉
_ZW = re.compile("[\u200b\u200c\u200d\u200e\u200f\ufeff]")


def _clean(s: str) -> str:
    return _ZW.sub("", str(s or "")).strip()


def main() -> int:
    d = json.loads(APP.read_text(encoding="utf-8", errors="replace"))
    bins = d.get("bins") or {}
    families = d.get("families") or {}
    print(f"蛋名 {len(bins)} 条 · 家族 {len(families)} 条")

    out: dict = {}
    for egg, n in bins.items():
        egg = _clean(egg)
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        out[str(egg)] = n
        # 蛋名去掉「的蛋 / 蛋」得到精灵名
        base = _clean(egg)
        for suf in ("的蛋", "蛋"):
            if base.endswith(suf) and len(base) > len(suf):
                out.setdefault(base[: -len(suf)], n)
                break
        for nm in (families.get(egg) or []):
            nm = _clean(nm)
            if nm:
                out.setdefault(nm, n)

    # 再补一层：ranges.json 里按精灵名收的也摊进去（保证名字变体都能查到）
    try:
        doc = json.loads((PLUGIN / "ranges.json").read_text(encoding="utf-8-sig"))
        for rec in (doc.get("species") or []):
            nm = _clean(rec.get("精灵"))
            if not nm:
                continue
            n = rec.get("区间数")
            if n in (None, ""):
                for v in (rec.get("variants") or []):
                    if "异色" in str(v.get("蛋名") or ""):
                        continue
                    if v.get("区间数") not in (None, ""):
                        n = v.get("区间数")
                        break
            try:
                if n not in (None, ""):
                    out.setdefault(nm, int(n))
            except (TypeError, ValueError):
                pass
    except Exception as e:
        print("ranges 合并跳过:", type(e).__name__, e)

    # ---- 手工补录（所有自动数据源里都没有的）----
    # 呆呆铃一链：用户给的官方数值
    #   蛋 身高 0.17-0.23 / 体重 0.6650-1.5320，大块头 1.51471 / 小不点 0.6823
    #   区间数 = round((1.5320-0.6650)/0.05)+1 = 18（用 大块头-小不点 也算 18，互相印证）
    # 跳跳铃 / 铃兰夫人 是同一颗蛋的二三阶段，沿用 18
    # 伊里斯 = 图鉴 117，与 彩虹独角兽 / 白金独角兽 同链，蛋是「小独角兽蛋」（区间 155）
    MANUAL = {
        "呆呆铃": 18, "跳跳铃": 18, "铃兰夫人": 18,
        "伊里斯": 155, "彩虹独角兽": 155, "白金独角兽": 155,
    }
    for k, v in MANUAL.items():
        out.setdefault(k, v)
    print(f"手工补录 {len(MANUAL)} 个：{MANUAL}")

    OUT.write_text(json.dumps({
        "updated": time.strftime("%Y-%m-%d"),
        "source": "蛋神助手 state/egg_bins.json (bins + families) + ranges.json",
        "count": len(out),
        "bins": dict(sorted(out.items())),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {OUT.name}: {len(out)} 个名字 · {OUT.stat().st_size / 1024:.0f} KB")

    # 抽查之前缺的那几只
    for nm in ("玳龟", "小浣蛋", "星星眼", "章脑丸", "幽铃", "布灵", "未完虫",
               "量风碗", "诅咒狼灵", "新月鹭", "热团团",
               "呆呆铃", "跳跳铃", "铃兰夫人", "伊里斯", "霹雳迪迪", "千棘海针"):
        print(f"   {nm}: {out.get(nm, '—')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
