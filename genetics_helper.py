# -*- coding: utf-8 -*-
"""遗传计算（洛克王国世界）—— 从「medal-inherit」skill 移植过来的单代计算内核。

来源与口径
----------
全部公式来自压缩包 `遗传计算skill_20261122/medal-inherit`（作者实测校准），
本模块只做移植 + 适配插件数据结构，**没有新增任何自造规则**。

    亲本身高%   p = (H − L)/(U − L)          L/U = 该物种一阶段身高区间
    子代身高%   p_child = (p父 + p母)/2，再过两段量化：
                蛋档 Ne=(Ue−Le)/0.01+1 → ke=floor(p×Ne) → 蛋高 E
                → 蛋% =(E−Le)/(Ue−Le) → 个体档 k1=floor(蛋%×N1) → 孵出身高
    子代体重%   规则1（一般）：(父%+母%)/2 ± 1pp
                规则2（双亲身高同为极值）：锁到那一端的高平台（满端 99.17）
    小不点      身高落在最低档 **且** 体重% ≤ 2.0
    大块头      身高落在最高档 **且** 体重% ≥ 98.0
    声音奖牌    dB ≥ 96 婉转声 / ≤ −96 粗嗓门（本模块保留但默认不参与，缺输入）

关于「几代」
-----------
源 skill 里**没有代数/多代迭代模型**：奖牌不看「传了几代」，
而是看子代出生时的身高档 + 体重%，**一代定生死**。
所以本模块提供的是「这一对父母 → 子代能否出奖牌 + 概率」，
不做「需要繁育几代」的推算（那需要源数据不存在的迭代规则）。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: 奖牌阈值（来自 species.json 的 meta.rules，保持一致）
SMALL_MAX = 2.0        # 小不点：体重% ≤ 2.0
BIG_MIN = 98.0         # 大块头：体重% ≥ 98.0
VOICE_ABS = 96         # |分贝| ≥ 96
#: 子代体重% 相对双亲平均的随机包络（±pp）。
#:
#: 依据源 skill（medal-inherit/SKILL.md 第 251 行）的实测：
#:   「436 颗蛋的『蛋% − 父母平均%』落在 ±1.9pp 内」
#: 也就是说子代体重不是定值，而是在「父母平均 ±1.9pp」这个区间里随机取。
#: 源工具默认用 1.0pp 是**保守包络**（medal.py NOISE=1.0），这里改按实测上限 1.9。
#: 想回到保守口径把下面改成 1.0 即可。
NOISE = 1.9
RULE2_TOP = 0.9917     # 规则2 满端平台（368 颗蛋实测 99.17±0.56）
RULE2_BOTTOM = 0.02    # 规则2 低端上限（≤2%）


def _pct(v: Optional[float], lo: Optional[float],
         hi: Optional[float]) -> Optional[float]:
    """(值 − 下限) / (上限 − 下限)，夹到 0~1。任一缺失返回 None。"""
    if v is None or lo is None or hi is None or hi <= lo:
        return None
    return max(0.0, min(1.0, (float(v) - float(lo)) / (float(hi) - float(lo))))


def _slots(lo: float, hi: float) -> int:
    """按 0.01 切出来的档数。"""
    return int(round((hi - lo) / 0.01)) + 1


def _prob_le(mean: float, thr: float, noise: float = NOISE) -> float:
    """子代 = mean ± noise（均匀分布）时 P(子代 ≤ thr)。"""
    if noise <= 0:
        return 1.0 if mean <= thr else 0.0
    return max(0.0, min(1.0, (thr - (mean - noise)) / (2.0 * noise)))


def _prob_ge(mean: float, thr: float, noise: float = NOISE) -> float:
    return _prob_le(-mean, -thr, noise)


def hatch_height(p: float,
                 h_egg: Optional[Tuple[float, float]],
                 h_stage1: Optional[Tuple[float, float]]
                 ) -> Dict[str, Any]:
    """两段量化：p → 蛋档 → 蛋高 → 蛋% → 孵出个体身高。

    没有蛋身高区间时退回「按 p 直接落一阶段档」的近似（源 skill 同款退化口径）。
    """
    out: Dict[str, Any] = {"model": "exact"}
    if h_egg:
        le, ue = float(h_egg[0]), float(h_egg[1])
        n_egg = _slots(le, ue)
        k_egg = max(0, min(n_egg - 1, int(math.floor(p * n_egg))))
        egg_m = round(min(le + 0.01 * k_egg, ue), 3)
        egg_pct = (egg_m - le) / (ue - le) if ue > le else 0.0
        out.update({"egg_h": egg_m, "egg_slot": k_egg, "egg_slots": n_egg,
                    "egg_pct": round(egg_pct * 100, 1)})
        if h_stage1:
            l1, u1 = float(h_stage1[0]), float(h_stage1[1])
            n1 = _slots(l1, u1)
            k1 = max(0, min(n1 - 1, int(math.floor(egg_pct * n1))))
            out.update({"hatch_h": round(min(l1 + 0.01 * k1, u1), 3),
                        "hatch_slot": k1, "hatch_slots": n1,
                        "hatch_pct": round(k1 / (n1 - 1) * 100, 1) if n1 > 1 else 0.0})
        return out
    if h_stage1:
        l1, u1 = float(h_stage1[0]), float(h_stage1[1])
        n1 = _slots(l1, u1)
        k1 = max(0, min(n1 - 1, int(math.floor(p * n1))))
        out.update({"model": "approx(无蛋身高区间)",
                    "hatch_h": round(min(l1 + 0.01 * k1, u1), 3),
                    "hatch_slot": k1, "hatch_slots": n1,
                    "hatch_pct": round(k1 / (n1 - 1) * 100, 1) if n1 > 1 else 0.0})
    return out


class Genetics:
    """单代奖牌计算器。数据 = genetics_species.json（压缩包 species.json 的副本）。"""

    def __init__(self, data_path: Path) -> None:
        self.path = Path(data_path)
        self._species: Dict[str, Dict[str, Any]] = {}
        self.rules: Dict[str, Any] = {}
        self.aliases: Dict[str, str] = {}
        self.non_breedable = set()
        self._load()

    # ---------------------------------------------------------------- 加载
    def _load(self) -> None:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:                                    # noqa: BLE001
            return
        self._species = doc.get("species") or {}
        meta = doc.get("meta") or {}
        self.rules = meta.get("rules") or {}
        self.aliases = meta.get("aliases") or {}
        self.non_breedable = set(meta.get("non_breedable") or [])

    @property
    def ready(self) -> bool:
        return bool(self._species)

    def names(self) -> List[str]:
        return list(self._species.keys())

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        """按名字取记录；剥掉「（xx的样子）」形态后缀、查别名。"""
        n = str(name or "").strip()
        if not n:
            return None
        for cand in (n, n.split("（")[0].strip(), n.split("(")[0].strip(),
                     self.aliases.get(n, "")):
            if cand and cand in self._species:
                return self._species[cand]
        return None

    def real_name(self, name: str) -> str:
        n = str(name or "").strip()
        for cand in (n, n.split("（")[0].strip(), n.split("(")[0].strip(),
                     self.aliases.get(n, "")):
            if cand and cand in self._species:
                return cand
        return n

    def breedable(self, name: str) -> bool:
        rec = self.get(name)
        if rec is None:
            return False
        if rec.get("breedable") is False:
            return False
        return self.real_name(name) not in self.non_breedable

    # ---------------------------------------------------------------- 解析
    def resolve(self, name: str, h: Optional[float] = None,
                w: Optional[float] = None,
                egg: Optional[List[str]] = None) -> Dict[str, Any]:
        """把「名字 + 身高/体重」整理成带百分比的结构。缺字段留 None，不猜。"""
        rec = self.get(name) or {}
        h_rng = rec.get("h_stage1")
        w_rng = rec.get("w_mon")
        return {
            "name": self.real_name(name),
            "rec": rec,
            "h": h, "w": w,
            "h_range": list(h_rng) if h_rng else None,
            "w_range": list(w_rng) if w_rng else None,
            "p_h": _pct(h, *(h_rng or (None, None))) if (h is not None and h_rng) else None,
            "p_w": _pct(w, *(w_rng or (None, None))) if (w is not None and w_rng) else None,
            "egg": list(egg) if egg else list(rec.get("egg") or []),
            "breedable": self.breedable(name),
        }

    # ---------------------------------------------------------------- 配对
    def pair(self, fa: Dict[str, Any], mo: Dict[str, Any],
             model: str = "auto", noise: float = NOISE) -> Dict[str, Any]:
        """算这一对的子代：身高档、体重%、奖牌概率。

        ⚠ 身高未知时**不能当 0%（最矮）**处理 —— 那会误触发「规则2（双亲同为极值）」，
        把体重锁到 2% 平台，算出假的「小不点 50%」。身高缺一个就走规则1（平均）。
        """
        p_fh = fa["p_h"]
        p_mh = mo["p_h"]
        h_known = (p_fh is not None) and (p_mh is not None)
        p_fh = p_fh if p_fh is not None else 0.0
        p_mh = p_mh if p_mh is not None else 0.0
        p_fw = fa["p_w"] if fa["p_w"] is not None else 0.0
        p_mw = mo["p_w"] if mo["p_w"] is not None else 0.0

        both_max = h_known and p_fh >= 0.985 and p_mh >= 0.985
        both_min = h_known and p_fh <= 0.015 and p_mh <= 0.015
        rule2 = both_max or both_min
        p_c = (p_fh + p_mh) / 2.0

        if model == "max":
            w_c = max(p_fw, p_mw)
        elif model == "avg":
            w_c = (p_fw + p_mw) / 2.0
        elif model == "legacy":
            w_c = max(0.98, p_fw, p_mw) if rule2 else (p_fw + p_mw) / 2.0
        else:
            if both_max:
                w_c = RULE2_TOP
            elif both_min:
                w_c = min(RULE2_BOTTOM, (p_fw + p_mw) / 2.0)
            else:
                w_c = (p_fw + p_mw) / 2.0

        # 子代物种 = 母本的基础形态（蛋属于基础形态）
        base = mo["rec"].get("base") or mo["name"]
        brec = self._species.get(base) or mo["rec"] or {}
        hh = hatch_height(p_c, brec.get("h_egg"), brec.get("h_stage1"))
        h_at_max = h_at_min = None
        if hh.get("hatch_slot") is not None:
            h_at_max = hh["hatch_slot"] == hh["hatch_slots"] - 1
            h_at_min = hh["hatch_slot"] == 0

        p_small = _prob_le(w_c * 100, SMALL_MAX, noise) if h_at_min else 0.0
        p_big = _prob_ge(w_c * 100, BIG_MIN, noise) if h_at_max else 0.0

        out: Dict[str, Any] = {
            "mother": mo["name"], "father": fa["name"],
            "dex": mo["rec"].get("dex"), "attr": mo["rec"].get("attr"),
            "child_species": base,
            "egg": mo["egg"], "shiny": mo["rec"].get("shiny"),
            "father_h_pct": round(p_fh * 100, 2),
            "father_w_pct": round(p_fw * 100, 2),
            "mother_h_pct": round(p_mh * 100, 2),
            "mother_w_pct": round(p_mw * 100, 2),
            "child_h_pct_mean": round(p_c * 100, 2),
            "rule": ("规则2(双亲身高同为极值)" if rule2
                     else ("规则1(平均) · 身高未知，未判极值" if not h_known
                           else "规则1(平均)")),
            "child_w_pct_mean": round(w_c * 100, 2),
            "child_w_pct_lo": round(max(0.0, w_c * 100 - noise), 2),
            "child_w_pct_hi": round(w_c * 100 + noise, 2),
            "p_small": round(p_small, 4),
            "p_big": round(p_big, 4),
            "height_at_extreme": ("max" if h_at_max else "min" if h_at_min else "none"),
            "compatible": (bool(set(fa["egg"]) & set(mo["egg"]))
                           if (fa["egg"] and mo["egg"]) else None),
        }
        out.update(hh)
        # 补一份和源 medal.py 同名的字段，方便对照/引用
        if hh.get("egg_h") is not None:
            out["child_egg_h"] = hh["egg_h"]
            out["child_egg_pct"] = hh.get("egg_pct")
            out["slots"] = hh.get("egg_slots")
        if hh.get("hatch_h") is not None:
            out["child_hatch_h"] = hh["hatch_h"]
            out["child_h_pct_slot"] = hh.get("hatch_pct")
        # 蛋重：用基础形态的蛋尺子
        if brec.get("w_egg"):
            wl, wh = brec["w_egg"]
            out["child_egg_w_kg"] = round(wl + w_c * (wh - wl), 3)
            out["egg_w_range"] = [wl, wh]
        return out

    # ---------------------------------------------------------------- 多代推演
    def generations(self, me: Dict[str, Any], target: str = "big",
                    partner_h_pct: float = 100.0, partner_w_pct: float = 98.0,
                    max_gen: int = 20) -> Dict[str, Any]:
        """逐代模拟：每代「保留最优子代 → 配一个固定百分位的配偶」重复下去。

        依据就是源 skill 的规则1：`p_child = (p父 + p母) / 2`。
        把它反复应用就是迭代收敛：

            p_n = 1 − (1 − p_0) / 2^n        （配偶固定 100% 时）

        身高还要过**两段量化**（蛋档 → 蛋% → 个体档），所以是有限步跳档；
        体重是连续量 + ±1pp 包络，判定标准放宽成「≥ 目标线」。

        ⚠ 这是**由单代公式推导**出来的模型，源 skill 本身只实测过单代；
        多代结论属于数学外推，已在此标注。
        """
        p_h = me["p_h"] if me["p_h"] is not None else 0.0
        p_w = me["p_w"] if me["p_w"] is not None else 0.0
        ph_partner = max(0.0, min(1.0, float(partner_h_pct) / 100.0))
        pw_partner = max(0.0, min(1.0, float(partner_w_pct) / 100.0))
        # 子代物种 = 母本基础形态；这里用「我」的 base 当子代尺子
        base = me["rec"].get("base") or me["name"]
        brec = self._species.get(base) or me["rec"] or {}
        h_egg = brec.get("h_egg")
        h_stage1 = brec.get("h_stage1")

        steps: List[Dict[str, Any]] = []
        hit_gen = None
        for gen in range(1, max_gen + 1):
            # 亲本身高（迭代前的值）—— 规则2 判的是「双亲」，不是子代量化后的值
            parent_h = p_h
            p_child_h = (parent_h + ph_partner) / 2.0
            hh = hatch_height(p_child_h, h_egg, h_stage1)
            p_h = hh.get("hatch_pct", 0.0) / 100.0 if hh.get("hatch_slot") is not None else p_child_h

            # 规则2：**双亲**身高都满（亲本 100% + 配偶 100%）→ 子代体重锁满端平台
            both_max = parent_h >= 0.985 and ph_partner >= 0.985
            if both_max:
                p_w = RULE2_TOP
            else:
                p_w = (p_w + pw_partner) / 2.0

            at_max = (hh.get("hatch_slot") is not None
                      and hh["hatch_slot"] == hh["hatch_slots"] - 1)
            at_min = (hh.get("hatch_slot") == 0)
            w_pct = p_w * 100.0
            steps.append({
                "gen": gen,
                "h_pct": round(p_h * 100, 1),
                "hatch_h": hh.get("hatch_h"),
                "at_max": at_max, "at_min": at_min,
                "w_pct": round(w_pct, 2),
                "rule2": both_max,
                "ok": (at_max and w_pct >= BIG_MIN) if target == "big"
                      else (at_min and w_pct <= SMALL_MAX),
                # ±1pp 包络下「有概率拿到」的判定
                "maybe": (at_max and w_pct + NOISE >= BIG_MIN) if target == "big"
                         else (at_min and w_pct - NOISE <= SMALL_MAX),
            })
            if steps[-1]["ok"] and hit_gen is None:
                hit_gen = gen
                break

        return {
            "target": target, "steps": steps, "gen": hit_gen,
            "partner_h_pct": partner_h_pct, "partner_w_pct": partner_w_pct,
            "base": base,
            "note": "由单代公式 p=(父+母)/2 迭代推导；源 skill 只实测过单代",
        }

    # ---------------------------------------------------------------- 声音奖牌
    @staticmethod
    def voice(fa_db: Optional[int], mo_db: Optional[int]) -> Dict[str, Any]:
        """子代分贝 = ⌊(父 + 母) / 2⌋（奇数和向下取整）。

        ≥ +96 → 婉转声；≤ −96 → 粗嗓门（源 skill：478 颗蛋 474 颗吻合）。
        两个分贝都要有才给结果，缺一个就返回空 dict（不猜）。
        """
        if fa_db is None or mo_db is None:
            return {}
        child = int(math.floor((int(fa_db) + int(mo_db)) / 2.0))
        return {
            "father_db": int(fa_db), "mother_db": int(mo_db),
            "child_db": child,
            "p_voice": 1.0 if child >= VOICE_ABS else 0.0,
            "p_rough": 1.0 if child <= -VOICE_ABS else 0.0,
        }

    @staticmethod
    def voice_need(fa_db: int, target: str = "voice") -> Dict[str, Any]:
        """反解：给定父本分贝，母本分贝要到多少才出声音奖牌。

        婉转声：⌊(父+母)/2⌋ ≥ 96  ⇔  父+母 ≥ 192  ⇔  母 ≥ 192 − 父
        粗嗓门：⌊(父+母)/2⌋ ≤ −96 ⇔  (父+母)/2 < −95 ⇔ 父+母 ≤ −191 ⇔ 母 ≤ −191 − 父
        （向下取整的边界差 1，这里按严格推导写）
        """
        f = int(fa_db)
        if target == "rough":
            need = -191 - f
            return {"target": "rough", "father_db": f, "need_max": need,
                    "thr": -VOICE_ABS}
        need = 192 - f
        return {"target": "voice", "father_db": f, "need_min": need,
                "thr": VOICE_ABS}

    # ---------------------------------------------------------------- 输入解析
    #: 默认值（用户口径：不填就是 100% / 100 / 100）
    DEFAULT_PCT = 100.0
    DEFAULT_DB = 100
    VOICE_MIN, VOICE_MAX = -100, 100

    def _stage_ranges(self, name: str) -> Tuple[List[Tuple[float, float]],
                                                List[Tuple[float, float]]]:
        """该精灵（含 base 基础形态）所有已知阶段的 (身高区间们, 体重区间们)。"""
        hs: List[Tuple[float, float]] = []
        ws: List[Tuple[float, float]] = []
        seen = set()
        for nm in (name, (self.get(name) or {}).get("base") or ""):
            if not nm or nm in seen:
                continue
            seen.add(nm)
            rec = self._species.get(nm) or {}
            for key, bucket in (("h_stage1", hs), ("h_egg", hs), ("w_mon", ws),
                                ("w_egg", ws)):
                rng = rec.get(key)
                if rng and len(rng) >= 2:
                    bucket.append((float(rng[0]), float(rng[1])))
        return hs, ws

    @staticmethod
    def _in(ranges: List[Tuple[float, float]], v: float) -> bool:
        for lo, hi in ranges:
            if lo - 1e-9 <= v <= hi + 1e-9:
                return True
        return False

    def fits_height(self, name: str, v: float) -> bool:
        hs, _ = self._stage_ranges(name)
        return self._in(hs, v)

    def fits_weight(self, name: str, v: float) -> bool:
        _, ws = self._stage_ranges(name)
        return self._in(ws, v)

    @classmethod
    def is_voice(cls, v) -> bool:
        """声音必须是 −100~100 的整数。"""
        if v is None:
            return False
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return False
        if abs(fv - round(fv)) > 1e-9:
            return False
        return cls.VOICE_MIN <= fv <= cls.VOICE_MAX

    def value_to_pct(self, name: str, v: float, kind: str) -> Tuple[float, bool]:
        """把输入数值统一成百分比。

        规则：带 % 的调用方已处理；这里处理不带 % 的 ——
        若落在该精灵的真实区间（米 / kg）内，就换算成百分比；
        否则原样当百分比（并返回 False 表示「没换算」）。
        返回 (百分比 0~100, 是否发生了换算)。
        """
        hs, ws = self._stage_ranges(name)
        ranges = hs if kind == "h" else ws
        for lo, hi in ranges:
            if hi > lo and lo - 1e-9 <= v <= hi + 1e-9:
                return max(0.0, min(100.0, (v - lo) / (hi - lo) * 100.0)), True
        return float(v), False

    def _one(self, name: str, toks: List[str]) -> Dict[str, Any]:
        """解析「名字后面那串 token」→ {h, w, db, notes}。

        **带 % 的按百分比**（0~100），**不带的按真实数据**（米 / kg）去比区间。
        """
        items: List[Tuple[float, bool]] = []
        for t in toks:
            s = str(t).strip()
            is_pct = s.endswith("%")
            try:
                items.append((float(s.rstrip("%")), is_pct))
            except ValueError:
                return {"error": f"「{t}」不是数字"}
        if len(items) > 3:
            return {"error": "一只精灵最多跟 3 个数字（身高 体重 声音）"}
        h = w = db = None
        notes: List[str] = []

        if len(items) == 3:
            # 三个数：按顺序 身高 / 体重 / 声音
            h_v, h_pct = items[0]
            w_v, w_pct = items[1]
            db = items[2][0]
            if not h_pct:
                h_v, _conv = self.value_to_pct(name, h_v, "h")
                if _conv:
                    notes.append(f"身高 {items[0][0]:g} 米 → {h_v:.2f}%")
            if not w_pct:
                w_v, _conv = self.value_to_pct(name, w_v, "w")
                if _conv:
                    notes.append(f"体重 {items[1][0]:g} kg → {w_v:.2f}%")
            if not (0 <= h_v <= 100):
                return {"error": f"身高 {h_v:g} 超出 0~100%"}
            if not (0 <= w_v <= 100):
                return {"error": f"体重 {w_v:g} 超出 0~100%"}
            h, w = h_v, w_v
            if items[2][1] and not (0 <= db <= 100):
                return {"error": f"声音 {db:g}% 超出 0~100"}
            if not self.is_voice(db):
                return {"error": f"声音 {db:g} 超出 −100~100（且必须是整数）"}
        elif len(items) == 2:
            h = items[0][0]
            if not items[0][1]:
                h, _conv = self.value_to_pct(name, h, "h")
                if _conv:
                    notes.append(f"身高 {items[0][0]:g} 米 → {h:.2f}%")
            v, vpct = items[1]
            if vpct:
                w = v                      # 带 % → 直接当体重%
                notes.append("第2个带 % 按体重%处理")
            elif self.fits_weight(name, v):
                w, _conv = self.value_to_pct(name, v, "w")
                if _conv:
                    notes.append(f"体重 {v:g} kg → {w:.2f}%")
            elif self.is_voice(v):
                db = v
                notes.append("第2个按声音处理")
            else:
                return {"error": f"{v:g} 既不是该精灵的合法体重，也不是合法声音"}
        elif len(items) == 1:
            v, vpct = items[0]
            if vpct:
                h = v                      # 带 % → 直接当身高%
                notes.append("带 % 按身高%处理")
            elif self.fits_height(name, v):
                h, _conv = self.value_to_pct(name, v, "h")
                if _conv:
                    notes.append(f"按身高处理（{v:g} 米 → {h:.2f}%）")
            elif self.fits_weight(name, v):
                w, _conv = self.value_to_pct(name, v, "w")
                notes.append(f"按体重处理" + (f"（{v:g} kg → {w:.2f}%）" if _conv else ""))
            elif self.is_voice(v):
                db = v
                notes.append("按声音处理")
            else:
                hs, ws = self._stage_ranges(name)
                return {"error": (f"{v:g} 对不上「{name}」的身高/体重，也不是合法声音\n"
                                  f"　身高区间 {hs or '缺数据'}　体重区间 {ws or '缺数据'}"
                                  f"　声音 −100~100 整数\n"
                                  f"　（想按百分比就写 50%）")}

        return {"name": name,
                # 一律填成实际生效值（默认 100 / 100 / 100），下游不用再判 None
                "h": float(h) if h is not None else self.DEFAULT_PCT,
                "w": float(w) if w is not None else self.DEFAULT_PCT,
                "db": int(round(db)) if db is not None else self.DEFAULT_DB,
                "h_default": h is None, "w_default": w is None, "db_default": db is None,
                "notes": notes}

    def parse_pair(self, text: str) -> Dict[str, Any]:
        """整条消息 → 两只精灵。成功 {"ok":True,"a":..,"b":..}；否则 {"error":..}。

        分段方式：从左扫 token，第一个已知精灵名 = 第一只的起点，
        下一个已知精灵名 = 第二只的起点。
        """
        raw = str(text or "").replace("+", " ").strip()
        if not raw:
            return {"error": "", "looks_like": False}
        toks = [t for t in raw.replace("　", " ").split(" ") if t]
        idx = [i for i, t in enumerate(toks) if self.get(t)]
        if not idx:
            return {"error": "", "looks_like": False}
        if idx[0] > 1:
            return {"error": "", "looks_like": False}      # 前面还有别的词，别抢
        if len(idx) < 2:
            # 只认出**一只**精灵：
            #   · 后面没数字 → 交给「裸名查询」
            #   · 后面有数字 → 配偶自动补「同种精灵的满阶（100% / 100% / 100）」
            nm = self.real_name(toks[idx[0]])
            tail = toks[idx[0] + 1:]
            if not tail:
                return {"error": "", "looks_like": False}
            one = self._one(nm, tail)
            if one.get("error"):
                return {"error": one["error"], "looks_like": True}
            mate = {
                "name": nm,
                "h": self.DEFAULT_PCT, "w": self.DEFAULT_PCT, "db": self.DEFAULT_DB,
                "h_default": True, "w_default": True, "db_default": True,
                "notes": ["配偶按同种满阶自动补（100% 身高 / 100% 体重 / 100 声音）"],
                "auto_mate": True,
            }
            return {"ok": True, "a": one, "b": mate, "auto_mate": True}
        name_a = self.real_name(toks[idx[0]])
        name_b = self.real_name(toks[idx[1]])
        seg_a = toks[idx[0] + 1: idx[1]]
        seg_b = toks[idx[1] + 1:]
        a = self._one(name_a, seg_a)
        if a.get("error"):
            return {"error": f"第一只「{name_a}」：{a['error']}", "looks_like": True}
        b = self._one(name_b, seg_b)
        if b.get("error"):
            return {"error": f"第二只「{name_b}」：{b['error']}", "looks_like": True}
        return {"ok": True, "a": a, "b": b}

    def partners(self, me: Dict[str, Any], gender: str = "公",
                 limit: int = 12) -> List[Dict[str, Any]]:
        """给一只精灵，列出能配的异性（蛋组有交集 + 能生蛋 + 只取基础形态）。"""
        my_egg = set(me["egg"] or [])
        out: List[Dict[str, Any]] = []
        for nm, rec in self._species.items():
            if nm == me["name"]:
                continue
            if "（" in nm or "(" in nm:            # 形态变体，不是基础形态
                continue
            if rec.get("breedable") is False:
                continue
            if rec.get("lowest") is False:
                continue
            other_egg = set(rec.get("egg") or [])
            if not (my_egg & other_egg):
                continue
            if gender == "公":
                fa, mo = me, self.resolve(nm)
            else:
                fa, mo = self.resolve(nm), me
            if not fa.get("egg") or not mo.get("egg"):
                continue
            res = self.pair(fa, mo)
            out.append(res)
        out.sort(key=lambda r: (-r.get("p_small", 0), -r.get("p_big", 0),
                                r.get("child_w_pct_mean", 999)))
        return out[:max(1, int(limit))]