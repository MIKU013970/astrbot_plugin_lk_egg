# -*- coding: utf-8 -*-
"""从 wiki「孵蛋组别查询」页抓每个蛋的公母：仅雄性 / 仅雌性 / 两性。

页面结构（每个蛋一张卡）：
    <div class="nrc-egg-card ..." data-search="189 棋骑士 棋骑士（白子） 妖精组">
      <span class="nrc-egg-number">NO.189</span>
      <span class="nrc-egg-male-only" title="仅雄性">M</span>      ← 仅雄性
      <span class="nrc-egg-female-only" title="仅雌性">F</span>    ← 仅雌性
      <span class="nrc-egg-stage">二阶</span>
      <img alt="棋骑士" ...>
"""
from __future__ import annotations

import json
import re
from pathlib import Path

V4 = Path(r"C:\Users\MIKU\Desktop\v4")
CACHE = V4 / "_lk_verify" / "cache"
OUT = V4 / "_wiki_gender.json"


def main():
    fp = CACHE / "nrc_孵蛋组别查询.html"
    h = fp.read_text(encoding="utf-8", errors="replace")
    print(f"页面 {len(h)} 字符")

    # 每张卡都以 data-search="..." 开头 → 按它切，最稳（外层 div 的 class 位置不固定）
    chunks = re.split(r'(?=data-search=")', h)
    out = {}
    for c in chunks[1:]:
        m = re.match(r'data-search="([^"]*)"', c)
        if not m:
            continue
        attrs = m.group(1).split()
        num = attrs[0] if attrs else ""
        group = attrs[-1] if len(attrs) > 2 else ""
        img = re.search(r'<img[^>]+alt="([^"]+)"', c)
        name = img.group(1) if img else (attrs[1] if len(attrs) > 1 else "")
        form = ""
        if len(attrs) > 3:
            form = " ".join(attrs[1:-1])
        male = 'nrc-egg-male-only' in c[:2500]
        female = 'nrc-egg-female-only' in c[:2500]
        stage = re.search(r'nrc-egg-stage">([^<]*)<', c[:2500])
        if male and female:
            gender = "雄性 50% / 雌性 50%"
        elif male:
            gender = "只有雄性（100% ♂）"
        elif female:
            gender = "只有雌性（100% ♀）"
        else:
            gender = ""
        rec = out.get(name) or {"编号": num, "蛋组": group,
                                "阶段": stage.group(1) if stage else "",
                                "形态": [], "公母": gender,
                                "male": male, "female": female}
        if form:
            rec["形态"].append(form)
        if gender and not rec["公母"]:
            rec["公母"] = gender
        rec["male"] = rec["male"] or male
        rec["female"] = rec["female"] or female
        out[name] = rec

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"解析出 {len(out)} 个蛋 → {OUT}")
    from collections import Counter
    cnt = Counter(v["公母"] for v in out.values())
    for k, n in cnt.most_common():
        print(f"  {k or '（无标记 → 两性）':<24} {n}")
    print("\n带标记的（仅雄/仅雌）：")
    for k, v in out.items():
        if v["公母"]:
            print(f"  {k:<10} {v['公母']:<18} 编号={v['编号']} 蛋组={v['蛋组']} 形态={v['形态']}")


if __name__ == "__main__":
    main()
