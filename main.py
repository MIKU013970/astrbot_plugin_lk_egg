# -*- coding: utf-8 -*-
"""洛克王国查精灵蛋（AstrBot 插件）。

纯本地图鉴查询工具，不依赖任何外部服务：
    · 查询 火花 —— 蛋组 / 进化链 / 区间数 / 身高体重 / 大块头·小块头过线极限
    · 查询 拟人组 —— 按蛋组列出全部精灵（一阶段名称）
    · 34/3.617 —— 身高体重反查可能是哪颗蛋

数据来源：插件目录下的 ranges.json（官方图鉴数据）+ icons/（精灵与蛋的图标）。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# ---------------------------------------------------------------- 出图模块
_CARD_MOD = None
_CARD_TRIED = False

#: 文字卡缓存：内容 + 配置一致就直接复用，省掉重复渲染
#: （帮助菜单、各种格式提示这些天天发同样的内容，画一次就够了）
_CARD_CACHE: Dict[str, bytes] = {}
#: 图鉴卡缓存：同一只精灵 + 同配置重复查，直接返回上次那张（图鉴数据是静态的）
_SPECIES_CACHE: Dict[str, bytes] = {}
_SPECIES_CACHE_MAX = 128
_CARD_CACHE_MAX = 64


def _card():
    """按文件路径加载同目录的 card.py。

    AstrBot 加载插件的方式各版本不同，相对导入不一定成立，
    所以直接按 __file__ 找同目录的 card.py，最稳。
    """
    global _CARD_MOD, _CARD_TRIED
    if _CARD_TRIED:
        return _CARD_MOD
    _CARD_TRIED = True
    import importlib.util
    fp = Path(__file__).with_name("card.py")
    if not fp.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("lk_egg_card", str(fp))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _CARD_MOD = m
    except Exception as e:
        logger.warning(f"[出图] card.py 加载失败，一律发文字: {type(e).__name__}: {e}")
        _CARD_MOD = None
    return _CARD_MOD


_PLUGIN_DIR = Path(__file__).resolve().parent
_PLUGIN_NAME = "astrbot_plugin_lk_egg"
VERSION = "1.59.0"


def _data_dir() -> Path:
    """按平台规范：持久化数据放 data/plugin_data/<plugin_name>/"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        return Path(get_astrbot_data_path()) / "plugin_data" / _PLUGIN_NAME
    except Exception:
        # 兼容旧版 AstrBot / 独立测试
        return Path("data") / "plugin_data" / _PLUGIN_NAME


#: 游戏角色资料缓存（等级 / 注册时间 / 家园信息），按游戏 UID 存
_ROCOM_CACHE_PATH = _data_dir() / "rocom_roles.json"
ZWSP = "\u200b"          # 零宽空格：防止适配器把首行/换行 strip 掉


@register(
    "astrbot_plugin_lk_egg",
    "MIKU013970 & DeepSeek",
    "洛克王国查精灵蛋 · 图鉴 / 蛋组 / 身高体重查询",
    "1.59.0",
)
class LkMarketNotify(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self._task: Optional[asyncio.Task] = None
        self.ranges = self._load_ranges()

    def _cfg(self, key: str, default: Any = None) -> Any:
        v = self.config.get(key, default)
        return default if v is None else v


    async def initialize(self) -> None:
        # 预生成卡片放最前面：第一条 /帮助 不用等冷启动
        try:
            self._warm_task = asyncio.create_task(self._prewarm_cards())
        except Exception as e:                      # 极端情况：没有事件循环
            logger.warning(f"[出图] 预生成启动失败: {type(e).__name__}: {e}")
            self._warm_task = None

    async def terminate(self) -> None:
        _wt = getattr(self, "_warm_task", None)
        if _wt:
            _wt.cancel()
            try:
                await _wt
            except (asyncio.CancelledError, BaseException):
                pass
            self._warm_task = None
        logger.info("[查蛋] 已停止")


    # ---------------------------------------------------------------- 图鉴区间
    #: 官方数据里缺性别比例的，这里手工补（键写蛋名，值是标准写法）。
    #: 只在原数据是「未知」或空的时候才覆盖 —— 官方以后补齐了就以官方为准。
    GENDER_OVERRIDE = {
        "云梦豚的蛋": "雄性 50% / 雌性 50%",
        "长江豚的蛋": "雄性 50% / 雌性 50%",
        "江豚的蛋": "雄性 50% / 雌性 50%",
    }

    def _apply_gender_fix(self, data: Dict[str, Any]) -> None:
        """把 GENDER_OVERRIDE 补进 ranges（就地改）。

        ranges.json 是从官方数据导出的，官方那边少数精灵没有 genderRatio
        （petBaseData.json 里就是 null），导出后就成了「未知」。
        这里兜一层，将来就算换一份新导出的 ranges.json 也不会再丢。
        """
        if not self.GENDER_OVERRIDE:
            return
        for rec in (data.get("species") or []):
            names = [str(rec.get("精灵") or ""), str(rec.get("蛋名") or "")]
            for v in (rec.get("variants") or [rec]):
                names.append(str(v.get("蛋名") or ""))
                if str(v.get("公母") or "").strip() in ("", "未知"):
                    want = next((self.GENDER_OVERRIDE[x] for x in
                                 (str(v.get("蛋名") or ""), str(rec.get("精灵") or ""))
                                 if x in self.GENDER_OVERRIDE), None)
                    if want:
                        v["公母"] = want
            if str(rec.get("公母") or "").strip() in ("", "未知"):
                want = next((self.GENDER_OVERRIDE[x] for x in names
                             if x in self.GENDER_OVERRIDE), None)
                if want:
                    rec["公母"] = want

    def _load_ranges(self) -> Dict[str, Any]:
        """读同目录 ranges.json：蛋的体重区间 / 过线值 / 蛋组 / 进化链。"""
        try:
            p = _PLUGIN_DIR / "ranges.json"
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                self._apply_gender_fix(data)
                sp = data.get("species") or []
                return {"species": {r["精灵"]: r for r in sp},
                        "alias": data.get("alias") or {},
                        "count": len(sp)}
        except Exception as e:
            logger.warning(f"[市集推送] 读取图鉴失败: {e}")
        return {"species": {}, "alias": {}, "count": 0}

    def _find_species(self, q: str, strict: bool = False) -> Optional[Dict[str, Any]]:
        """精灵名 / 蛋名 / 进化链上任意名字 / 裸名字 → 记录（含多形态）。

        strict=True 时不做模糊匹配 —— 概率接口返回的是**准确**名字，
        用模糊匹配会有误伤（比如数据库里没有「储水地鼠」，模糊匹配会命中「地鼠」）。
        """
        q = str(q or "").strip()
        if not q:
            return None
        sp = self.ranges.get("species") or {}
        alias = self.ranges.get("alias") or {}
        if q in sp:
            return sp[q]
        if alias.get(q) in sp:
            return sp[alias[q]]
        # 去/加「蛋」再试
        for cand in (q[:-1] if q.endswith("蛋") else q + "蛋",
                     q.replace("的蛋", ""), q + "的蛋"):
            if alias.get(cand) in sp:
                return sp[alias[cand]]
            if cand in sp:
                return sp[cand]
        if strict:
            return None
        # 模糊
        for name, rec in sp.items():
            if q in name or name in q:
                return rec
        for nm, target in alias.items():
            if (q in nm or nm in q) and target in sp:
                return sp[target]
        return None


    async def _species_payload(self, rec: Dict[str, Any], query: str = ""):
        """图鉴正文 + 图片；返回 (文字版, 图片版或 None)。"""
        text = self._render_species(rec)
        png = None
        if self._image_reply_on():
            try:
                png = await asyncio.to_thread(self._species_card_cached, rec, None, query)
            except Exception as e:
                logger.warning(f"[出图] 图鉴卡失败，退回文字: {type(e).__name__}: {e}")
        return text, png

    def _species_card_cached(self, rec: Dict[str, Any],
                             items: Optional[List[Dict[str, Any]]] = None,
                             query: str = "") -> Optional[bytes]:
        """图鉴卡带缓存：同一只精灵 + 同配置重复查，直接返回上次那张。

        图鉴数据是静态的（不像行情会变），缓存非常安全。改卡片配置会自动失效。
        """
        kw = self._card_kwargs()
        _items_sig = "|".join(str((it or {}).get("key") or "") for it in (items or []))
        raw = "\x1f".join([
            str(rec.get("精灵") or ""), str(query or ""), _items_sig,
            str(kw.get("theme") or ""), str(kw.get("font_path") or ""),
            str(kw.get("width")), str(kw.get("font_size")),
        ])
        key = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        hit = _SPECIES_CACHE.get(key)
        if hit is not None:
            return hit
        png = self._species_card(rec, items, query)
        if png:
            while len(_SPECIES_CACHE) >= _SPECIES_CACHE_MAX:
                try:
                    _SPECIES_CACHE.pop(next(iter(_SPECIES_CACHE)))
                except StopIteration:
                    break
            _SPECIES_CACHE[key] = png
        return png

    async def _render_species_quoted(self, rec: Dict[str, Any]) -> str:
        """只要文字版（兼容老调用）。"""
        text, _ = await self._species_payload(rec)
        return text

    def _render_species(self, r: Dict[str, Any]) -> str:
        """渲染图鉴。默认只显示蛋；show_body=true 时追加本体（成体）数据。"""
        show_body = bool(self._cfg("show_body", False))
        variants = r.get("variants") or []
        if not variants:                      # 兼容旧结构
            variants = [r]
        n = len(variants)
        first = variants[0]
        head = (f"{r['精灵']}（{first.get('蛋名') or r.get('蛋名') or ''}）"
                f"图鉴 {first.get('图鉴编号') or r.get('图鉴编号') or ''}")
        if n > 1:
            head += f" · {n} 种形态"
        else:
            bins = first.get("区间数")
            if bins not in (None, "", 0):
                head += f" · 区间 {bins}"

        lines = [head,
                 f"蛋组：{r['蛋组']}",
                 f"公母比例：{r.get('公母') or '未知'}",
                 f"进化链：{r['进化链']}"]

        def egg_lines(v, prefix="") -> list:
            out = []
            if prefix:
                _b = v.get('区间数')
                out.append(f"{prefix}{v.get('形态') or ''}"
                           + (f" · 区间 {_b}" if _b not in (None, '', 0) else ""))
                # 多蛋精灵：蛋名和默认形态不一样时要标出来（比如鸭吉吉的「燃了鸭」）
                egg = str(v.get("蛋名") or "")
                first_egg = str((variants[0] or {}).get("蛋名") or "")
                if egg and egg != first_egg:
                    out.append(f"   蛋名：{egg}（和其它形态不同）")
                out.append(f"   蛋：{v['身高区间']} · {v['体重区间']}")
                # 异色形态来自官方蛋数据，没有「过线值」，有才打
                if v.get("小块头过线值") and v.get("大块头过线值"):
                    out.append(f"       小块头 {v['小块头过线值']} / 大块头 {v['大块头过线值']}")
            else:
                out.append(f"身高区间：{v['身高区间']}")
                out.append(f"体重区间：{v['体重区间']}")
                if v.get("小块头过线值") is not None:
                    out.append(f"小块头：过线 {v['小块头过线值']} · 极限 {v['小块头极限值']}")
                if v.get("大块头过线值") is not None:
                    out.append(f"大块头：过线 {v['大块头过线值']} · 极限 {v['大块头极限值']}")
            return out

        def body_lines(v, prefix="") -> list:
            b = v.get("本体") or {}
            if not b:
                return []
            if prefix:
                return [f"   本体：{b.get('身高区间') or '—'} · {b.get('体重区间')}",
                        f"       小块头 {b.get('小块头过线值')} / 大块头 {b.get('大块头过线值')}"]
            return ["—— 本体（成体）——",
                    f"身高区间：{b.get('身高区间') or '—'}",
                    f"体重区间：{b.get('体重区间')}",
                    f"小块头：过线 {b.get('小块头过线值')} · 极限 {b.get('小块头极限值')}",
                    f"大块头：过线 {b.get('大块头过线值')} · 极限 {b.get('大块头极限值')}"]

        if n == 1:
            lines += egg_lines(first)
            if show_body:
                lines += body_lines(first)
        else:
            for i, v in enumerate(variants, 1):
                lines += egg_lines(v, prefix=f"{i}. ")
                if show_body:
                    lines += body_lines(v, prefix=f"{i}. ")

        return "\n".join(lines)

    def _stage1_path(self, name: str, form: str = "") -> Optional[str]:
        """一阶段精灵图（查蛋结果要显示一阶段，不是最终形态）。"""
        c = _card()
        if c is None:
            return None
        return c.icon_path("pet_stage1", name, form) or c.icon_path("pet", name, form)

    def _form_own_dex(self, rec: Dict[str, Any], v: Dict[str, Any]) -> str:
        """形态自己有独立图鉴号就返回那个号（= 活动专属形态），否则返回空串。

        睡铃雪影娃娃（440）、果实立方人（466）都挂在别的精灵下面，但图鉴号是独立的，
        这类形态只能活动获取、没有市售蛋区间，卡片上不该显示「区间」。
        """
        if not isinstance(v, dict):
            return ""
        form = str(v.get("形态简称") or "").strip()
        my_dex = str(v.get("图鉴编号") or "").strip()
        if not my_dex or not form:
            return ""
        # 本体（第一个非异色形态）的图鉴号才是这条记录的号
        base_dex = ""
        for x in (rec.get("variants") or []):
            if "异色" in str(x.get("蛋名") or ""):
                continue
            base_dex = str(x.get("图鉴编号") or "").strip()
            break
        if not base_dex:
            base_dex = str(rec.get("图鉴编号") or "").strip()
        return my_dex if (base_dex and my_dex != base_dex) else ""

    def _form_label(self, v: Dict[str, Any]) -> str:
        """形态显示名：有一阶段名就用它，没有（活动形态）就用形态自己的名字。"""
        s = str((v or {}).get("形态简称") or "").strip()
        if s:
            return s
        full = str((v or {}).get("形态") or "").strip()
        return re.sub(r"^.*?[（(]", "", full).rstrip("）) ").strip() or full

    def _form_sprite_path(self, rec: Dict[str, Any], v: Dict[str, Any]) -> Optional[str]:
        """形态自己有独立图鉴号 / 独立立绘时，用形态自己的图（最高形态图）。

        睡铃雪影娃娃（图鉴 440）、果实立方人（图鉴 466）这种「活动特殊形态」其实是
        最高形态那一档的立绘，出现在 大耳帽兜 / 立方人 的图鉴里时不能再用一阶段图。
        普通形态（西瓜碧玺的样子、鸭吉吉各形态…）图鉴号跟本体一样，继续走一阶段图。
        """
        c = _card()
        if c is None or not self._form_own_dex(rec, v):
            return None
        form = str(v.get("形态简称") or "").strip()
        if not form:
            return None
        name = str(rec.get("精灵") or "")
        return (c.icon_path("pet", name, form)
                or c.icon_path("pet", form)
                or c.icon_path("pet_stage1", form))

    def _card_kwargs(self) -> Dict[str, Any]:
        """把卡片相关配置整理成 card.render_card 的参数。"""
        try:
            fs = int(self._cfg("card_font_size", 18))
        except (TypeError, ValueError):
            fs = 18
        try:
            wd = int(self._cfg("card_width", 700))
        except (TypeError, ValueError):
            wd = 700
        return {
            "card_version": VERSION,
            "theme": str(self._cfg("card_theme", "") or ""),
            "font_path": str(self._cfg("card_font_path", "") or ""),
            "width": max(320, min(1200, wd)),
            "font_size": max(10, min(40, fs)),
        }

    def _image_reply_on(self) -> bool:
        """默认出图。配置关掉、或者环境里没 Pillow / 没字体，就发文字。"""
        v = self._cfg("reply_as_image", True)
        if isinstance(v, str):
            on = v.strip().lower() not in ("", "0", "false", "no", "off")
        else:
            on = bool(v)
        if not on:
            return False
        c = _card()
        return bool(c is not None and c.available())

    def _species_card(self, rec: Dict[str, Any],
                      items: Optional[List[Dict[str, Any]]] = None,
                      query: str = "") -> Optional[bytes]:
        """图鉴卡。任何异常都往上抛，由 _species_payload 退回文字。

        query 用来在多形态里挑对形态（比如查「鸭吉吉的蛋蛋」要显示「燃了鸭」）。
        """
        c = _card()
        if c is None or not c.available():
            return None
        variants = rec.get("variants") or [rec]
        v = self._pick_variant(rec, query) if query else (variants[0] if variants else {})
        eggs = self._distinct_eggs(rec)
        dex2 = v.get("图鉴编号") or rec.get("图鉴编号") or ""
        rows = self._species_rows(rec, dex2, query)
        boxes = None
        # 只有能生蛋的形态参与「区间 / 过线」这套算法；活动获取的那种（睡铃雪影娃娃）
        # 买不到蛋，身高体重和过线值都不印，也就不参与底下的彩色框判断
        _rf = [x for x in variants
               if "异色" not in str(x.get("蛋名") or "")
               and not self._form_own_dex(rec, x)]
        # 单形态，或多形态但数据全同 → 都要出彩色框
        _bsig = {(str(x.get("身高区间") or ""), str(x.get("体重区间") or ""),
                  str(x.get("区间数") or "")) for x in _rf}
        if len(_rf) <= 1 or len(_bsig) == 1:
            _bv = _rf[0] if _rf else v          # 别覆盖 v：它决定头部图标和副标题
            boxes = [
                ("小块头",
                 f"过线 {_bv.get('小块头过线值') or '—'}  极限 {_bv.get('小块头极限值') or '—'}",
                 (78, 138, 186)),
                ("大块头",
                 f"过线 {_bv.get('大块头过线值') or '—'}  极限 {_bv.get('大块头极限值') or '—'}",
                 (194, 88, 40)),
            ]
        table = None
        if items:
            table = {
                "columns": ["", "今日成交", "中位价", "最低积分", "在售"],
                "items": [(str(it.get("key") or ""),
                           str(it.get("today", 0)),
                           "-" if it.get("median") in (None, "")
                           else str(it.get("median")),
                           _mark_low(it.get("low", 0), it.get("median")),
                           str(it.get("listed", 0))) for it in items[:6]],
            }
        dex = v.get("图鉴编号") or rec.get("图鉴编号") or ""
        _real_v = [x for x in variants
                   if "异色" not in str(x.get("蛋名") or "")
                   and not self._form_own_dex(rec, x)]
        # 副标题的图鉴号：以「能生蛋的那只」为主；形态自己是独立号就补在后面
        _base_dex = ""
        for _x in _real_v:
            _base_dex = str(_x.get("图鉴编号") or "").strip()
            if _base_dex:
                break
        _base_dex = _base_dex or str(rec.get("图鉴编号") or "").strip()
        _own = self._form_own_dex(rec, v)
        _dex_txt = _base_dex or str(v.get("图鉴编号") or "").strip()
        if _base_dex and _own and _own != _base_dex:
            _dex_txt = f"{_base_dex}（形态 {_own}）"
        # 右上角「区间 N」：取这张图鉴**主蛋**（第一个能生蛋的形态）的区间数。
        # 每个形态行里的区间数 / 过线值 / 极限值都是**它自己那颗蛋**的数据，互相不借用；
        # 睡铃雪影娃娃 / 果实立方人 这种只能活动获取的蛋没有市售区间，那行就不印区间数。
        _stage_v = next((x for x in _real_v if not self._form_own_dex(rec, x)), None)
        if _stage_v is None:
            _stage_v = _real_v[0] if _real_v else v
        bins = _stage_v.get("区间数")
        pet_name = str(rec.get("精灵") or "")
        tiles = None
        _tile_icon = int(self._card_kwargs().get("tile_icon") or 64)
        _head_icon = 0
        _real_forms = [v for v in variants
                       if "异色" not in str(v.get("蛋名") or "")]
        # 底部图标格：只给真·多形态（>=2 个非异色形态）；纯异色/单形态不出格
        if len(_real_forms) > 1:
            # 多形态：每个形态一格，格子里是「该形态的精灵图 + 该形态的蛋图」拼起来，
            # 下面写形态名和蛋名 —— 这样每个形态都看得见，不只默认那一个
            tiles = []
            _tile_icon = 140          # 多形态的格子是一张 2×2 拼图，要放大才看得清
            _head_icon = 64           # 顶部那两个（精灵+蛋）维持原大小不变
            for _v in _real_forms[:12]:
                _f = str(_v.get("形态简称") or _v.get("形态") or "")
                _pair = self._icon_grid([
                    self._form_sprite_path(rec, _v) or self._stage1_path(pet_name, _f),
                    c.icon_path("egg", str(_v.get("蛋名") or ""), _f)],
                    cell=max(60, _tile_icon // 2))
                _cap = [x for x in (_f, str(_v.get("蛋名") or "")) if x]
                tiles.append((_pair, _cap))
        elif False:      # 多蛋图标格也停用（下面不再出现任何蛋图）
            # 多种蛋：每种蛋一格，写清蛋名和它对应的形态
            tiles = []
            for egg_name, eforms in eggs:
                cap = [egg_name, f"{len(eforms)} 种形态" if eforms else ""]
                tiles.append((c.icon_path("egg", egg_name, eforms[0] if eforms else ""),
                              [x for x in cap if x]))
        return c.render_card(
            title=str(rec.get("精灵") or ""),
            subtitle=" · ".join(x for x in (
                (f"图鉴 {_dex_txt}" if _dex_txt else ""),
                ("\x01可同乘" if rec.get("同乘") else "")) if x),
            tag=(f"区间 {bins}" if bins not in (None, "", 0) else ""),
            rows=rows, boxes=boxes, table=table, tiles=tiles,
            tile_icon=_tile_icon, head_icon=_head_icon,
            head_icons=self._spe_icons(rec, v), **self._card_kwargs())

    def _icon_grid(self, paths: List[str], cell: int = 74) -> Optional[str]:
        """把最多 4 张图标拼成 2×2 一张小图（渲染器每个结果只吃一个图标路径）。

        排列按要求的顺序：普通一阶段 · 普通精灵蛋 · 异色一阶段 · 异色精灵蛋。
        """
        ps = [p for p in (paths or []) if p]
        if not ps:
            return None
        if len(ps) == 1:
            return ps[0]
        try:
            from PIL import Image as _I
            canvas = _I.new("RGBA", (cell * 2, cell * 2), (0, 0, 0, 0))
            hi_dir = Path(__file__).resolve().parent / "icons" / "hi"
            for i, p in enumerate(ps[:4]):
                # 优先用高清版（icons/hi 里 256px 的原图），否则 96px 的放大就糊
                hp = hi_dir / Path(p).name
                im = _I.open(str(hp) if hp.is_file() else p).convert("RGBA")
                im.thumbnail((cell, cell), _I.LANCZOS)
                x = (i % 2) * cell + (cell - im.width) // 2
                y = (i // 2) * cell + (cell - im.height) // 2
                canvas.paste(im, (x, y), im)
            import tempfile
            fp = Path(tempfile.gettempdir()) / "lk_egg_cards" / (
                "grid-" + hashlib.md5(f"{cell}:{ps}".encode()).hexdigest()[:12] + ".png")
            fp.parent.mkdir(parents=True, exist_ok=True)
            if not fp.is_file():
                canvas.save(fp, "PNG", optimize=True)
            return str(fp)
        except Exception as e:
            logger.warning(f"[拼图] 失败: {type(e).__name__}: {e}")
            return ps[0]

    def _identify_card(self, h: float, w: float,
                       hits: Optional[List[Dict[str, Any]]] = None,
                       note: str = "") -> Optional[bytes]:
        """身高体重查蛋的结果卡。"""
        c = _card()
        if c is None or not c.available():
            return None
        if hits is None:
            try:
                hits = self._match_species(h, w)
            except Exception:
                hits = []
        payload = []
        for x in hits[:8]:
            bins = x.get("区间数")
            payload.append({
                "name": x.get("精灵"),
                "range": (f"区间 {bins}" if bins not in (None, "", 0) else ""),
                "size": self._size_of(h, w, x),
                "egg": x.get("蛋名"),
                "groups": x.get("蛋组"),
                "sex": x.get("公母"),
                "hw": f"{x.get('身高区间')} · {x.get('体重区间')}",
            })
        hits = self._merge_hits(hits)
        hs = []
        for i, x in enumerate(hits[:6], 1):
            nm = str(x.get("精灵") or "")
            bins = x.get("区间数")
            pct = x.get("pct")
            head_v = " · ".join(y for y in (
                (f"{float(pct):.1f}%" if pct is not None else ""),
                (f"区间 {bins}" if bins not in (None, "", 0) else ""),
                str(self._size_of(h, w, x))) if y)
            subs = []
            forms = [f for f in (x.get("forms") or []) if f]
            if len(forms) > 1:
                subs.append("形态 " + " / ".join(forms))
            sub = " · ".join(y for y in (
                (f"蛋名 {x.get('蛋名')}" if x.get("蛋名") else ""),
                str(x.get("蛋组") or ""), str(x.get("公母") or "")) if y)
            if sub:
                subs.append(sub)
            hw = f"{x.get('身高区间') or ''} · {x.get('体重区间') or ''}".strip(" ·")
            if hw:
                subs.append(hw)
            form0 = (forms[0] if forms else "") or str(x.get("形态简称") or "")
            # 顺序：普通一阶段 → 普通精灵蛋 → 异色一阶段 → 异色精灵蛋
            egg_nm = str(x.get("蛋名") or "")
            four = [self._stage1_path(nm, form0), c.icon_path("egg", egg_nm, form0)]
            shiny_pet = c.icon_path("shiny", nm, form0)
            if shiny_pet:
                four.append(shiny_pet)
                # 异色蛋：蛋名带「异色」的那种（活动蛋）
                if "异色" not in egg_nm:
                    disp = self._find_species(nm)
                    for en, _fs in (self._distinct_eggs(disp) if disp else []):
                        if "异色" in en:
                            four.append(c.icon_path("egg", en, form0))
                            break
                else:
                    four.append(c.icon_path("egg", egg_nm, form0))
            hs.append((self._icon_grid(four), f"{i}. {nm}", head_v, subs))
        return c.render_card(
            title="身高体重查蛋",
            subtitle=" · ".join(y for y in (
                f"身高 {h:g} cm · 体重 {w / 1000:.3f} kg", note) if y),
            rows=([("结果", "没有匹配的精灵蛋"),
                   ("", note or "可能是输入格式不对，或者这颗蛋不是图鉴里的品种")]
                  if not hs else None),
            hits=(hs or None),
            **self._card_kwargs())


    def _temp_png(self, png: bytes) -> str:
        """落到临时文件（协议端要按路径读），顺手清掉 1 小时前的旧图。

        清理不用每次都做：攒够 50 次再扫一遍目录，省掉每次的 glob。
        """
        import tempfile
        d = Path(tempfile.gettempdir()) / "lk_egg_cards"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            d = Path(tempfile.gettempdir())
        global _TEMP_CLEAN_TICK
        _TEMP_CLEAN_TICK = (globals().get("_TEMP_CLEAN_TICK") or 0) + 1
        if _TEMP_CLEAN_TICK % 50 == 1:
            try:
                now = time.time()
                for f in d.glob("card-*.png"):
                    if now - f.stat().st_mtime > 3600:
                        f.unlink(missing_ok=True)
            except Exception:
                pass
        fp = d / f"card-{int(time.time() * 1000)}-{os.getpid() % 999:03d}.png"
        fp.write_bytes(png)
        return str(fp)

    def _result_image(self, event: Any, png: bytes, caption: str = ""):
        """把 PNG 包成 AstrBot 的结果对象，几种写法挨个试。

        caption 非空时优先用 chain 把「图 + 一行字」一起发出去。
        """
        path = self._temp_png(png)
        fn = getattr(event, "image_result", None)
        if caption:
            # 图 + 文字的链式结果（出图耗时就是这么带出来的）
            try:
                from astrbot.api.message_components import Image as _Image, Plain
                img = _Image.fromFileSystem(path)
                ch = getattr(event, "chain_result", None)
                if callable(ch):
                    return ch([img, Plain(caption)])
            except Exception as e:
                logger.warning(f"[出图] 图+耗时链式发送失败: {type(e).__name__}: {e}")
        if callable(fn):
            try:
                return fn(path)
            except Exception as e:
                logger.warning(f"[出图] image_result 不可用: {type(e).__name__}: {e}")
        try:
            from astrbot.api.message_components import Image as _Image
            img = _Image.fromFileSystem(path)
            fn = getattr(event, "chain_result", None)
            if callable(fn):
                try:
                    return fn([img])
                except Exception as e:
                    logger.warning(f"[出图] chain_result 不可用: {type(e).__name__}: {e}")
            mr = getattr(event, "make_result", None)
            if callable(mr):
                r = mr()
                for meth in ("file_image", "image"):
                    m2 = getattr(r, meth, None)
                    if callable(m2):
                        try:
                            return m2(path)
                        except Exception:
                            continue
        except Exception as e:
            logger.warning(f"[出图] 组装图片结果失败: {type(e).__name__}: {e}")
        return None

    async def _reply(self, event: Any, text: str, png: Optional[bytes],
                     t0: Optional[float] = None):
        """统一出口：能出图就出图，出不了就发文字。"""
        if png and self._image_reply_on():
            try:
                r = self._result_image(event, png)
                if r is not None:
                    return r
            except Exception as e:
                logger.warning(f"[出图] 发图失败，退回文字: {type(e).__name__}: {e}")
        return event.plain_result(text)

    # ---------------------------------------------------------------- 时间格式
    @staticmethod
    def _fmt_ts_hm(v: Any) -> str:
        """时间戳（秒/毫秒）→ 'YYYY-MM-DD HH:MM'，用于「上次离线」这种要看到时分的。"""
        try:
            n = float(v)
        except (TypeError, ValueError):
            return str(v or "")
        if n > 1e11:
            n /= 1000.0
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(n))
        except Exception:
            return str(v or "")

    @staticmethod
    def _fmt_ts(v: Any) -> str:
        """时间戳（秒/毫秒）→ 'YYYY-MM-DD'；不是数字就原样返回。"""
        try:
            n = float(v)
        except (TypeError, ValueError):
            return str(v or "")
        if n > 1e11:
            n /= 1000.0
        try:
            return time.strftime("%Y-%m-%d", time.localtime(n))
        except Exception:
            return str(v or "")

    def _card_key(self, text: str, icons=None) -> str:
        """缓存键：内容 + 图标 + 影响画面的那几个配置。改配置就不会拿到旧图。"""
        kw = self._card_kwargs()
        raw = "\x1f".join([
            str(kw.get("theme") or ""),
            str(kw.get("font_path") or ""),
            str(kw.get("width")),
            str(kw.get("font_size")),
            "|".join(str(x) for x in (icons or [])),
            str(text or ""),
        ])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _spe_icons(self, rec: Any, v: Any = None):
        """查具体精灵时标题前的图标，顺序固定：

            普通一阶段 → 普通精灵蛋 → 异色一阶段 → 异色精灵蛋

        异色那两个**只在查精灵时**出现（查蛋结果、货架、行情都不带）。
        异色宠物图来自 wiki 的 assets/pets/<id>/shiny.png；
        异色蛋图只有活动蛋才有（目前 10 只 + 龙须派那类），没有就只出 3 个。
        """
        c = _card()
        if c is None or not c.available():
            return None
        if not isinstance(v, dict) or not v:
            # 没指定形态就默认第一个形态（否则会落到"按名字"索引，
            # 而那个索引是被最后一个形态覆盖过的 —— 蹦蹦种子就错成了象牙球）
            _vs = rec.get("variants") or []
            v = _vs[0] if _vs else {}
        pet = str(v.get("精灵") or rec.get("精灵") or "")
        egg = str(v.get("蛋名") or rec.get("蛋名") or pet)
        form0 = str(v.get("形态简称") or v.get("形态") or "")
        egg_path = c.icon_path("egg", egg)
        # magic_egg 是通用占位白蛋：通行证精灵官方只有异色蛋、没有普通蛋，别拿它凑数
        if egg_path and Path(egg_path).stem == "magic_egg":
            egg_path = None
        out = [self._form_sprite_path(rec, v) or self._stage1_path(pet, form0), egg_path]
        # 异色
        try:
            _s1n = json.loads((_PLUGIN_DIR / "stage1name.json").read_text(encoding="utf-8"))
        except Exception:
            _s1n = {}
        shiny_pet = (c.icon_path("shiny", _s1n.get(pet, pet), form0)
                     or c.icon_path("shiny", pet, form0))
        if shiny_pet:
            out.append(shiny_pet)
            shiny_egg = None
            for en, _forms in self._distinct_eggs(rec):
                if "异色" in en:
                    shiny_egg = c.icon_path("egg", en, form0)
                    break
            if shiny_egg:
                out.append(shiny_egg)
        # 同一张图不重复显示：通行证精灵的「普通蛋」和「异色蛋」官方就是同一张图，
        # 归一化到 item 数字 id 再比（蛋150701 / 异色蛋150701 视为同一张）
        def _icon_key(p):
            stem = Path(p).stem
            mm = re.search(r"(\d{4,})", stem)
            base = mm.group(1) if mm else stem
            if stem.startswith("shiny"):
                return "shiny-" + base
            if stem.startswith("pet"):
                return "pet-" + base
            return base

        _seen = set()
        uniq = []
        for _p in out:
            if not _p:
                continue
            _k = _icon_key(_p)
            if _k in _seen:
                continue
            _seen.add(_k)
            uniq.append(_p)
        return uniq or None

    #: 奖牌简写（货架格子窄，写全称挤不下）
    MEDAL_SHORT = {
        "大块头": "大", "小不点": "小", "婉转声": "婉", "粗嗓门": "粗",
        "大块头 MAX": "大M", "小不点 MIN": "小m",
        "婉转声 MAX": "婉M", "粗嗓门 MAX": "粗M",
    }


    #: 角色资料缓存有效期（秒）
    ROCOM_TTL = 6 * 3600


    def _text_card(self, text: str, icons=None) -> Optional[bytes]:
        """把任意一段文字画成卡片（和查蛋同款样式）。命中缓存直接返回。"""
        c = _card()
        if c is None or not c.available():
            return None
        key = self._card_key(text, icons)
        hit = _CARD_CACHE.get(key)
        if hit is not None:
            return hit
        png = c.render_text_card(text, head_icons=icons, **self._card_kwargs())
        if png:
            while len(_CARD_CACHE) >= _CARD_CACHE_MAX:
                try:
                    _CARD_CACHE.pop(next(iter(_CARD_CACHE)))
                except (StopIteration, KeyError):
                    break
            _CARD_CACHE[key] = png
        return png

    async def _prewarm_cards(self) -> None:
        """开服先把常发的那几张图渲好，第一条 /帮助 不用等。

        Pillow 的 import、字体加载、首次渲染这些一次性开销全都在这里吃掉。
        """
        if not self._image_reply_on():
            return
        warm = [self.MENU] + list(self.FORMAT_HINTS.values())
        done = 0
        for txt in warm:
            try:
                await asyncio.to_thread(self._text_card, txt)
                done += 1
            except Exception as e:
                logger.warning(f"[出图] 预生成失败: {type(e).__name__}: {e}")
                return
        logger.info(f"[出图] 预生成完成：{done} 张，缓存共 {len(_CARD_CACHE)} 张")

    async def _say(self, event: Any, text: str, icons=None):
        """所有文字回复的统一出口：能出图就出图，出不了发文字。

        渲染放到线程里跑 —— 一张卡片 30 毫秒，同步画会把整个事件循环堵住。
        """
        png = None
        if self._image_reply_on():
            try:
                png = await asyncio.to_thread(self._text_card, text, icons)
            except Exception as e:
                logger.warning(f"[出图] 文字卡失败，退回文字: {type(e).__name__}: {e}")
        return await self._reply(event, text, png)

    async def _identify_reply(self, event: Any, hw):
        """身高体重查蛋的统一出口（识别卡也放到线程里画）。

        优先用控制台的实时孵化概率（底层是孵蛋预测接口）：
        概率低于 identify_min_pct 的候选直接不显示，免得把「理论上在区间内、
        实际几乎不可能出」的品种列出来误导人。接口不可用时退回本地区间匹配。
        """
        h, w = float(hw[0]), float(hw[1])
        hits, note = await self._identify_hits(h, w)
        png = None
        if self._image_reply_on():
            try:
                png = await asyncio.to_thread(self._identify_card, h, w, hits, note)
            except Exception as e:
                logger.warning(f"[出图] 识别卡失败: {type(e).__name__}: {e}")
        return await self._reply(event, self._render_identify(h, w, hits, note), png)

    #: 概率接口返回的文本行：「1. 里拉鳐 [大块头] (100.0%)」
    _ID_HEAD_RE = re.compile(r"^\s*\d+\.\s*(.+?)\s*\(([\d.]+)%\)\s*$")
    _ID_TAG_RE = re.compile(r"^(.*?)\s*\[(.+?)\]\s*$")

    @classmethod
    def _parse_identify_raw(cls, text: str) -> Dict[str, Dict[str, float]]:
        """解析-predict 接口的文本结果（控制台 guess 为空时兜底）。

        文本长这样：
            【0.44/17.3460】
            可能精灵：
            1. 里拉鳐 (100.0%)
            特殊精灵：
            1. 守夜烛 [大块头] (100.0%)
        """
        out: Dict[str, Dict[str, float]] = {"top": {}, "special": {}}
        section = "top"
        for raw in str(text or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("特殊精灵"):
                section = "special"
                continue
            if line.startswith("可能精灵"):
                section = "top"
                continue
            if line.startswith("未找到"):
                continue
            m = cls._ID_HEAD_RE.match(line)
            if not m:
                continue
            name = m.group(1).strip()
            try:
                pct = float(m.group(2))
            except (TypeError, ValueError):
                continue
            mt = cls._ID_TAG_RE.match(name)
            if mt:
                name = mt.group(1).strip()
            if name:
                out[section][name] = max(pct, out[section].get(name, 0.0))
        return out

    #: 孵蛋预测接口（公用的计算服务，不需要登录、不需要 key）
    IDENTIFY_API = "https://cinene.cloud/egg/api/compute"

    async def _identify_probs(self, h: float, w: float):
        """问孵蛋预测接口要这颗蛋的**真实孵化概率**。

        接口：POST https://cinene.cloud/egg/api/compute
            {"attribute": "孵蛋预测", "data": "0.27 3.0680"}   ← 米 + 千克
            返回 {"result": "【0.27/3.0680】\\n可能精灵：\\n1. 呼呼猪 (69.8%)..."}

        返回 ({"top": {名字: %}, "special": {名字: %}}, 状态)：
            "ok"    拿到候选
            "empty" 接口明确说这个身高体重对不上任何蛋
            "down"  接口连不上 / 报错（调用方退回本地区间匹配）
            "off"   配置里关掉了
        """
        empty = {"top": {}, "special": {}}
        if not bool(self._cfg("identify_use_remote", True)):
            return empty, "off"
        if h <= 0 or w <= 0:
            return empty, "empty"
        try:
            timeout = max(2.0, float(self._cfg("identify_timeout", 10)))
        except (TypeError, ValueError):
            timeout = 10.0
        # 接口要「米 + 千克」：身高 cm/100、体重 g/1000
        body = {
            "attribute": "孵蛋预测",
            "data": f"{float(h) / 100:.6f} {float(w) / 1000:.6f}",
        }
        text = ""
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
                async with sess.post(
                        self.IDENTIFY_API, json=body,
                        headers={"User-Agent": "lk-egg/1.0"}) as resp:
                    if resp.status != 200:
                        logger.warning(f"[查蛋] 预测接口 HTTP {resp.status}")
                        return empty, "down"
                    data = await resp.json(content_type=None)
            if isinstance(data, dict):
                text = str(data.get("result") or "")
        except Exception as e:
            logger.warning(f"[查蛋] 预测接口失败: {type(e).__name__}: {e}")
            return empty, "down"
        if not text:
            return empty, "down"
        res = self._parse_identify_raw(text)
        if res["top"] or res["special"]:
            return res, "ok"
        return empty, "empty"

    def _hit_from_name(self, name: str) -> Optional[Dict[str, Any]]:
        """把概率接口给的精灵名换成本地图鉴的一条候选（带蛋名/蛋组/区间）。

        接口返回的是准确名字，这里用严格匹配：数据库里没有就直接跳过，
        不要拿模糊匹配凑（「储水地鼠」不能被凑成「地鼠」）。
        """
        rec = self._find_species(name, strict=True)
        if not rec:
            logger.info(f"[查蛋] 概率接口给了「{name}」，但本地图鉴没有这条记录，已跳过")
            return None
        v = self._pick_variant(rec, name)
        return {
            "形态": v.get("形态") or "", "形态简称": v.get("形态简称") or "",
            "精灵": rec.get("精灵"), "蛋名": v.get("蛋名") or rec.get("蛋名"),
            "身高区间": v.get("身高区间") or "", "体重区间": v.get("体重区间") or "",
            "区间数": v.get("区间数"), "蛋组": rec.get("蛋组"),
            "公母": rec.get("公母") or v.get("公母") or "未知", "area": 0,
        }

    async def _identify_hits(self, h: float, w: float):
        """身高体重 → (候选列表, 说明文案)。有实时概率就按概率来，否则退回本地匹配。"""
        local = self._match_species(h, w)
        probs, state = await self._identify_probs(h, w)
        if state == "ok":
            try:
                min_pct = float(self._cfg("identify_min_pct", 1.0))
            except (TypeError, ValueError):
                min_pct = 1.0
            show_special = bool(self._cfg("identify_show_special", False))

            def _collect(section: str) -> List[Dict[str, Any]]:
                acc: List[Dict[str, Any]] = []
                for nm, pct in sorted((probs.get(section) or {}).items(),
                                      key=lambda kv: -kv[1]):
                    if pct < min_pct:
                        continue
                    item = self._hit_from_name(nm)
                    if item:
                        item["pct"] = pct
                        if section == "special":
                            item["special"] = True
                        acc.append(item)
                return acc

            tail = "" if min_pct <= 0 else f" · 低于 {min_pct:g}% 的已隐藏"
            out = _collect("top")
            if show_special:
                # 开关打开：普通 + 特殊一起列，特殊那几行带标记
                special = _collect("special")
                if out and special:
                    return out + special, f"实时孵化概率（含特殊精灵）{tail}"
                if out:
                    return out, f"实时孵化概率{tail}"
                if special:
                    return special, f"特殊精灵（不在普通蛋池）· 实时概率{tail}"
                return [], f"实时概率里没有超过 {min_pct:g}% 的候选"
            # 默认关闭：完全忽略特殊精灵
            if out:
                return out, f"实时孵化概率{tail}"
            return [], (f"实时概率里没有超过 {min_pct:g}% 的普通蛋候选"
                        f"（特殊精灵默认不显示，可在配置里打开 identify_show_special）")
        if state == "empty":
            return [], "接口没找到匹配的精灵（也可能刚好卡在区间边界，比如正好等于上限）"
        if state == "off":
            return local, "按本地区间匹配（实时概率已在配置里关闭）"
        return local, "概率接口不可用，下面是本地区间匹配（可能含低概率品种）"


    def _parse_hw(self, text: str):
        """'34/3.617'、'34 3.617'、'0.34 3.617'、'34cm 3.617kg' → (34.0, 3617.0)

        分隔符用 / 空格 逗号 x 都行；带单位（cm / m / kg / g / 克）也认，
        甚至「44cm19.8kg」这种不加空格的写法。
        身高小于 4 的一律按「米」算（游戏里显示的就是 0.32 这种），×100 转成厘米；
        图鉴里最矮的蛋是 4 cm，所以这个阈值不会误伤。
        体重带小数点或 <100 按 kg，否则按克。
        """
        import re as _re
        s = str(text or "").strip()
        if not s:
            return None
        # 把单位统一去掉，但记住身高是不是「米」
        h_is_m = False
        m_h = _re.search(r"(\d+(?:\.\d+)?)\s*(m|cm|厘米|米)", s, _re.I)
        if m_h:
            h_is_m = m_h.group(2).lower() in ("m", "米")
        s = _re.sub(r"\s*(cm|m|kg|g|克|厘米|米|千克|公斤)\s*", " ", s, flags=_re.I)
        s = s.replace("／", "/").strip()
        m = _re.match(r"^\s*(\d+(?:\.\d+)?)\s*[/\s,，、x×*]?\s*(\d+(?:\.\d+)?)\s*$", s, _re.I)
        if not m:
            return None
        h = float(m.group(1))
        if h_is_m or h < 4:            # 游戏里给的是米
            h *= 100
        w_raw = float(m.group(2))
        # 带小数点 或 <100 当成 kg，否则当克
        if "." in m.group(2) or w_raw < 100:
            w = w_raw * 1000
        else:
            w = w_raw
        if not (1 <= h <= 400) or not (1 <= w <= 500000):
            return None
        return h, w

    def _match_species(self, h: float, w: float, limit: int = 8):
        """在 ranges.json 里找身高体重都落在区间内的形态"""
        out = []
        sp = self.ranges.get("species") or {}
        for rec in (sp.values() if isinstance(sp, dict) else sp):
            for v in (rec.get("variants") or [rec]):
                try:
                    hl, hh = [float(x) for x in
                              v["身高区间"].replace(" cm", "").split("~")]
                    wl, wh = [float(x) * 1000 for x in
                              v["体重区间"].replace(" kg", "").split("~")]
                except Exception:
                    continue
                if not (hl <= h <= hh and wl <= w <= wh):
                    continue
                # 位置（0=下限 1=上限）
                hp = (h - hl) / (hh - hl) if hh > hl else 0.5
                wp = (w - wl) / (wh - wl) if wh > wl else 0.5
                out.append({
                    "形态": v.get("形态") or "",
                    "形态简称": v.get("形态简称") or "",
                    "area": (hh - hl) * (wh - wl) / 1000,
                    "公母": rec.get("公母") or v.get("公母") or "未知",
                    "精灵": rec.get("精灵"), "蛋名": v.get("蛋名"),
                    "身高区间": v["身高区间"], "体重区间": v["体重区间"],
                    "区间数": v.get("区间数"), "蛋组": rec.get("蛋组"),
                    "hp": hp, "wp": wp,
                })
        # 排序：区间越窄越像（面积小的优先），其次越靠中间越可能
        out.sort(key=lambda x: (x["area"], abs(x["hp"] - 0.5) + abs(x["wp"] - 0.5)))
        return out[:limit]

    def _size_of(self, h: float, w: float, rec) -> str:
        """这颗蛋相对该形态是大块头 / 小块头 / 普通"""
        wl, wh = [float(x) * 1000 for x in rec["体重区间"].replace(" kg", "").split("~")]
        sl = round((wl * 98 + wh * 2) / 100)
        bl = round((wl * 2 + wh * 98) / 100)
        if w >= bl:
            return f"大块头（≥{bl / 1000:.3f}kg）"
        if w <= sl:
            return f"小块头（≤{sl / 1000:.3f}kg）"
        return "普通体型"

    def _render_identify(self, h: float, w: float,
                         hits: Optional[List[Dict[str, Any]]] = None,
                         note: str = "") -> str:
        if hits is None:
            hits = self._match_species(h, w)
        head = f"身高 {h:g} cm · 体重 {w / 1000:.3f} kg"
        if not hits:
            return (f"{head}\n没有匹配的精灵蛋。\n"
                    f"※ {note or '可能是输入格式不对，或者这颗蛋不是图鉴里的品种'}")
        lines = [head, f"可能孵出（{len(hits)} 种）："]
        for i, x in enumerate(self._merge_hits(hits), 1):
            size = self._size_of(h, w, x)
            bins = x.get("区间数")
            bits = []
            pct = x.get("pct")
            if pct is not None:
                bits.append(f"{float(pct):.1f}%")
            if bins not in (None, "", 0):
                bits.append(f"区间 {bins}")
            bits.append(size)
            forms = [f for f in (x.get("forms") or []) if f]
            if len(forms) > 1:
                bits.append("形态 " + " / ".join(forms))
            if x.get("special"):
                bits.append("特殊")
            lines.append(f"{i}. {x['精灵']} · " + " · ".join(bits))
            lines.append(f"   蛋名 {x['蛋名']} · 蛋组 {x['蛋组']} · {x.get('公母') or '未知'}")
            lines.append(f"   身高 {x['身高区间']} · 体重 {x['体重区间']}")
        if note:
            lines.append(f"※ {note}")
        return "\n".join(lines)


    MENU = (
        "🥚 洛克王国查蛋 · 可用指令\n"
        "—————— 查蛋 ——————\n"
        "查询 火花               区间数/蛋组/进化链/身高体重/过线值\n"
        "查蛋 火花               跟「查询」完全一样\n"
        "/查询火花               不加空格也行（前面带斜线）\n"
        "查询 火神               进化链上任意名字都行\n"
        "查询 鸭吉吉             多形态会列出每种规格\n"
        "查询 拟人组             按蛋组列全部精灵（一阶段名称）\n"
        "查询 拟人 / 查询 海洋    不带「组」也认\n"
        "查询 组                 看有哪些蛋组、各多少只\n"
        "—————— 身高体重查蛋 ——————\n"
        "0.32 2.12               身高体重查蛋（空格分隔，身高按米算）\n"
        "34/3.617                身高厘米 + 体重千克（斜杠也行）\n"
        "34 3617                 整数体重按克算\n"
        "（会自动显示每种可能蛋的真实孵化概率，低于 1% 的默认隐藏）\n"
        "—————— 其它 ——————\n"
        "UID 503671642            用游戏 UID 查等级/在线/家园/收集度\n"
        "帮助 / 菜单              看这张卡片\n"
        "诊断                     管理员用：查图片链路哪里挂了\n"
        "回复默认都是图片         配置里关掉 reply_as_image 就发文字\n"
        "（提示：直接发精灵名字的功能已关闭，请用「查询 名字」）\n"
    )

    FORMAT_HINTS = {
        "查询": "格式：查询 火花（查蛋 也一样）\n"
                "进化链上任意名字都行：查询 火神 / 查询 焰火\n"
                "查蛋组：查询 拟人组 / 查询 拟人 / 查询 海洋",
        "查蛋": "格式：查蛋 火花\n和「查询」完全一样，斜杠可加可不加",
    }


    # ---------------------------------------------------------------- UID 查角色
    #: 游戏角色资料缓存有效期内不重复拉（秒）
    ROCOM_TTL = 6 * 3600
    #: UID 命令词：UID / 查UID / 游戏ID，后面跟纯数字
    _UID_CMD_RE = re.compile(r"^\s*(?:查\s*)?(?:[Uu][Ii][Dd]|游戏[Ii][Dd])\s*(\d{5,15})\s*$")

    @staticmethod
    def _rocom_fields(payload: Any) -> List[tuple]:
        """把 rocom 能拿到的字段**尽量全**地整理成行。

        来源：POST /api/v1/games/rocom/ingame/player/card
                data.register_timestamp / online / topic_point /
                data.player_card_brief_info.card_signature /
                  card_handbook_collect_num / card_fashion_bond_collect_num /
                  card_pet_info.collected_glass_pet_count / collected_shining_pet_count
              POST /api/v1/games/rocom/ingame/home/info
                result.home_info.friend_home_brief_info.home_name / home_level /
                  room_level / home_comfort_level / home_experience / home_pet_count
        """
        p = payload if isinstance(payload, dict) else {}
        card = p.get("card") if isinstance(p.get("card"), dict) else {}
        player = p.get("player") if isinstance(p.get("player"), dict) else {}
        home = (p.get("home") if isinstance(p.get("home"), dict) else {}) or \
            (player.get("home_info") if isinstance(player.get("home_info"), dict) else {})
        brief = card.get("player_card_brief_info") if isinstance(
            card.get("player_card_brief_info"), dict) else {}
        pet = brief.get("card_pet_info") if isinstance(
            brief.get("card_pet_info"), dict) else {}
        out: List[tuple] = []

        def add(label: str, val: Any, ts: bool = False, unit: str = "") -> None:
            if val in (None, "", [], {}):
                return
            out.append((label, (LkMarketNotify._fmt_ts(val) if ts else str(val)) + unit))

        def yn(label: str, val: Any) -> None:
            if val is None:
                return
            out.append((label, "是" if val else "否"))

        GENDER = {1: "雄性", 2: "雌性"}
        add("角色等级", player.get("level"))
        add("世界等级", player.get("world_level"))
        add("性别", GENDER.get(player.get("gender"), player.get("gender")))
        add("注册时间", player.get("regist_date") or card.get("register_timestamp"),
            ts=True)
        online = player.get("online")
        if online is None:
            online = card.get("online")
        if online is not None:
            out.append(("在线", "在线" if online else "离线"))
        _lo = player.get("last_logout_time")
        if _lo not in (None, "", 0):
            out.append(("上次离线", LkMarketNotify._fmt_ts_hm(_lo)))
        add("个性签名", player.get("signature") or brief.get("card_signature"))
        add("图鉴收集", brief.get("card_handbook_collect_num")
            or player.get("card_handbook_collect_num"), unit=" 种")
        add("时装羁绊", brief.get("card_fashion_bond_collect_num"), unit=" 个")
        add("炫彩精灵数", pet.get("collected_glass_pet_count"), unit=" 只")
        add("异色精灵数", pet.get("collected_shining_pet_count"), unit=" 只")
        add("话题点", card.get("topic_point"))
        yn("好友", card.get("is_friend"))
        yn("被拉黑", card.get("is_black_role"))
        add("家园名称", home.get("home_name"))
        add("家园等级", home.get("home_level"))
        add("房间等级", home.get("room_level"))
        add("家园舒适度", home.get("home_comfort_level"))
        add("家园好感/经验", home.get("home_experience"))
        add("家园精灵数", home.get("home_pet_count"), unit=" 只")
        return out

    def _rocom_cache(self) -> Dict[str, Any]:
        try:
            d = json.loads(_ROCOM_CACHE_PATH.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _rocom_save(self, cache: Dict[str, Any]) -> None:
        try:
            _ROCOM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _ROCOM_CACHE_PATH.write_text(
                json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[角色资料] 缓存写盘失败: {type(e).__name__}: {e}")

    def _rocom_on(self) -> bool:
        return bool(str(self._cfg("rocom_api_key", "") or "").strip())

    async def _rocom_call(self, path: str, game_id: str,
                          key: str, base: str) -> Optional[Dict[str, Any]]:
        """打一发 rocom 接口：POST 拿 task_id → 轮询 tasks/{id} → 返回 data 层。"""
        hdr = {"X-API-Key": key, "Content-Type": "application/json"}
        url = base + path
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=12)) as s:
                async with s.post(url, json={"uid": str(game_id), "wait_ms": 6000},
                                  headers=hdr) as r:
                    if r.status not in (200, 202):
                        logger.warning(f"[角色资料] {path} {game_id} → HTTP {r.status}")
                        return None
                    env = await r.json(content_type=None)
        except Exception as e:
            logger.warning(f"[角色资料] {path} 请求失败: {type(e).__name__}: {e}")
            return None
        if not isinstance(env, dict):
            return None
        data = env.get("data")
        if not isinstance(data, dict):
            data = env if env.get("code") in (0, None) else {}
        tid = str(((data or {}).get("meta") or {}).get("task_id") or "")
        if not tid:
            return data or None
        # player/card 第一次就直接把完整资料塞在 data 里了（只是顺带带了个 task_id），
        # 这种情况别去排队轮询 —— 一轮询就只剩任务结果，图鉴收集那些字段就丢了。
        if path.endswith(("/player/card", "/player/search")) and any(
                k not in ("meta", "ret_info", "source",
                          "cache_ttl_seconds", "cached_age_seconds")
                for k in data):
            return data
        for _ in range(8):                      # 每 1.5 秒探一次，最多 ~12 秒
            await asyncio.sleep(1.5)
            try:
                async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=10)) as s:
                    async with s.get(
                            base + "/api/v1/games/rocom/ingame/tasks/" + tid,
                            headers=hdr) as r:
                        if r.status != 200:
                            return None
                        task = await r.json(content_type=None)
            except Exception:
                return None
            if not isinstance(task, dict):
                return None
            # 轮询返回同样是 {code, data, message} 信封，status/result 在 data 里
            body = task.get("data") if isinstance(task.get("data"), dict) else task
            st = str(body.get("status") or "").lower()
            if st in ("failed", "error", "cancelled", "canceled"):
                logger.warning(f"[角色资料] {path} 任务失败: {st}")
                return None
            res = body.get("result")
            if isinstance(res, dict):
                return res
        return None

    async def _rocom_fetch(self, game_id: str) -> Optional[Dict[str, Any]]:
        """用游戏 UID 拉角色资料。

        实测结构（2026-10，用真 key 验证过）：
          POST /ingame/player/card → data.register_timestamp / online / card_signature
          POST /ingame/home/info   → task → result.home_info.friend_home_brief_info
                                     {home_name, home_level, room_level,
                                      home_comfort_level, home_experience}
        角色等级那两个接口都没给（player/search 现在返回 502）。
        """
        key = str(self._cfg("rocom_api_key", "") or "").strip()
        if not key or not game_id:
            return None
        base = str(self._cfg("rocom_api_base", "")
                   or "https://wegame.shallow.ink").rstrip("/")
        out: Dict[str, Any] = {}
        # player/search 一次就带全：等级 / 世界等级 / 上次离线 / 性别 / 家园，
        # player/card 补 图鉴收集、炫彩·异色精灵数、话题点。
        # 两个并发打，各 1.2 秒左右；**不再调 home/info**（那个要走任务队列，慢 4 秒，
        # 而且家园数据 player/search 里已经有了）。
        search, card = await asyncio.gather(
            self._rocom_call("/api/v1/games/rocom/ingame/player/search",
                             game_id, key, base),
            self._rocom_call("/api/v1/games/rocom/ingame/player/card",
                             game_id, key, base),
            return_exceptions=True)
        if isinstance(search, Exception):
            search = None
        if isinstance(card, Exception):
            card = None
        if isinstance(search, dict):
            pi = search.get("player_info")
            if isinstance(pi, dict) and pi:
                out["player"] = pi
        if isinstance(card, dict) and card:
            out["card"] = card
        # 家园：优先用 player_info.home_info（秒回），没有再退到 home/info
        home = (out.get("player") or {}).get("home_info")
        if isinstance(home, dict) and home:
            out["home"] = home
        else:
            h = await self._rocom_call(
                "/api/v1/games/rocom/ingame/home/info", game_id, key, base)
            if isinstance(h, dict) and h:
                hi = h.get("home_info") if isinstance(h.get("home_info"), dict) else {}
                brief = (hi.get("friend_home_brief_info")
                         or hi.get("home_brief_info") or {})
                if brief:
                    out["home"] = brief
        return out or None

    async def _rocom_maybe_refresh(self, data: Dict[str, Any],
                                   wait: float = 3.0) -> None:
        """角色资料：**只同步等 player/card**（1.3 秒，拿注册时间/在线/签名），
        家园那一路（3.8 秒）转后台，下次查就有了。

        为啥这么拆：实测 home/info 要走任务队列，比 card 慢两三倍，
        为了它让用户多等 4 秒不划算。
        """
        if not self._rocom_on():
            return
        roles = [r for r in (data.get("roles") or []) if isinstance(r, dict)]
        gid = str((roles[0] if roles else {}).get("gameId") or "").strip()
        if not gid:
            return
        cache = self._rocom_cache()
        rec = cache.get(gid) or {}
        fresh = time.time() - float(rec.get("at") or 0) < self.ROCOM_TTL
        if rec.get("fields") and fresh and rec.get("full"):
            return                                  # 完整数据还热，直接用
        if not rec.get("fields") or not fresh:
            # ① 同步只拉 card（快）
            try:
                key = str(self._cfg("rocom_api_key", "") or "").strip()
                base = str(self._cfg("rocom_api_base", "")
                           or "https://wegame.shallow.ink").rstrip("/")
                card = await asyncio.wait_for(
                    self._rocom_call("/api/v1/games/rocom/ingame/player/card",
                                     gid, key, base),
                    timeout=max(2.0, float(wait)))
                if isinstance(card, dict) and card:
                    fields = self._rocom_fields({"card": card})
                    if fields:
                        c = self._rocom_cache()
                        old = (c.get(gid) or {}).get("fields") or []
                        c[gid] = {"at": time.time(), "fields": fields, "full": False}
                        self._rocom_save(c)
            except Exception as e:
                logger.info(f"[角色资料] {gid} card 没等到（{type(e).__name__}），转后台")
        if not rec.get("full"):
            self._rocom_spawn(gid)                  # ② 家园那一路后台补

    def _rocom_spawn(self, gid: str) -> None:
        """后台把完整资料（card + home）拉一遍，下次查就有家园数据了。"""
        running = getattr(self, "_rocom_running", None)
        if running is None:
            running = set()
            self._rocom_running = running
        if gid in running:
            return
        running.add(gid)

        async def _job():
            try:
                payload = await self._rocom_fetch(gid)
                fields = self._rocom_fields(payload) if payload else []
                if fields:
                    c = self._rocom_cache()
                    c[gid] = {"at": time.time(), "fields": fields, "full": True}
                    self._rocom_save(c)
                    logger.info(f"[角色资料] {gid} 后台补全 {len(fields)} 项")
            except Exception as e:
                logger.warning(f"[角色资料] 后台采集失败: {type(e).__name__}: {e}")
            finally:
                running.discard(gid)

        try:
            asyncio.create_task(_job())
        except Exception as e:
            running.discard(gid)
            logger.warning(f"[角色资料] 起任务失败: {type(e).__name__}: {e}")

    def _rocom_rows(self, game_id: str) -> List[tuple]:
        """读缓存给卡片用（这个是同步的，跑在线程里，绝不能发网络请求）。"""
        if not game_id or not self._rocom_on():
            return []
        rec = self._rocom_cache().get(str(game_id)) or {}
        fields = rec.get("fields") or []
        if fields:
            return [(str(k), str(v)) for k, v in fields]
        return [("角色资料", "后台采集中，稍后再查一次就有了")]

    async def _uid_result(self, event: Any, gid: str):
        """用游戏 UID 查角色资料（注册时间 / 家园 / 收集度 等），能列的全列。"""
        gid = str(gid or "").strip()
        if not gid.isdigit():
            return event.plain_result(
                "格式：UID 503671642\n（游戏内的角色 UID，纯数字）")
        if not self._rocom_on():
            return event.plain_result(
                "查游戏角色要先在配置里填 rocom_api_key\n"
                "（在 https://rocom.shallow.ink 申请，填了才启用）")
        cache = self._rocom_cache()
        rec = cache.get(gid) or {}
        fresh = time.time() - float(rec.get("at") or 0) < self.ROCOM_TTL
        if not rec.get("full") or not fresh:
            payload = await self._rocom_fetch(gid)
            fields = self._rocom_fields(payload) if payload else []
            if fields:
                c = self._rocom_cache()
                # 没拿到家园数据就不算 full，下次查会再试一次
                c[gid] = {"at": time.time(), "fields": fields,
                        "full": bool(payload.get("home"))}
                self._rocom_save(c)
                rec = c[gid]
            elif rec.get("fields"):
                pass                       # 拉失败就用旧缓存
        fields = rec.get("fields") or []
        if not fields:
            return event.plain_result(
                f"没查到游戏 UID {gid} 的资料。\n"
                f"可能是接口排队，稍等再发一次；或者这个 UID 不存在。")
        rows = [("游戏UID", gid)] + [(str(k), str(v)) for k, v in fields]
        png = None
        if self._image_reply_on():
            try:
                c = _card()
                if c is not None and c.available():
                    png = await asyncio.to_thread(
                        c.render_card, title="游戏角色", rows=rows,
                        **self._card_kwargs())
            except Exception as e:
                logger.warning(f"[出图] UID 卡失败: {type(e).__name__}: {e}")
        text = "\n".join(f"{k}：{v}" if v else str(k) for k, v in rows)
        return await self._reply(event, text, png)

    @filter.command("UID")
    async def cmd_uid(self, event: AstrMessageEvent):
        """UID 503671642 —— 用游戏角色 UID 查注册信息 / 家园"""
        arg = self._cmd_args(event)
        if not arg:
            m = self._UID_CMD_RE.match(
                str(getattr(event, "message_str", "") or ""))
            arg = m.group(1) if m else ""
        yield await self._uid_result(event, arg)

    @filter.regex(r"^\s*[/#]?\s*(?:查\s*)?(?:[Uu][Ii][Dd]|游戏[Ii][Dd])\s*\d{5,15}\s*$")
    async def on_uid_loose(self, event: AstrMessageEvent):
        """兜底入口：/UID503671642 这种不加空格的写法。"""
        if not self._sp_dedupe(event):
            return
        m = self._UID_CMD_RE.match(
            str(getattr(event, "message_str", "") or "").lstrip("/#").strip())
        if not m:
            return
        yield await self._uid_result(event, m.group(1))

    def _cmd_parts(self, event) -> list:
        """['/标','Z123','黑','备注'] → ['标','Z123','黑','备注']"""
        prefix = str(self._cfg("wake_prefix", "/") or "/")
        raw = (getattr(event, "message_str", "") or "").strip()
        if raw.startswith(prefix):
            raw = raw[len(prefix):].strip()
        return raw.split()

    def _cmd_args(self, event) -> str:
        """把 /查询 火花 → 返回 "火花"（去掉前缀和指令名）"""
        parts = self._cmd_parts(event)
        return " ".join(parts[1:]).strip() if len(parts) > 1 else ""

    def _is_private(self, event) -> bool:
        """当前消息是不是私聊（aiocqhttp 的 umo 里 FriendMessage / GroupMessage）"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if "FriendMessage" in umo or "PrivateMessage" in umo or "friend" in umo.lower():
            return True
        if "GroupMessage" in umo or "group" in umo.lower():
            return False
        # 兜底：看 message_obj 里有没有 group_id
        obj = getattr(event, "message_obj", None)
        if obj is not None and getattr(obj, "group_id", None):
            return False
        return True

    # AstrBot 在派发前已经剥掉了 / 前缀，插件这边不能再校验前缀（否则恒为 False）
    COMMAND_WORDS = ("查询", "查蛋", "帮助", "菜单")

    def _looks_like_command(self, text: str) -> bool:
        t = str(text or "").lstrip()
        return any(t.startswith(w) for w in self.COMMAND_WORDS)


    @filter.command("帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield await self._say(event, self.MENU)


    # 只发了关键词、没带参数 → 直接教格式
    @filter.regex(r"^\s*[\u4e00-\u9fa5A-Za-z0-9·]{2,12}\s*$")
    async def on_bare_species(self, event: AstrMessageEvent):
        """私聊里直接发精灵名字也能查；群里必须带 /"""
        if not bool(self._cfg("bare_name_private", True)):
            return
        if not self._is_private(event):
            return                      # 群聊不响应
        raw = (getattr(event, "message_str", "") or "").strip()
        if not raw or self._looks_like_command(raw):
            return
        rec = self._find_species(raw)
        if not rec:
            grp = self._match_group(raw)      # 私聊直接发「拟人组」也能出清单
            if grp is None:
                return                    # 不是精灵名 / 组名就闭嘴
            yield await self._group_reply(event, grp)
            return
        _t, _p = await self._species_payload(rec)
        yield await self._reply(event, _t, _p)

    @filter.regex(r"^\s*/?\s*\d+(?:\.\d+)?\s*[/\s,，、x×*]\s*\d+(?:\.\d+)?\s*$")
    async def on_bare_hw(self, event: AstrMessageEvent):
        """34/3.617、34 3.617、0.34 3.617 —— 身高/体重 查蛋（也认米）"""
        raw = (getattr(event, "message_str", "") or "").strip()
        prefix = str(self._cfg("wake_prefix", "/") or "/")
        body = raw[len(prefix):].strip() if raw.startswith(prefix) else raw
        hw = self._parse_hw(body)
        if not hw:
            return
        yield await self._identify_reply(event, hw)


    def _admin_ids(self) -> list:
        raw = str(self._cfg("admin_ids", "") or "")
        for ch in ("，", " ", "\n", "\t"):
            raw = raw.replace(ch, ",")
        out = [x.strip() for x in raw.split(",") if x.strip()]
        tq = str(self._cfg("target_qq", "") or "").strip()
        if tq and tq not in out:
            out.append(tq)
        return out

    def _sender_id(self, event) -> str:
        for fn in ("get_sender_id",):
            try:
                v = getattr(event, fn)()
                if v:
                    return str(v)
            except Exception:
                pass
        try:
            obj = getattr(event, "message_obj", None)
            sender = getattr(obj, "sender", None)
            for attr in ("user_id", "uin", "id"):
                v = getattr(sender, attr, None)
                if v:
                    return str(v)
        except Exception:
            pass
        return ""

    def _is_admin(self, event) -> bool:
        admins = self._admin_ids()
        sid = self._sender_id(event)
        if not admins:
            # 没配管理员：只允许私聊里查（相对安全）
            return self._is_private(event)
        return sid in admins

    #: 市场控制台管理员 QQ 的默认值（只有他们能看换绑 / 改名记录）
    DEFAULT_CONSOLE_ADMIN_QQ = "1877811952"
    # ---------------------------------------------------------------- 精灵查询
    #: 命令词：查询 / 查蛋，中间空格随便
    _SP_CMD_RE = re.compile(r"^\s*(?:查\s*询|查\s*蛋)(.*)$")
    #: 尾巴上的「最近 / 历史」→ 该功能已下线
    _SP_RECENT_RE = re.compile(r"(?:最近成交|成交记录|最近|历史)$")

    def _sp_arg_from_text(self, raw: str):
        """从原始消息抠出查蛋参数。

        返回 None 表示「不是查蛋指令」；否则返回 (参数, 是不是风险写法)。

        风险写法 = 命令词后面既没有空格、也没看到斜线。
        AstrBot 派发前就把 `/` 剥掉了（见 COMMAND_WORDS 上面那段注释），
        所以「/查询火花」和「查询火花」在插件里长得一模一样，区分不了。
        这种写法不直接拒绝，交给 _species_result 判断：只有认得出精灵名
        或身高体重才回话，免得把闲聊吃掉。
        """
        s = str(raw or "").strip()
        prefix = str(self._cfg("wake_prefix", "/") or "/")
        had_prefix = False
        if prefix and s.startswith(prefix):
            s = s[len(prefix):].strip()
            had_prefix = True
        elif s.startswith("#"):
            s = s.lstrip("#").strip()
            had_prefix = True
        m = self._SP_CMD_RE.match(s)
        if not m:
            return None
        tail = m.group(1)
        has_sep = tail[:1] in (" ", "\t", "\u3000")
        arg = tail.strip()
        if not arg:
            return ("", False)
        return (arg, not has_sep and not had_prefix)

    # ---------------------------------------------------------------- 蛋组
    #: 「查询 拟人组」「查询 拟人」「查询拟人蛋组」都认
    GROUP_TAILS = ("蛋组", "组别", "组")
    #: 「查询 组」这种，列全部组名
    GROUP_LIST_WORDS = {"组", "组别", "蛋组", "所有组", "全部组", "组列表", "蛋组列表"}
    #: 数据里这些不是真蛋组（空 / 占位）
    GROUP_JUNK = {"", "—", "-", "－", "--", "无", "未知", "待定", "?", "？", "其他"}

    def _all_groups(self) -> Dict[str, List[Dict[str, Any]]]:
        """蛋组名 → 属于它的记录列表（一次性建好，数据是静态的）。"""
        cached = getattr(self, "_group_cache", None)
        if cached is not None:
            return cached
        out: Dict[str, List[Dict[str, Any]]] = {}
        for rec in (self.ranges.get("species") or {}).values():
            seen = []
            for holder in [rec] + list(rec.get("variants") or []):
                for g in re.split(r"[/／、,，]", str(holder.get("蛋组") or "")):
                    g = g.strip()
                    if g in self.GROUP_JUNK:
                        continue
                    if g and g not in seen:
                        seen.append(g)
            for g in seen:
                out.setdefault(g, []).append(rec)
        try:
            self._group_cache = out
        except Exception:
            pass
        return out

    @staticmethod
    def _chain_head(rec: Dict[str, Any]) -> str:
        """进化链一阶段名（记录标题不一定是一阶段，比如 立方人 / 罗隐 这些形态条目）。"""
        chain = str(rec.get("进化链") or "")
        parts = [p.strip() for p in re.split(r"\s*[→>]\s*", chain) if p.strip()]
        head = re.sub(r"[（(].*?[)）]", "", parts[0]).strip() if parts else ""
        return head or str(rec.get("精灵") or "").strip()

    def _match_group(self, q: str) -> Optional[str]:
        """把查询词归一成蛋组名：拟人组 / 拟人 / 拟人蛋组 / 组别 都行。"""
        s = str(q or "").strip()
        if not s:
            return None
        groups = self._all_groups()
        if s in self.GROUP_LIST_WORDS:
            return ""                        # 空串 = 要一份组名清单
        if s in groups:
            return s
        for tail in self.GROUP_TAILS:
            if s.endswith(tail) and len(s) > len(tail):
                head = s[: -len(tail)].strip()
                if head:
                    if head in groups:
                        return head
                    if head + "组" in groups:
                        return head + "组"
        if s + "组" in groups:
            return s + "组"
        return None

    def _group_heads(self, group: str) -> Dict[str, Optional[str]]:
        """组内精灵：一阶段名 → 图鉴号（同一条进化链只留一条）。"""
        heads: Dict[str, Optional[str]] = {}
        for rec in (self._all_groups().get(group) or []):
            nm = self._chain_head(rec)
            if not nm:
                continue
            if nm not in heads:
                heads[nm] = None
            if heads[nm] is None:
                for v in rec.get("variants") or [rec]:
                    d = str(v.get("图鉴编号") or rec.get("图鉴编号") or "").strip()
                    if d:
                        heads[nm] = d
                        break
        return heads

    def _group_reply_text(self, group: str) -> str:
        """某个蛋组的全部精灵（只列进化链一阶段名称）。"""
        groups = self._all_groups()
        if group == "":
            rows = sorted(((g, len(self._group_heads(g))) for g in groups),
                          key=lambda kv: (-kv[1], kv[0]))
            body = " · ".join(f"{g} {n}" for g, n in rows)
            return (f"🧬 蛋组一共 {len(rows)} 个：\n{body}\n\n"
                    f"查法：查询 拟人组 / 查询 拟人 / 查询 海洋")
        heads = self._group_heads(group)
        if not heads:
            return f"「{group}」这个组暂时没有收录的精灵。"

        def _key(item):
            nm, d = item
            try:
                return (0, int(str(d).lstrip("0") or 0), nm)
            except (TypeError, ValueError):
                return (1, 0, nm)

        lines = [f"🧬 {group} · {len(heads)} 只（一阶段名称）"]
        for i, (nm, d) in enumerate(sorted(heads.items(), key=_key), 1):
            lines.append(f"{i:>2}. {nm}{('  ' + str(d)) if d else ''}")
        lines.append("")
        lines.append(f"发「查询 {group}」随时看这个组；发「查询 名字」看单只的蛋数据")
        return "\n".join(lines)

    async def _group_reply(self, event: Any, group: str):
        return await self._say(event, self._group_reply_text(group))

    def _sp_split_mode(self, arg: str):
        """'红绒十字 最近' / '红绒十字历史' → ('红绒十字', 'recent')"""
        a = str(arg or "").strip()
        m = self._SP_RECENT_RE.search(a)
        if not m or len(a) <= len(m.group(0)):
            return a, "info"
        return a[:m.start()].strip(), "recent"

    def _sp_dedupe(self, event: Any) -> bool:
        """命令入口和宽松正则入口可能都命中同一条消息，保证只回一次。"""
        if getattr(event, "_lk_species_done", False):
            return False
        try:
            event._lk_species_done = True
        except Exception:
            pass
        return True

    def _merge_hits(self, hits) -> List[Dict[str, Any]]:
        """同种 + 同身高区间 + 同体重区间的多个形态合成一条。

        海枝枝有 4 个形态（碧蓝珊瑚 / 杏黄百合 / 洋红沙丁 / 翠绿纶布），
        数值完全一样，只有形态名不同 —— 不合并就会在查蛋结果里出现 4 行
        看着一模一样的条目。合并后形态名收进 forms 里一起展示。
        """
        out: List[Dict[str, Any]] = []
        idx: Dict[tuple, Dict[str, Any]] = {}
        for h in (hits or []):
            k = (str(h.get("精灵") or ""),
                 str(h.get("身高区间") or ""),
                 str(h.get("体重区间") or ""))
            form = str(h.get("形态简称") or h.get("形态") or "").strip()
            g = idx.get(k)
            if g is None:
                g = dict(h)
                g["forms"] = [form] if form else []
                idx[k] = g
                out.append(g)
            elif form and form not in g["forms"]:
                g["forms"].append(form)
        return out

    def _distinct_eggs(self, rec: Dict[str, Any]) -> List[tuple]:
        """这只精灵有几个**不同的蛋**，每个蛋对应哪些形态。

        鸭吉吉 6 个形态其实只有 2 个蛋名（5 个共用「鸭吉吉蛋」、燃了鸭是「鸭吉吉的蛋蛋」），
        但每个形态的蛋图标和体重区间都不一样（3012001/3036001/…）。
        查行情要按蛋分别查，不然只出一列。
        """
        order: List[str] = []
        forms_of: Dict[str, List[str]] = {}
        for v in (rec.get("variants") or [rec]):
            egg = str(v.get("蛋名") or rec.get("蛋名") or "").strip()
            if not egg:
                continue
            if egg not in forms_of:
                forms_of[egg] = []
                order.append(egg)
            fm = str(v.get("形态简称") or v.get("形态") or "").strip()
            if fm:
                forms_of[egg].append(fm)
        return [(e, forms_of[e]) for e in order]

    def _pick_variant(self, rec: Dict[str, Any], query: str) -> Dict[str, Any]:
        """多形态精灵：按查询词挑对应形态。

        有的蛋名不是默认形态的（比如「鸭吉吉的蛋蛋」对应「燃了鸭」），
        查这个蛋名时要展示那个形态，而不是第一个。
        """
        variants = rec.get("variants") or [rec]
        q = str(query or "").strip()
        if q:
            for cand in variants:
                for f in ("蛋名", "形态", "形态简称"):
                    if str(cand.get(f) or "").strip() == q:
                        return cand
            for cand in variants:
                for f in ("蛋名", "形态", "形态简称"):
                    s = str(cand.get(f) or "").strip()
                    if s and q and (q in s or s in q):
                        return cand
        return variants[0] if variants else rec

    def _species_rows(self, rec: Dict[str, Any], dex: str = "",
                      query: str = "") -> List[tuple]:
        """图鉴卡的行。多形态会列全部形态（以前只显示第一个）。"""
        variants = rec.get("variants") or [rec]
        _gd = str(rec.get("公母") or "未知")
        # 公母加性别符号：♂ 蓝 / ♀ 红（符号用颜色区分，文字保持原色）
        if ("雄性" in _gd or "雌性" in _gd) and "♂" not in _gd and "♀" not in _gd:
            _gd = _gd.replace("雄性", "♂ 雄性").replace("雌性", "♀ 雌性")
        _eg = str(rec.get("蛋组") or "")
        if "未发现" in _eg:
            _eg = "无法孵蛋"
        rows: List[tuple] = [("蛋组", _eg),
                             ("公母", _gd),
                             ("进化链", rec.get("进化链") or "")]
        if len(variants) <= 1:
            v = variants[0] if variants else {}
            rows += [("身高区间", v.get("身高区间") or ""),
                     ("体重区间", v.get("体重区间") or "")]

            return rows
        # 底部只列「真·多形态」：
        #   - 纯异色变体不算形态（它只体现在上面第 3/4 个图标）
        #   - 只能活动获取的形态（睡铃雪影娃娃 / 果实立方人）整块不列：
        #     它们买不到蛋，身高体重区间 / 过线值 这些数据没有意义
        _real_forms = [v for v in variants
                       if "异色" not in str(v.get("蛋名") or "")
                       and not self._form_own_dex(rec, v)]
        if len(_real_forms) <= 1:
            # 只有普通（或普通+异色）：按单形态展示，别把区间/过线值丢了
            _v = _real_forms[0] if _real_forms else {}
            rows += [("身高区间", _v.get("身高区间") or ""),
                     ("体重区间", _v.get("体重区间") or "")]
            return rows
        # 多形态但数据（身高/体重/区间数）完全一样 → 只显示一遍，另起一行列形态名
        _sigs = {(str(x.get("身高区间") or ""), str(x.get("体重区间") or ""),
                  str(x.get("区间数") or "")) for x in _real_forms}
        # 「形态」这行：3 种及以下写名字，4 种以上只写数量
        _forms_txt = (" / ".join(self._form_label(x) for x in _real_forms)
                      if len(_real_forms) <= 3 else f"{len(_real_forms)} 种")
        if len(_sigs) == 1:
            _v = _real_forms[0]
            rows += [("形态", _forms_txt),
                     ("身高区间", _v.get("身高区间") or ""),
                     ("体重区间", _v.get("体重区间") or "")]
            return rows
        rows.append(("形态", _forms_txt))
        default_egg = str(_real_forms[0].get("蛋名") or "")
        for i, cand in enumerate(_real_forms[:8], 1):
            nm = self._form_label(cand)
            bins = cand.get("区间数")
            rows.append((f"{i}. {nm}",
                         (f"区间 {bins} · " if bins not in (None, "", 0) else "")
                         + str(cand.get("身高区间") or "")
                         + (f" · {cand.get('体重区间')}" if cand.get("体重区间") else "")))
            # 每个形态的过线值 / 极限值跟在它下面（都是这颗蛋自己的数）
            for _lab, _k1, _k2 in (("小块头", "小块头过线值", "小块头极限值"),
                                   ("大块头", "大块头过线值", "大块头极限值")):
                _a, _b = str(cand.get(_k1) or "").strip(), str(cand.get(_k2) or "").strip()
                if _a or _b:
                    rows.append(("", f"{_lab} 过线 {_a or '—'} · 极限 {_b or '—'}"))
            egg = str(cand.get("蛋名") or "")
            if egg and egg != default_egg:
                rows.append(("", f"蛋名 {egg}（和其它形态不同）"))
        return rows

    async def _species_result(self, event: Any, arg: str, risky: bool = False):
        """精灵查询的统一出口。返回 None 表示「决定不吭声」。"""
        q = str(arg or "").strip()
        if not q:
            return await self._say(
                event,
                "格式：查询 火花 / 查蛋 火花\n"
                "· 加空格：带不带斜线都行（查询 火花、/查询 火花）\n"
                "· 不加空格：前面最好带斜线（/查询火花）\n"
                "· 查蛋组：查询 拟人组 / 查询 拟人 / 查询 海洋")
        grp = self._match_group(q)
        if grp is not None:
            return await self._group_reply(event, grp)
        name, mode = self._sp_split_mode(q)
        if mode == "recent":
            return await self._say(
                event, "「查询 xx 最近」的成交记录功能已下线。\n直接查图鉴：查询 火花")
        hw = self._parse_hw(q) if mode == "info" else None
        rec = None if hw else self._find_species(q)
        if risky and mode == "info" and not hw and not rec:
            return None                     # 风险写法又认不出来 → 不吭声
        if hw:
            return await self._identify_reply(event, hw)
        if not rec:
            return await self._say(
                event,
                f"「{q}」暂未收录。\n"
                f"（传说精灵、活动限定这些官方图鉴没公开的品种查不到；"
                f"知道身高体重就直接发，比如 34/3.617 或 0.34 3.617）")
        text, png = await self._species_payload(rec, q)
        return await self._reply(event, text, png)


    @filter.command("诊断")
    async def cmd_diag(self, event: AstrMessageEvent):
        """诊断 看图片链路哪里挂了"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能用「诊断」")
            return
        c = _card()
        lines = [f"插件版本：{VERSION}"]
        lines.append(f"出图开关 reply_as_image：{self._cfg('reply_as_image', True)}")
        lines.append(f"实际出图：{'是' if self._image_reply_on() else '否'}")
        lines.append(f"card.py 加载：{'成功' if c is not None else '失败（看日志 [出图] card.py 加载失败）'}")
        if c is not None:
            lines.append(f"Pillow：{'可用' if c.available() else '缺失（要 pip install pillow）'}")
            try:
                f = c.find_font(str(self._cfg("card_font_path", "") or ""))
                lines.append(f"中文字体：{f or '找不到（会退回文字）'}")
            except Exception as e:
                lines.append(f"中文字体：探测失败 {type(e).__name__}")
            try:
                lines.append(f"图标库：{'有' if c.has_icons() else '缺失（icons/ 没装上）'}")
            except Exception:
                lines.append("图标库：探测失败")
        kw = self._card_kwargs()
        lines.append(f"卡片参数：宽 {kw.get('width')} · 字号 {kw.get('font_size')}")
        lines.append(f"缓存里现有：{len(_CARD_CACHE)} 张")
        try:
            from pathlib import Path as _P
            has_icons_dir = (_P(__file__).resolve().parent / "icons").is_dir()
            lines.append(f"icons/ 目录：{'在' if has_icons_dir else '不在'}")
        except Exception:
            pass
        # 真渲一张试试
        try:
            png = await asyncio.to_thread(self._text_card, "诊断：这张图能出来就说明渲染没问题",
                                          None)
            lines.append(f"试渲一张：{'OK %d 字节' % len(png) if png else '失败'}")
        except Exception as e:
            lines.append(f"试渲一张：抛异常 {type(e).__name__}: {str(e)[:60]}")
        # 诊断结果**故意发文字**：出了问题才好复制，图片链路坏了也能看到
        yield event.plain_result("\n".join(lines))

    # ---------------------------------------------------------------- 诊断图标
    @filter.command("诊断图标")
    async def cmd_icon_diag(self, event: AstrMessageEvent):
        """诊断图标 蹦蹦种子 —— 列出每个形态实际用的精灵图/蛋图文件名"""
        q = self._cmd_args(event)
        if not q:
            return event.plain_result("用法：诊断图标 精灵名")
        rec = self._find_species(q)
        if not rec:
            return event.plain_result(f"「{q}」暂未收录")
        c = _card()
        lines = [f"{rec.get('精灵')} · 图鉴 {rec.get('图鉴编号') or '-'} · {len(rec.get('variants') or [])} 形态"]
        for v in (rec.get("variants") or [])[:8]:
            f = str(v.get("形态简称") or v.get("形态") or "")
            pi = c.icon_path("pet", str(rec.get("精灵") or ""), f) if c else None
            ei = c.icon_path("egg", str(v.get("蛋名") or ""), f) if c else None
            lines.append(f"{f or '默认'}：精灵={Path(pi).name if pi else '缺'} 蛋={Path(ei).name if ei else '缺'}")
        return event.plain_result("\n".join(lines))

    @filter.command("查询")
    async def cmd_species(self, event: AstrMessageEvent):
        """查询 火花 / 查询 火花 最近 —— 进化链上的名字都行"""
        if not self._sp_dedupe(event):
            return
        r = self._sp_arg_from_text(getattr(event, "message_str", ""))
        if not r:
            return
        res = await self._species_result(event, r[0], r[1])
        if res is not None:
            yield res

    @filter.command("查蛋")
    async def cmd_species_alias(self, event: AstrMessageEvent):
        """查蛋 火花 —— 和 查询 完全一样"""
        if not self._sp_dedupe(event):
            return
        r = self._sp_arg_from_text(getattr(event, "message_str", ""))
        if not r:
            return
        res = await self._species_result(event, r[0], r[1])
        if res is not None:
            yield res

    @filter.regex(r"^\s*[/#]?\s*(?:查\s*询|查\s*蛋)(?!\s*[Ii][Dd]).*$")
    async def on_species_loose(self, event: AstrMessageEvent):
        """兜底入口：/查询火花、/查蛋火花 这些 filter.command 派发不到的写法。"""
        if not self._sp_dedupe(event):
            return
        r = self._sp_arg_from_text(getattr(event, "message_str", ""))
        if not r:
            return
        res = await self._species_result(event, r[0], r[1])
        if res is not None:
            yield res

    # 直接发精灵名字也能查（只认图鉴里存在的名字，不认识的直接不理）


def _mark_low(low, median) -> str:
    """最低积分相对中位价染色标记：低于中位=绿(\x01)，高于=红(\x02)。"""
    try:
        lv = float(low)
    except (TypeError, ValueError):
        return str(low if low is not None else 0)
    try:
        mv = float(median)
    except (TypeError, ValueError):
        return str(int(lv))
    if mv <= 0:
        return str(int(lv))
    if lv < mv:
        return "\x01" + str(int(lv))
    if lv > mv:
        return "\x02" + str(int(lv))
    return str(int(lv))
