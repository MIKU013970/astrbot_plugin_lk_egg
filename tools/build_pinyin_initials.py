# -*- coding: utf-8 -*-
"""生成插件的拼音首字母表 pinyin_initials.json。

  · 大部分字用 GB2312 区位近似算法（纯标准库，不需要装拼音包）
  · 算法认不出的 54 个字（多为二级字库里的生僻字/动物字）在这里手工补

跑一次就行；以后 ranges 里加了新字，再跑一次会自动补进来。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

V4 = Path(r"C:\Users\MIKU\Desktop\v4")
PLUGIN = V4 / "astrbot_plugin_lk_egg"
sys.path.insert(0, str(V4))
from _test_pinyin_initials import initial  # noqa: E402

#: 算法认不出的字，手工标注（都是精灵名/蛋名里真出现过的）
MANUAL = {
    "冥": "M", "呱": "G", "咔": "K", "咩": "M", "咪": "M", "喵": "M", "嘟": "D",
    "噼": "P", "学": "X", "岚": "L", "嵴": "J", "嶂": "Z", "巡": "X", "廿": "N",
    "曜": "Y", "枭": "X", "浣": "H", "炫": "X", "煲": "B", "獠": "L", "玳": "D",
    "玺": "X", "珀": "P", "瞌": "K", "砣": "T", "绡": "X", "绮": "Q", "缇": "T",
    "翎": "L", "翡": "F", "芙": "F", "蚪": "D", "蜥": "X", "蝌": "K", "蝠": "F",
    "裘": "Q", "谕": "Y", "豚": "T", "铠": "K", "雪": "X", "雳": "L", "魅": "M",
    "鱿": "Y", "鲨": "S", "鳍": "Q", "鳐": "Y", "鳗": "M", "鹄": "H", "鹫": "J",
    "鹬": "Y", "鹭": "L", "鹳": "G", "鼬": "Y", "鼹": "Y",
}

#: 少数多音字，按游戏里的读法纠正
FIX = {
    "长": "C",      # 长江豚 cháng
    "重": "C",      # 重甲 chóng
    "乐": "L",      # 乐乐鲨 lè
    "觉": "J",      # 觉醒 jué
    "行": "X",      # 行者 xíng
    "角": "J",      # 犀角鸟 jiǎo
    "落": "L",
    "奇": "Q",
    "朝": "C",
    "都": "D",
}


def main() -> int:
    ranges = json.loads((PLUGIN / "ranges.json").read_text(encoding="utf-8"))
    chars: set[str] = set()

    def feed(s) -> None:
        for c in str(s or ""):
            if "\u4e00" <= c <= "\u9fff":
                chars.add(c)

    for rec in ranges["species"]:
        feed(rec.get("精灵"))
        feed(rec.get("蛋名"))
        feed(rec.get("进化链"))
        for v in rec.get("variants") or []:
            feed(v.get("形态"))
            feed(v.get("形态简称"))
            feed(v.get("蛋名"))
    for k in (ranges.get("alias") or {}):
        feed(k)

    table: dict[str, str] = {}
    miss: list[str] = []
    for c in sorted(chars):
        if c in FIX:
            table[c] = FIX[c]
            continue
        if c in MANUAL:
            table[c] = MANUAL[c]
            continue
        i = initial(c)
        if i and i != "?":
            table[c] = i
        else:
            miss.append(c)

    out = PLUGIN / "pinyin_initials.json"
    out.write_text(json.dumps(table, ensure_ascii=False, indent=0, sort_keys=True),
                   encoding="utf-8")
    print(f"写入 {out}：{len(table)} 个字")
    if miss:
        print(f"⚠ 仍然认不出 {len(miss)} 个，需要补进 MANUAL: {''.join(miss)}")
    # 抽查几个
    for nm in ("翡翠水母", "豆粒鹅", "小剪影", "西瓜虫", "鲸羽", "长江豚", "蟹蟹椰", "滴溜豚"):
        print(f"  {nm:<8} → {''.join(table.get(c, '?') for c in nm)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
