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
NOISE = 1.0            # 子代体重% 相对双亲平均的随机包络（±pp）
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
            p_child_h = (p_h + ph_partner) / 2.0
            hh = hatch_height(p_child_h, h_egg, h_stage1)
            p_h = hh.get("hatch_pct", 0.0) / 100.0 if hh.get("hatch_slot") is not None else p_child_h

            # 规则2：双亲身高都满 → 子代体重锁满端平台
            both_max = p_h >= 0.985 and ph_partner >= 0.985
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