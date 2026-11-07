# -*- coding: utf-8 -*-
"""洛克王国市集 · 购买成功推送插件（AstrBot / SnowLuma 调优版）。

轮询本机市集控制台，发现新的购买记录后，把「蛋名 + 可能孵出的精灵与概率 +
消耗积分」推送到 QQ。

针对 SnowLuma（OneBot v11 → AstrBot aiocqhttp 适配器）做了三件事：

1. **不猜 umo**：在目标 QQ 会话里发一次 `/蛋推送绑定`，插件记住
   `event.unified_msg_origin`，之后永远推送到这个会话，配置里不用写任何格式。
2. **换行不被吃掉**：aiocqhttp 会对 Plain 做 strip，这里把消息拆成多个组件并
   在首行加零宽空格，多行排版不会再糊成一行。
3. **多套发送方式兜底**：MessageChain(list) → MessageChain().message() → list → 纯字符串，
   不同 AstrBot 版本都能发。

数据来源：
    GET {api_base}/api/purchases        购买记录（含查蛋结果）
    GET {api_base}/api/egg/identify     记录里还没查过的蛋，补查一次
"""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
import hashlib
import math
import os
import re
import json
import shutil
import urllib.parse
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
#: 查ID 的商家数据短缓存：key → (时间戳, data)
_MERCHANT_CACHE: Dict[str, tuple] = {}
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


# ---------------------------------------------------------------- 网格图模块
_GRID_MOD = None
_GRID_TRIED = False


def _grid():
    """加载同目录的 grid.py（4 列网格图渲染器）。

    和 card.py 一样按 __file__ 找，不依赖相对导入。
    grid.py 是 templates/grid.html 的 Pillow 复刻，两条引擎吃同一份数据。
    """
    global _GRID_MOD, _GRID_TRIED
    if _GRID_TRIED:
        return _GRID_MOD
    _GRID_TRIED = True
    import importlib.util
    fp = Path(__file__).with_name("grid.py")
    if not fp.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("lk_egg_grid", str(fp))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _GRID_MOD = m
    except Exception as e:
        logger.warning(f"[网格图] grid.py 加载失败，退回普通卡片: {type(e).__name__}: {e}")
        _GRID_MOD = None
    return _GRID_MOD


#: emoji / 特殊符号：字体（微软雅黑）没有这些字形，画出来是小方块
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"      # 表情、符号、交通、旗帜…
    "\U00002600-\U000027BF"      # 杂项符号（☀✂✉ 等）
    "\U00002B00-\U00002BFF"      # 箭头星形
    "\U0000FE00-\U0000FE0F"      # 变体选择符
    "\U0001F1E6-\U0001F1FF"      # 区域旗帜
    "]+"
)


def _strip_emoji(text: str) -> str:
    """把 emoji 去掉，顺便把多余空格收拢，避免图片里出现豆腐块。"""
    s = str(text or "")
    if not s:
        return s
    s2 = _EMOJI_RE.sub("", s)
    # 标签行前面留的空格清一下（"  已订阅" → "已订阅"）
    s2 = re.sub(r"[ \t]{2,}", " ", s2)
    s2 = re.sub(r"^[ \t]+", "", s2, flags=re.M)
    s2 = re.sub(r"[ \t]+$", "", s2, flags=re.M)
    return s2


_PLUGIN_DIR = Path(__file__).resolve().parent
_PLUGIN_NAME = "astrbot_plugin_lk_egg"
VERSION = "2.1.14"


def _cand_dirs() -> List[Path]:
    """状态文件的候选目录（按优先级）。

    三个都写、读的时候合并 —— 这样：
      · 插件升级覆盖目录（data/ 被删）→ ① 里的还在
      · 换电脑 → 每台机器各自生成，各记各的（本来就是不同环境）
      · 某个目录不可写 → 还有别的顶上
    """
    out: List[Path] = []

    # ① AstrBot 标准数据目录（插件升级不丢，平台规范）
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        out.append(Path(get_astrbot_data_path()) / "plugin_data" / _PLUGIN_NAME)
    except Exception:
        pass

    # ② 插件目录下的 data（__file__ 运行时定位，换电脑自动适配；
    #    不是硬编码路径，装在哪算哪）
    try:
        out.append(Path(__file__).resolve().parent / "data")
    except Exception:
        pass

    # ③ 用户目录兜底
    try:
        out.append(Path.home() / ".astrbot" / "plugin_data" / _PLUGIN_NAME)
    except Exception:
        pass

    # 去重 + 只留能建的
    seen = set()
    ok: List[Path] = []
    for d in out:
        k = str(d)
        if k in seen:
            continue
        seen.add(k)
        try:
            d.mkdir(parents=True, exist_ok=True)
            ok.append(d)
        except Exception:
            continue
    return ok or [Path.cwd() / "data" / _PLUGIN_NAME]


def _data_dir() -> Path:
    """主目录（写这里 + 其它可写目录也同步一份）。"""
    ds = _cand_dirs()
    return ds[0] if ds else Path("data") / _PLUGIN_NAME


_STATE_PATH = _data_dir() / "notified.json"
#: 游戏角色资料缓存（等级 / 注册时间 / 家园信息），按游戏 UID 存
_ROCOM_CACHE_PATH = _STATE_PATH.parent / "rocom_roles.json"
ZWSP = "\u200b"          # 零宽空格：防止适配器把首行/换行 strip 掉

DEFAULT_TEMPLATE = (
    "🥚 购买成功\n"
    "蛋名：{name}\n"
    "消耗：{points} 积分\n"
    "可能孵出：{pets}\n"
    "尺寸：{size}\n"
    "卖家：{seller}\n"
    "时间：{time}"
)


def _migrate_old_state() -> None:
    """把旧版本放在插件目录里的 notified.json 迁移到新位置。"""
    try:
        old = _PLUGIN_DIR / "notified.json"
        if old.exists() and not _STATE_PATH.exists():
            _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, _STATE_PATH)
            logger.info(f"[市集推送] 已迁移旧状态文件到 {_STATE_PATH}")
    except Exception as e:
        logger.warning(f"[市集推送] 迁移旧状态失败: {e}")


def _state_files() -> List[Path]:
    """所有可能存在状态文件的位置。"""
    return [d / "notified.json" for d in _cand_dirs()]


#: 这些是「推送记录」——读的时候所有位置合并，写的时候先跟磁盘合并。
#: 12 个地方用 _save_state(self.state)，而 self.state 里的记录是启动时的旧值，
#: 直接写会把刚写的覆盖掉，导致同一件商品重复推送。
#:
#: 时间戳字典（key → 时间）一律取「较大的时间」，也就是新操作永远赢：
#:   pushed_ids    已推过的挂单 id
#:   pushed_sig    已推过的「蛋内容指纹」（同款蛋重新上架换新 id 也认得）
#:   watch_ops     新上架 添加 的时间（> watch_deleted 才算在监控里）
#:   watch_deleted 新上架 删除 的时间（压过配置里的 watch_sellers）
_MERGE_DICT_KEYS = ("pushed_ids", "pushed_sig", "watch_ops", "watch_deleted",
                    "flower_seen_ids", "flower_sig")
_MERGE_LIST_KEYS = ("flower_seen", "seen")
_MERGE_KEYS = _MERGE_DICT_KEYS + _MERGE_LIST_KEYS


def _merge_ts(old: Any, cur: Any) -> Dict[str, Any]:
    """两个「key → 时间戳」字典合并：同一个 key 取较大的时间。"""
    out: Dict[str, Any] = {}
    for src in (old, cur):
        if not isinstance(src, dict):
            continue
        for k, v in src.items():
            try:
                fv = float(v or 0)
            except (TypeError, ValueError):
                fv = 0.0
            try:
                fo = float(out.get(k) or 0)
            except (TypeError, ValueError):
                fo = 0.0
            if k not in out or fv > fo:
                out[k] = v
    return out


def _load_state() -> Dict[str, Any]:
    """读状态。

    - **标量/列表字段（started、umos、marks、listings…）：以「最后修改的那个文件」为准**
    - **推送记录 + 监控开关：所有位置合并**
        · pushed_ids / pushed_sig / watch_ops / watch_deleted 按时间戳取较大的
        · flower_seen / seen 取并集
      哪个位置丢了都不影响去重，也不会出现「删了监控又被别的文件加回来」。
    """
    _migrate_old_state()
    base: Dict[str, Any] = {"seen": [], "started": 0, "umos": [], "watch": [],
                            "listings": {}, "marks": {}, "watch_ops": {},
                            "watch_deleted": {}, "pushed_ids": {}, "pushed_sig": {}}

    files = []
    for fp in _state_files():
        try:
            if fp.is_file():
                files.append((fp.stat().st_mtime, fp))
        except Exception:
            continue
    if not files:
        return base

    # 最新的排前面
    files.sort(key=lambda x: -x[0])

    merged_dict: Dict[str, Dict[str, Any]] = {k: {} for k in _MERGE_DICT_KEYS}
    merged_list: Dict[str, List[Any]] = {k: [] for k in _MERGE_LIST_KEYS}

    for idx, (mt, fp) in enumerate(files):
        try:
            d = json.loads(fp.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        # ① 非推送记录字段：只取**最新的那个文件**
        if idx == 0:
            for k, v in d.items():
                if k not in _MERGE_KEYS:
                    base[k] = v
        # ② 推送记录 / 监控开关：全部合并
        for k in _MERGE_DICT_KEYS:
            merged_dict[k] = _merge_ts(merged_dict[k], d.get(k))
        for k in _MERGE_LIST_KEYS:
            v = d.get(k)
            if isinstance(v, list):
                merged_list[k].extend(v)

    for k in _MERGE_DICT_KEYS:
        base[k] = merged_dict[k]
    for k in _MERGE_LIST_KEYS:
        base[k] = list(dict.fromkeys(merged_list[k]))
    return base


def _write_state(state: Dict[str, Any]) -> None:
    """把状态原子写到所有候选目录（不做任何合并）。"""
    payload = json.dumps(state, ensure_ascii=False, indent=1)
    wrote = 0
    errs = []
    for d in _cand_dirs():
        try:
            d.mkdir(parents=True, exist_ok=True)
            fp = d / "notified.json"
            tmp = fp.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(fp)            # 原子写，防写一半被读到
            wrote += 1
        except Exception as e:
            errs.append(f"{d}: {type(e).__name__}")
    if not wrote:
        logger.warning(f"[市集推送] 状态保存全部失败: {'; '.join(errs)}")


def _save_state(state: Dict[str, Any]) -> None:
    """保存状态；推送记录先跟磁盘上的合并（只增不减 / 取较新）。"""
    try:
        if _STATE_PATH.is_file():
            try:
                disk = json.loads(_STATE_PATH.read_text(encoding="utf-8-sig"))
                for key in _MERGE_DICT_KEYS:
                    state[key] = _merge_ts(disk.get(key), state.get(key))
                for key in _MERGE_LIST_KEYS:
                    old = disk.get(key)
                    if isinstance(old, list) and old:
                        cur = state.get(key)
                        state[key] = list(dict.fromkeys(
                            (list(cur) if isinstance(cur, list) else []) + list(old)))
            except Exception:
                pass
        _write_state(state)
    except Exception as e:
        logger.warning(f"[市集推送] 保存状态失败: {type(e).__name__}: {e}")


def _hard_save_state(state: Dict[str, Any]) -> None:
    """强制覆盖写：连磁盘上的推送记录一起清掉（蛋推送重置 用）。"""
    try:
        _write_state(state)
    except Exception as e:
        logger.warning(f"[市集推送] 保存状态失败: {type(e).__name__}: {e}")


@register(
    "astrbot_plugin_lk_egg",
    "MIKU013970 & DeepSeek",
    "洛克小肥鱼全功能 + 炫彩花种订阅",
    "2.1.14",
)
class LkMarketNotify(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self.state = _load_state()
        self.state.setdefault("watch", [])
        self.state.setdefault("listings", {})
        self.state.setdefault("marks", {})
        self.ranges = self._load_ranges()
        self.stats = {"polls": 0, "sent": 0, "skipped": 0, "errors": 0, "last_at": 0}

    # ---------------------------------------------------------------- 配置
    def _cfg(self, key: str, default: Any = None) -> Any:
        v = self.config.get(key, default)
        return default if v is None else v

    @property
    def api_base(self) -> str:
        return str(self._cfg("api_base", "http://127.0.0.1:8777")).rstrip("/")

    #: 市场服务暂停总开关：服务停了就把依赖终端(蛋神助手)的功能全部关掉。
    #: 图鉴查询 / 蛋组查询这些纯本地功能不受影响。
    PAUSED_HINT = ("🥚 市场服务暂时关闭\n"
                   "「查ID / 查蛋实时概率 / 购买推送 / 新上架监控 / 蛋推送 / 改绑·改名记录」"
                   "这些依赖终端的功能已暂停，恢复时间请留意官方公告。\n"
                   "图鉴查询（查询 xx / 蛋组）不受影响，照常用。")

    def _market_paused(self) -> bool:
        return bool(self._cfg("market_paused", True))

    @property
    def interval(self) -> float:
        try:
            return max(3.0, float(self._cfg("poll_interval", 20)))
        except (TypeError, ValueError):
            return 20.0

    def _platform(self) -> str:
        """SnowLuma 走 OneBot v11，AstrBot 那边的平台 id 是 aiocqhttp。"""
        return str(self._cfg("platform", "aiocqhttp") or "aiocqhttp")

    def _targets(self) -> List[str]:
        raw = str(self._cfg("target_qq", "") or "")
        return [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]

    def _candidate_umos(self) -> List[str]:
        """优先用 /蛋推送绑定 记下来的会话，其次按模板拼。"""
        umos: List[str] = [u for u in (self.state.get("umos") or []) if u]
        tpl = str(self._cfg("umo_template", "{platform}:FriendMessage:{qq}") or "")
        plat = self._platform()
        for qq in self._targets():
            u = (tpl.replace("{platform}", plat)
                    .replace("{qq}", qq)
                    .replace("{session}", qq))
            if u and u not in umos:
                umos.append(u)
        return umos

    # ---------------------------------------------------------------- HTTP
    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self._session

    async def _api_post(self, path: str, body: str) -> Optional[Dict[str, Any]]:
        """向控制台发 POST 请求（市场补货等）"""
        try:
            sess = await self._session_get()
            async with sess.post(self.api_base + path, data=body,
                                 headers={"Content-Type": "application/json"}) as resp:
                if resp.status != 200:
                    logger.warning(f"[市集推送] POST {path} → HTTP {resp.status}")
                    return None
                return await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[市集推送] POST {path} 失败: {type(e).__name__}: {e}")
            return None

    async def _api(self, path: str) -> Optional[Dict[str, Any]]:
        try:
            sess = await self._session_get()
            async with sess.get(self.api_base + path) as resp:
                if resp.status != 200:
                    logger.warning(f"[市集推送] {path} → HTTP {resp.status}")
                    return None
                return await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[市集推送] {path} 请求失败: {type(e).__name__}: {e}")
            return None

    # ---------------------------------------------------------------- 拼消息
    async def _identify(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """市集推送用：身高体重 → 可能精灵。

        优先用本插件直连的概率接口（cinene），控制台的 /api/egg/identify
        已经失效（恒返回「接口没有解析出可能精灵」），只当兜底。
        """
        guess = item.get("guess")
        if guess and guess.get("top"):
            return guess
        h, w = item.get("height_cm"), item.get("weight_g")
        if not h or not w:
            return None
        # 1) 直连概率接口
        try:
            probs, state = await self._identify_probs(h, w)
            if state == "ok" and probs.get("top"):
                top = [{"name": k, "pct": v} for k, v in probs["top"].items()]
                special = [{"name": k, "pct": v}
                           for k, v in (probs.get("special") or {}).items()]
                return {"top": top, "special": special}
        except Exception as e:
            logger.warning(f"[市集推送] 本机概率接口失败: {type(e).__name__}: {e}")
        # 2) 控制台兜底（多半没用，但留着不亏）
        try:
            data = await self._api(f"/api/egg/identify?h={h}&w={w}")
            got = (data or {}).get("guess")
            if isinstance(got, dict) and got.get("top"):
                return got
        except Exception:
            pass
        return None

    def _pets_text(self, guess: Optional[Dict[str, Any]]) -> str:
        if not guess or not guess.get("top"):
            return "未知（还没查到）"
        limit = int(self._cfg("max_candidates", 4) or 4)
        parts = []
        for c in guess["top"][:limit]:
            name = c.get("name") or ""
            tag = f"[{c['tag']}]" if c.get("tag") else ""
            try:
                pct_s = f"{float(c.get('pct')):g}%"
            except (TypeError, ValueError):
                pct_s = ""
            parts.append(f"{name}{tag} {pct_s}".strip())
        text = " · ".join(parts) if parts else "未知"
        if self._cfg("include_special", False) and guess.get("special"):
            sp = " · ".join(f"{c.get('name')} {float(c.get('pct', 0)):g}%"
                            for c in guess["special"][:3])
            if sp:
                text += f"\n特殊：{sp}"
        return text

    def _render(self, item: Dict[str, Any], guess: Optional[Dict[str, Any]]) -> str:
        h, w = item.get("height_cm"), item.get("weight_g")
        size = f"{h} cm / {round(float(w) / 1000, 3)} kg" if (h and w) else "—"
        ts = item.get("claimed_at")
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(ts / 1000)) if ts \
            else time.strftime("%m-%d %H:%M:%S")
        tpl = str(self._cfg("template", DEFAULT_TEMPLATE) or DEFAULT_TEMPLATE)
        fields = dict(
            name=item.get("name") or "未知蛋", points=item.get("points"),
            pets=self._pets_text(guess), size=size,
            seller=item.get("seller") or "—", courier=item.get("courier") or "—",
            code=item.get("code") or "—", time=when,
        )
        try:
            return tpl.format(**fields)
        except (KeyError, IndexError, ValueError) as e:
            logger.warning(f"[市集推送] 模板占位符有误({e})，改用默认模板")
            return DEFAULT_TEMPLATE.format(**{k: v for k, v in fields.items()
                                              if k in ("name", "points", "pets", "size",
                                                       "seller", "time")})

    # ---------------------------------------------------------------- 发送
    def _chains(self, text: str) -> List[Any]:
        """几种消息链构造方式，按版本兜底；多行拆成多个 Plain 防被 strip。"""
        try:
            from astrbot.api.message_components import Plain
        except Exception:
            return [ZWSP + text]

        lines = text.split("\n")
        parts = [Plain(ZWSP + lines[0])]
        for ln in lines[1:]:
            parts.append(Plain("\n" + ln))
        out: List[Any] = []
        try:
            from astrbot.api.event import MessageChain
            out.append(MessageChain(parts))
            try:
                mc = MessageChain()
                if hasattr(mc, "message"):
                    out.append(mc.message(ZWSP + text))
            except Exception:
                pass
        except Exception:
            pass
        out.append(parts)          # 有些版本直接吃 list[Comp]
        out.append(ZWSP + text)    # 最后退化到纯字符串
        return out

    def _admin_qq_list(self) -> List[str]:
        """管理员 QQ（先看缓存，没有就按配置/内置默认算，不查控制台，保持同步）。"""
        cached = getattr(self, "_console_admin_cache", None)
        if cached and time.time() - cached[0] < self.CONSOLE_ADMIN_TTL:
            return list(cached[1])
        base = str(self._cfg("console_admin_qq", self.DEFAULT_CONSOLE_ADMIN_QQ) or "")
        for ch in ("，", "；", ";", " ", "\n", "\t"):
            base = base.replace(ch, ",")
        out = [x.strip().strip("\"'") for x in base.split(",") if x.strip()]
        return [x for x in out if x] or [self.DEFAULT_CONSOLE_ADMIN_QQ]

    def _notify_targets(self, kind: str = "purchase") -> List[str]:
        """购买成功 / 新上架 通知发到哪。

        配置 `buy_notify_target`（购买成功，默认空 = 只私聊管理员）：
            空 / admin / 管理员 / 私聊  → 只私聊管理员（`console_admin_qq`，没配就 `target_qq`）
            bind / 绑定                → 老行为：发到 `/蛋推送绑定` 过的会话 + `target_qq`
            123456                     → 私聊这个 QQ
            g:123456 / 群:123456        → 发到这个群
            aiocqhttp:GroupMessage:xxx  → 完整 umo 原样用
        多个用逗号隔开；`listing_notify_target` 管新上架（默认 bind，即老行为）。
        """
        key = "listing_notify_target" if kind == "listing" else "buy_notify_target"
        raw = str(self._cfg(key, "" if kind == "purchase" else "bind") or "").strip()
        plat = self._platform()
        tpl = str(self._cfg("umo_template", "{platform}:FriendMessage:{qq}") or "")

        def _priv(qq: str) -> str:
            return (tpl.replace("{platform}", plat)
                       .replace("{qq}", qq).replace("{session}", qq))

        mode = raw.lower()
        if not raw or mode in ("admin", "管理员", "私聊", "private", "私信"):
            admins = self._admin_qq_list() or self._targets()
            out = [_priv(q) for q in admins]
            return out or [_priv(q) for q in self._targets()]
        if mode in ("bind", "绑定", "旧", "legacy", "老"):
            # 绑了几个会话就发几个（用户明确要的）。
            # 「同一件商品不重复」靠 _poll_listings 里的 pushed_ids 保证 ——
            # 同一件商品每轮只会进 fresh 一次，所以每个目标各收一条。
            return self._candidate_umos()
        out: List[str] = []
        for item in re.split(r"[,，\s]+", raw):
            item = item.strip()
            if not item:
                continue
            low = item.lower()
            if low.startswith(("g:", "g：", "群:", "群：")):
                out.append(f"{plat}:GroupMessage:" + re.sub(r"^[^:：]+\s*[:：]", "", item).strip())
            elif item.count(":") >= 2 or item.count("：") >= 2:
                out.append(item)                    # 完整 umo
            elif item.isdigit():
                out.append(_priv(item))
            else:
                out.append(item)
        return out or self._candidate_umos()

    # ---------------------------------------------------------------- 推送开关
    #: 三条推送管线各有各的开关。**监控名单只影响 listing**，
    #: 名单清空了还收到「买到的蛋」播报 / 花种上新，就是另外两条在发。
    KIND_NAMES = {"purchase": "购买播报", "listing": "新上架提醒", "flower": "花种订阅"}
    KIND_SWITCH = {"purchase": "purchase_off", "listing": "listing_push_off",
                   "flower": "flower_push_off"}

    @staticmethod
    def _is_group_umo(umo: str) -> bool:
        s = str(umo or "")
        return "GroupMessage" in s or "group" in s.lower()

    def _kind_off(self, kind: str) -> bool:
        key = self.KIND_SWITCH.get(str(kind or "purchase"), "purchase_off")
        return bool(float(self.state.get(key) or 0))

    def _push_switch_state(self) -> Dict[str, Any]:
        return {
            "push_off": bool(float(self.state.get("push_off") or 0)),
            "group_off": bool(float(self.state.get("group_off") or 0)),
            "purchase_off": self._kind_off("purchase"),
            "listing_push_off": self._kind_off("listing"),
            "flower_push_off": self._kind_off("flower"),
        }

    def _push_switch_text(self) -> str:
        s = self._push_switch_state()
        on, off = "🟢 开", "🔴 关"

        def _tpl_state(key: str) -> str:
            if self._tpl_off(key):
                return "关"
            return "内置排版" if not str(self._cfg(key, "") or "").strip() else "自定义"

        return (
            f"推送总开关：{off if s['push_off'] else on}\n"
            f"① 购买播报：{off if s['purchase_off'] else on}\n"
            f"② 新上架提醒：{off if s['listing_push_off'] else on}"
            f"（监控商家 {len(self._watch())} 个）\n"
            f"③ 花种订阅：{off if s['flower_push_off'] else on}"
            f"（配置 flower_enabled={'开' if bool(self._cfg('flower_enabled', True)) else '关'}）\n"
            f"群聊推送：{'🔴 关（只发私聊）' if s['group_off'] else on}\n"
            f"模板：新上架={_tpl_state('listing_template')} · 购买={_tpl_state('template')}"
            f"（模板留空＝内置排版，照样推；填「关」才是关）\n"
            f"配置里的目标：购买={self._cfg('buy_notify_target', '') or '空(只私聊管理员)'}"
            f" · 新上架={self._cfg('listing_notify_target', 'bind') or '空'}"
            + (f"\n旧版残留已清理：{'、'.join(sorted((self.state.get('watch_legacy_cleaned') or {}).keys()))}"
               f"（旧版 wire 在 notified.json 的 watch 数组里，新版不再认它）"
               if self.state.get("watch_legacy_cleaned") else "")
        )

    def _set_push_switch(self, key: str, on: bool) -> None:
        self.state[key] = 0 if on else time.time()
        _save_state(self.state)
        logger.info(f"[市集推送] 开关 {key} → {'开' if on else '关'}")

    def _log_push(self, kind: str, ok: int, targets: List[str]) -> None:
        """记一条推送流水，`蛋推送状态` 里能看到最近是谁在发。"""
        log = list(self.state.get("push_log") or [])
        log.append({"t": int(time.time()), "k": str(kind), "n": ok,
                    "to": targets[:3]})
        self.state["push_log"] = log[-20:]
        _save_state(self.state)

    def _push_log_text(self, limit: int = 5) -> str:
        log = list(self.state.get("push_log") or [])[-limit:]
        if not log:
            return "最近推送：（还没有记录）"
        lines = [f"最近 {len(log)} 条推送（从旧到新）："]
        for it in log:
            ts = time.strftime("%m-%d %H:%M:%S", time.localtime(float(it.get("t") or 0)))
            kind = self.KIND_NAMES.get(str(it.get("k")), str(it.get("k")))
            to = "、".join(("群" if self._is_group_umo(x) else "私")
                           + str(x).split(":")[-1] for x in (it.get("to") or []))
            lines.append(f"· {ts} {kind} → {to}")
        return "\n".join(lines)

    async def _send(self, text: str, kind: str = "purchase") -> int:
        # 运行期开关（指令切换，写在 state 里，重启不丢）
        if float(self.state.get("push_off") or 0):
            logger.info(f"[市集推送] 推送总开关是关的，这条 {kind} 不发")
            return 0
        if self._kind_off(kind):
            logger.info(f"[市集推送] {self.KIND_NAMES.get(kind, kind)} 已关，这条不发")
            return 0
        if kind == "purchase" and self._tpl_off("template"):
            logger.info("[市集推送] template = 关 → 购买播报整条关掉，不发")
            return 0
        group_off = bool(float(self.state.get("group_off") or 0))
        umos = self._notify_targets(kind)
        if group_off:
            umos = [u for u in umos if not self._is_group_umo(u)]
        if not umos:
            logger.warning("[市集推送] 没有推送目标：先在目标会话发一次 /蛋推送绑定，"
                           "或在配置里填 target_qq"
                           + ("（群聊推送已关，所以群被过滤掉了）" if group_off else ""))
            return 0
        ok = 0
        sent_to: List[str] = []
        for umo in umos:
            sent = False
            last: Optional[Exception] = None
            for chain in self._chains(text):
                try:
                    await self.context.send_message(umo, chain)
                    sent = True
                    break
                except Exception as e:
                    last = e
            if sent:
                ok += 1
                sent_to.append(umo)
                logger.info(f"[市集推送] 已推送到 {umo}")
            else:
                logger.warning(f"[市集推送] 推送到 {umo} 失败: "
                               f"{type(last).__name__}: {last}")
        if ok:
            try:
                self._log_push(kind, ok, sent_to)
            except Exception as e:
                logger.warning(f"[市集推送] 记推送流水失败: {type(e).__name__}: {e}")
        return ok

    # ---------------------------------------------------------------- 主循环
    async def _poll_once(self, notify: bool = True) -> List[Dict[str, Any]]:
        data = await self._api("/api/purchases")
        if not data or not data.get("ok"):
            self.stats["errors"] += 1
            if data and data.get("error"):
                logger.warning(f"[市集推送] 读取购买记录失败: {data['error']}")
            return []
        items = data.get("items") or []
        seen = set(self.state.get("seen") or [])
        fresh = [it for it in items if it.get("id") and it["id"] not in seen]
        if not fresh:
            return []

        # 第一次跑：默认跳过已有记录，避免一装插件刷屏
        if not self.state.get("started"):
            self.state["started"] = int(time.time())
            if not self._cfg("notify_existing", False):
                seen.update(it["id"] for it in items if it.get("id"))
                self.state["seen"] = list(seen)[-5000:]
                _save_state(self.state)
                logger.info(f"[市集推送] 首次运行，已跳过 {len(items)} 条历史记录")
                return []
        self.state["started"] = self.state.get("started") or int(time.time())

        only_unclaimed = bool(self._cfg("only_unclaimed", True))
        pushed = []
        for it in fresh:
            seen.add(it["id"])
            if only_unclaimed and it.get("state") != "available":
                self.stats["skipped"] += 1
                continue
            guess = await self._identify(it)
            text = self._render(it, guess)
            if notify:
                if await self._send(text):
                    self.stats["sent"] += 1
                    pushed.append(it)
                else:
                    self.stats["errors"] += 1
            else:
                pushed.append(it)

        self.state["seen"] = list(seen)[-5000:]
        _save_state(self.state)
        return pushed

    # ---------------------------------------------------------------- 花种订阅轮询
    #: 花种已推记录保留时长（秒）。花种挂单会下架重上（新 id、内容一样），
    #: 只按 id 记就会重复推 —— 所以还要按内容指纹兜一层。
    FLOWER_KEEP = 30 * 86400

    @classmethod
    def _flower_sig(cls, species: str, b: Dict[str, Any]) -> str:
        raw = "|".join(str(x) for x in (
            species, b.get("appearance"), b.get("color_name"), b.get("c1"),
            b.get("c2"), b.get("particle_name"), b.get("price"),
            b.get("seller_id"), b.get("seller"), b.get("remark"),
            b.get("min_people"), b.get("max_people")))
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]

    async def _flower_poll_once(self) -> int:
        """扫一遍花种市场，把匹配订阅的新挂单推出去。返回推送条数。

        去重跟「新上架」那套一致：
          · flower_seen_ids：挂单 id → 时间（30 天，还在架上的每轮续命）
          · flower_sig：内容指纹 → 时间（卖家下架重上、换了新 id 也认得出）
          · 先落盘再推送
        """
        if not self._cfg("flower_enabled", True):
            return 0
        subs = self._flower_subs()
        if not subs:
            return 0
        if self._market_paused():
            return 0
        if self._kind_off("flower"):
            return 0

        now = time.time()
        # 上一轮被限流（429）→ 这一轮干脆别碰接口，免得把「花种 xxx」查询也一起坑了
        lim = float(self.state.get("flower_limited_at") or 0)
        if lim and now - lim < 120:
            return 0
        keep = self.FLOWER_KEEP
        seen_ids = dict(self.state.get("flower_seen_ids") or {})
        seen_sig = dict(self.state.get("flower_sig") or {})
        # 老版本只有一个列表，迁移过来（时间按现在算，避免一上来全重推）
        legacy = self.state.get("flower_seen")
        if isinstance(legacy, list) and legacy and not seen_ids:
            for oid in legacy:
                if oid:
                    seen_ids[str(oid)] = now
        for k in [k for k, ts in seen_ids.items() if now - float(ts or 0) > keep]:
            seen_ids.pop(k, None)
        for k in [k for k, ts in seen_sig.items() if now - float(ts or 0) > keep]:
            seen_sig.pop(k, None)

        catalog = await self._flower_catalog()
        if not catalog:            # 失败 或 市场空，都不推
            return 0
        pushed = 0
        for it in catalog:
            pid = it.get("petId")
            if not pid:
                continue
            species = str(it.get("name") or "")
            offers = await self._flower_offers(pid)
            if float(self.state.get("flower_limited_at") or 0) >= now - 5:
                logger.warning("[花种] 取挂单被限流，本轮中止（已记的已推记录保留）")
                break
            for o in offers:
                b = self._flower_brief(o)
                oid = str(b.get("id") or "")
                if not oid:
                    continue
                sig = self._flower_sig(species, b)
                if oid in seen_ids or sig in seen_sig:
                    seen_ids[oid] = now      # 还在架上：续命
                    seen_sig[sig] = now
                    continue
                seen_ids[oid] = now
                seen_sig[sig] = now
                # 匹配订阅
                hit = []
                for s in subs:
                    kind, val = str(s.get("kind")), str(s.get("value"))
                    if kind == "color" and val and val in b.get("color_name", ""):
                        hit.append(f"颜色·{val}")
                    elif kind == "particle" and val and val in b.get("particle_name", ""):
                        hit.append(f"粒子·{val}")
                    elif kind == "species":
                        # 订阅的是精灵：用图鉴记录对 petId
                        rec = self._find_species(val)
                        if rec and self._flower_species_name(pid, species) == str(
                                rec.get("精灵") or ""):
                            hit.append(f"精灵·{val}")
                        elif val and (val in species or species in val):
                            hit.append(f"精灵·{val}")
                if not hit:
                    continue
                # 先落盘再推送
                self.state["flower_seen_ids"] = seen_ids
                self.state["flower_sig"] = seen_sig
                _save_state(self.state)
                text = self._render_flower_alert(species, b, hit)
                if await self._send(text, kind="flower"):
                    pushed += 1
        self.state["flower_seen_ids"] = seen_ids
        self.state["flower_sig"] = seen_sig
        self.state["flower_seen"] = []          # 老字段不再使用
        _save_state(self.state)
        return pushed

    def _render_flower_alert(self, species: str, b: Dict[str, Any],
                             hit: List[str]) -> str:
        """一条花种上新推送。"""
        look = str(b.get("appearance_txt") or "未标注")
        lines = [
            "🌸 新花种上架",
            f"精灵：{look} {species}",
            f"类型：{self.FLOWER_TYPE.get(b.get('type') or '', b.get('type') or '')}",
            f"颜色：{b.get('color_name') or '未知'}"
            + (f"  ({b.get('c1')} / {b.get('c2')})" if b.get("c1") else ""),
        ]
        if b.get("particle_name"):
            lines.append(f"粒子：{b['particle_name']}")
        lines.append(f"价格：{b.get('price')} 分" if b.get("price") is not None
                     else "价格：?")
        if b.get("seller"):
            lines.append(f"卖家：{b['seller']}"
                         + (f"（{b['seller_id']}）" if b.get("seller_id") else ""))
        if b.get("min_people"):
            lines.append(f"成团：{b.get('people')}/{b.get('min_people')}")
        if b.get("remark"):
            lines.append(f"备注：{b['remark']}")
        if hit:
            lines.append("匹配：" + "、".join(hit))
        return "\n".join(lines)

    async def _loop(self) -> None:
        if self._market_paused():
            logger.info("[市集推送] 市场服务暂停（market_paused=true），不轮询终端")
            return
        logger.info(f"[市集推送/lk_egg v{VERSION}] 轮询 {self.api_base} 每 {self.interval:g}s"
                    f" · 平台 {self._platform()} · 目标 {len(self._candidate_umos())} 个"
                    f" · 监控商家 {len(self._watch())} 个")
        # 最常见的误解：把「新上架提醒模板」的内容删干净，以为就不推了
        if (not self._tpl_off("listing_template")
                and not str(self._cfg("listing_template", "") or "").strip()
                and self._watch()):
            logger.info("[市集推送] listing_template 是空的 → 用内置排版，新上架提醒照样会推。"
                        "不想推：清空 watch_sellers / 发「蛋推送新关」/ 模板填「关」")
        while True:
            if self._market_paused():
                logger.info("[市集推送] 市场服务已暂停，停止轮询")
                break
            self.stats["polls"] += 1
            self.stats["last_at"] = int(time.time())
            try:
                await self._poll_once(True)
                await self._poll_listings()
                # 花种订阅：每 flower_poll_every 轮扫一次（默认 3，避免请求太密）
                try:
                    every = max(1, int(self._cfg("flower_poll_every", 3) or 3))
                except (TypeError, ValueError):
                    every = 3
                self.stats["flower_tick"] = int(self.stats.get("flower_tick") or 0) + 1
                if self.stats["flower_tick"] % every == 0:
                    await self._flower_poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.stats["errors"] += 1
                logger.warning(f"[市集推送] 轮询异常: {type(e).__name__}: {e}")
            await asyncio.sleep(self.interval)

    # ---------------------------------------------------------------- 生命周期
    async def initialize(self) -> None:
        # 预生成卡片放最前面：就算下面的轮询被 enabled 关掉，
        # 指令handler 照样在跑，/帮助 也得快
        try:
            self._warm_task = asyncio.create_task(self._prewarm_cards())
        except Exception as e:                      # 极端情况：没有事件循环
            logger.warning(f"[出图] 预生成启动失败: {type(e).__name__}: {e}")
            self._warm_task = None
        # 网格图预生成：蛋组 / 各赛季异色这些内容基本不变，先渲好放缓存，
        # 用户发指令时直接命中（不用等 HTML 截图或 Pillow 现画）
        try:
            self._grid_warm_task = asyncio.create_task(self._prewarm_grids())
        except Exception as e:
            logger.warning(f"[网格图] 预生成启动失败: {type(e).__name__}: {e}")
            self._grid_warm_task = None
        if not bool(self._cfg("enabled", True)):
            logger.info("[市集推送] 配置 enabled=false，插件不启动")
            return
        # 防重复启动：热重载时 initialize 会被再调一次，
        # 不检查的话会有两个 _loop 同时跑 → 同一件商品推两遍
        _old = getattr(self, "_task", None)
        if _old is not None and not _old.done():
            logger.info("[市集推送] 轮询已在运行，跳过重复启动")
            return
        logger.info(f"[市集推送] 状态文件: {_STATE_PATH}")
        self._task = asyncio.create_task(self._loop())

    async def terminate(self) -> None:
        _wt = getattr(self, "_warm_task", None)
        if _wt:
            _wt.cancel()
            try:
                await _wt
            except (asyncio.CancelledError, BaseException):
                pass
            self._warm_task = None
        _gt = getattr(self, "_grid_warm_task", None)
        if _gt:
            _gt.cancel()
            try:
                await _gt
            except (asyncio.CancelledError, BaseException):
                pass
            self._grid_warm_task = None
        if _ct:
            _ct.cancel()
            try:
                await _ct
            except (asyncio.CancelledError, BaseException):
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, BaseException):
                pass
            self._task = None
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("[市集推送] 已停止")


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

        strict=True 时不做模糊匹配（概率接口给的是准确名字，模糊会误伤）。
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

        # ---- 下面两档是给「打缩写 / 记不全名字」用的 ----
        # ① 拼音首字母：fcsm → 翡翠水母、dle → 豆粒鹅
        hit = self._match_by_initials(q)
        if hit:
            return hit
        # ② 汉字按顺序出现：翡翠母 / 水母 → 翡翠水母
        hit = self._match_by_subsequence(q)
        if hit:
            return hit
        return None

    # ---------------------------------------------------------------- 拼音/缩写匹配
    #: 汉字 → 拼音首字母（表由 build_pinyin_initials.py 生成，纯标准库，不依赖拼音包）
    _PY_INIT: Dict[str, str] = {}
    _PY_TRIED = False

    @classmethod
    def _py_table(cls) -> Dict[str, str]:
        if cls._PY_TRIED:
            return cls._PY_INIT
        cls._PY_TRIED = True
        try:
            fp = _PLUGIN_DIR / "pinyin_initials.json"
            cls._PY_INIT = json.loads(fp.read_text(encoding="utf-8-sig"))
        except Exception as e:
            logger.warning(f"[拼音] 首字母表加载失败: {type(e).__name__}: {e}")
            cls._PY_INIT = {}
        return cls._PY_INIT

    @classmethod
    def _initials(cls, text: str) -> str:
        """汉字串 → 拼音首字母串（非汉字原样转大写）。"""
        t = cls._py_table()
        out = []
        for c in str(text or ""):
            if "\u4e00" <= c <= "\u9fff":
                out.append(t.get(c, ""))
            elif c.isalnum():
                out.append(c.upper())
        return "".join(out)

    def _all_names(self) -> List[tuple]:
        """候选池：(名字, 记录) —— 精灵名 / 形态名 / 蛋名 / 别名。"""
        sp = self.ranges.get("species") or {}
        alias = self.ranges.get("alias") or {}
        out: List[tuple] = []
        for name, rec in sp.items():
            out.append((name, rec))
            for v in rec.get("variants") or []:
                for k in ("形态", "形态简称", "蛋名"):
                    x = str(v.get(k) or "").strip()
                    if x:
                        out.append((x, rec))
        for nm, target in alias.items():
            if target in sp:
                out.append((str(nm), sp[target]))
        return out

    def _match_by_initials(self, q: str) -> Optional[Dict[str, Any]]:
        """纯字母查询按拼音首字母匹配（fcsm → 翡翠水母）。

        完全相等 100 分 > 前缀 80 > 子序列 60，同分优先名字短的。
        """
        qq = re.sub(r"[^0-9A-Za-z]", "", str(q or "")).upper()
        if not (2 <= len(qq) <= 10):
            return None
        best = None
        best_score = -1
        for name, rec in self._all_names():
            ini = self._initials(name)
            if not ini:
                continue
            if ini == qq:
                score = 100
            elif ini.startswith(qq):
                score = 80
            else:
                it = iter(ini)
                if all(ch in it for ch in qq):
                    score = 60 - max(0, len(ini) - len(qq)) // 3
                else:
                    continue
            score -= len(name) // 4          # 同等匹配下优先短的
            if score > best_score:
                best_score = score
                best = rec
        return best

    def _match_by_subsequence(self, q: str) -> Optional[Dict[str, Any]]:
        """汉字记不全时按「字符按顺序出现」匹配：翡翠母 / 水母 → 翡翠水母。"""
        s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(q or ""))
        if len(s) < 2:
            return None
        best = None
        best_key = None
        for name, rec in self._all_names():
            nm = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", name)
            if len(nm) < len(s):
                continue
            it = iter(nm)
            if not all(ch in it for ch in s):
                continue
            key = (len(nm), nm)
            if best_key is None or key < best_key:
                best_key = key
                best = rec
        return best

    # ---------------------------------------------------------------- 查蛋行情
    async def _market_quote_raw(self, name: str) -> Dict[str, Any]:
        """查蛋时附带的实时行情（大婉 / 小婉 / 大粗 / 小粗）。

        走控制台的 GET /api/market/quote，控制台那边负责多账号轮询 + 按蛋种缓存。
        拿不到（超时 / 控制台没起 / 接口报错）就返回空字典，绝不抛出去卡住查蛋。
        """
        if self._market_paused():
            return {}
        if not bool(self._cfg("market_quote_enabled", True)):
            return {}
        nm = str(name or "").strip()
        if not nm:
            return {}
        try:
            cache_min = int(self._cfg("market_quote_cache_min", 60))
        except (TypeError, ValueError):
            cache_min = 60
        try:
            timeout = max(1.0, float(self._cfg("market_quote_timeout", 6)))
        except (TypeError, ValueError):
            timeout = 6.0
        path = (f"/api/market/quote?q={urllib.parse.quote(nm)}"
                f"&cache_min={max(0, cache_min)}")
        try:
            sess = await self._session_get()
            async with sess.get(self.api_base + path,
                                timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    logger.warning(f"[查蛋行情] {nm} → HTTP {resp.status}")
                    return {}
                data = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[查蛋行情] {nm} 取失败: {type(e).__name__}: {e}")
            return {}
        if not isinstance(data, dict) or not data.get("ok"):
            err = (data or {}).get("error") if isinstance(data, dict) else ""
            if err:
                logger.warning(f"[查蛋行情] {nm}: {str(err)[:90]}")
            return {}
        if data.get("stale"):
            logger.info(f"[查蛋行情] {nm} 用了过期缓存（{data.get('error') or '取数失败'}）")
        return data

    async def _market_quote_lines(self, name: str) -> List[str]:
        data = await self._market_quote_raw(name)
        lines = data.get("lines") or []
        return [str(x) for x in lines if str(x).strip()][:8]

    async def _species_payload(self, rec: Dict[str, Any], query: str = "",
                               with_market: bool = True):
        """图鉴正文 + 行情；返回 (文字版, 图片版或 None)。

        with_market=False 时不取行情（没 market 权限的人看不到市场价）。
        """
        text = self._render_species(rec)
        lines: List[str] = []
        items: List[Dict[str, Any]] = []
        eggs = self._distinct_eggs(rec) if with_market else []
        try:
            for egg_name, _forms in (eggs or []):
                data = await self._market_quote_raw(egg_name)
                seg = [str(x) for x in (data.get("lines") or []) if str(x).strip()][:8]
                if not seg:
                    continue
                if len(eggs) > 1:            # 多蛋：标一下这是哪个蛋的行情
                    lines.append(f"—— {egg_name} ——")
                lines.extend(seg)
                if not items:
                    items = [x for x in (data.get("items") or []) if isinstance(x, dict)]
                    items = [dict(x, _egg=egg_name) for x in items]
        except Exception as e:                       # 兜底：行情绝不能影响查蛋
            logger.warning(f"[查蛋行情] 渲染异常: {type(e).__name__}: {e}")
        if lines:
            text = text + "\n" + "\n".join(lines)
        png = None
        if self._image_reply_on():
            try:
                png = await asyncio.to_thread(self._species_card_cached, rec, items, query)
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

    # ---------------------------------------------------------------- 出图
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
            title=_strip_emoji(str(rec.get("精灵") or "")),
            subtitle=_strip_emoji(" · ".join(x for x in (
                (f"图鉴 {_dex_txt}" if _dex_txt else ""),
                ("\x01可同乘" if rec.get("同乘") else "")) if x)),
            tag=_strip_emoji(f"区间 {bins}" if bins not in (None, "", 0) else ""),
            rows=[(_strip_emoji(a), _strip_emoji(b)) for a, b in rows],
            boxes=boxes, table=table, tiles=tiles,
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

    @staticmethod
    def _fix_avatar_url(url: str) -> str:
        """补全头像 URL 的端口。

        实测平台给的头像地址有两种：
            https://api.danking.cn:18443/miniapp/...   ← 能下
            https://api.danking.cn/miniapp/...         ← 少的这个，直连 SSL 报错、
                                                          走控制台代理也只回 68 字节错误页
        所以统一补上 :18443。
        """
        u = str(url or "").strip()
        if not u:
            return ""
        if u.startswith("https://api.danking.cn/"):
            u = "https://api.danking.cn:18443/" + u[len("https://api.danking.cn/"):]
        elif u.startswith("http://api.danking.cn/"):
            u = "https://api.danking.cn:18443/" + u[len("http://api.danking.cn/"):]
        return u

    async def _fetch_bytes(self, path: str, timeout: float = 8.0):
        """从控制台取二进制（头像用）。"""
        try:
            sess = await self._session_get()
            async with sess.get(self.api_base + path,
                                timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    logger.warning(f"[头像] {path[:40]} → HTTP {resp.status}")
                    return None
                return await resp.read()
        except Exception as e:
            logger.warning(f"[头像] 取失败: {type(e).__name__}: {e}")
            return None

    async def _avatar_fetch_job(self, seller: str, url: str) -> None:
        """后台下载头像（不阻塞查ID）。"""
        try:
            data = await self._fetch_bytes(
                "/api/avatar?u=" + urllib.parse.quote(url, safe=""))
            if data and len(data) >= 200:
                fp = self._avatar_file(seller)
                fp.write_bytes(data)
                logger.info(f"[头像] {seller} 下好了 {len(data)} B")
        except Exception as e:
            logger.warning(f"[头像] 后台下载失败: {type(e).__name__}: {e}")

    def _avatar_file(self, seller: str) -> Path:
        import tempfile
        d = Path(tempfile.gettempdir()) / "lk_egg_cards"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            d = Path(tempfile.gettempdir())
        tag = re.sub(r"[^A-Za-z0-9]+", "_", str(seller or "x"))[:24]
        return d / f"avatar-{tag}.img"

    async def _avatar_path(self, seller: str, url: str) -> Optional[str]:
        """**不阻塞**：本地有（24 小时内）就返回路径，没有就起个后台任务去下。

        平台头像首次要 0.3 秒，但个别商家的 URL 少端口，控制台代理会卡到 20 秒
        —— 那玩意儿绝不能挡在查ID 前面。
        """
        u = self._fix_avatar_url(url)
        if not u:
            return None
        fp = self._avatar_file(seller)
        try:
            if fp.is_file() and fp.stat().st_size > 200 \
                    and time.time() - fp.stat().st_mtime < 86400:
                return str(fp)
        except Exception:
            pass
        # 同步拉，最多 2.5 秒。
        # 实测控制台代理缓存命中只要 0.02 秒；个别商家 URL 少端口会卡到 20 秒，
        # 那种就放弃（下次再说），绝不为了它让整条查ID 等 20 秒。
        try:
            data = await asyncio.wait_for(
                self._fetch_bytes("/api/avatar?u=" + urllib.parse.quote(u, safe="")),
                timeout=2.5)
        except Exception:
            data = None
        if data and len(data) >= 200:
            try:
                fp.write_bytes(data)
                return str(fp)
            except Exception:
                return None
        return None

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
        # 文件名必须唯一：以前用「毫秒 + pid」，同一毫秒连出多张（网格图分页）
        # 会互相覆盖，第二页把第一页顶掉 —— 现在加序号 + 随机尾巴
        global _TEMP_PNG_SEQ
        _TEMP_PNG_SEQ = (globals().get("_TEMP_PNG_SEQ") or 0) + 1
        fp = d / (f"card-{int(time.time() * 1000)}-{os.getpid() % 999:03d}"
                  f"-{_TEMP_PNG_SEQ}-{uuid.uuid4().hex[:6]}.png")
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

    def _result_images(self, event: Any, pngs: List[bytes], caption: str = ""):
        """一次发多张图（网格图分页时用）。能链式就链式，不行只发第一张。"""
        pics = [p for p in (pngs or []) if p]
        if not pics:
            return None
        if len(pics) == 1:
            return self._result_image(event, pics[0], caption)
        try:
            from astrbot.api.message_components import Image as _Image, Plain
            paths = [self._temp_png(p) for p in pics]
            parts: List[Any] = [_Image.fromFileSystem(p) for p in paths]
            if caption:
                parts.append(Plain(caption))
            ch = getattr(event, "chain_result", None)
            if callable(ch):
                return ch(parts)
        except Exception as e:
            logger.warning(f"[出图] 多图链式发送失败: {type(e).__name__}: {e}")
        return self._result_image(event, pics[0], caption)

    async def _reply(self, event: Any, text: str, png: Optional[bytes],
                     t0: Optional[float] = None):
        """统一出口：能出图就出图，出不了就发文字。

        t0 传入指令开始处理的时间戳，就会把「出图耗时」跟着图片一起发出来
        （配置 show_timing 控制，默认开）。
        """
        if png and self._image_reply_on():
            try:
                r = self._result_image(event, png)
                if r is not None:
                    return r
            except Exception as e:
                logger.warning(f"[出图] 发图失败，退回文字: {type(e).__name__}: {e}")
        return event.plain_result(text)

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
        # 多形态精灵每个形态可能各有一颗蛋（地鼠：枯水期 107012 / 储水时 107170），
        # 这里必须把形态一起传下去，否则两个形态都会拿到同一张图
        egg_path = c.icon_path("egg", egg, form0)
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
            # 图鉴记录里没有「异色××的蛋」这种形态时（新赛季那批就是这样），
            # 直接按「异色 + 蛋名」去图标库里找 —— 不然异色蛋永远不显示
            if not shiny_egg:
                shiny_egg = (c.icon_path("egg", f"异色{egg}", form0)
                             or c.icon_path("egg", f"异色{pet}的蛋", form0))
            if shiny_egg:
                out.append(shiny_egg)
        # 同一张图不重复显示：通行证精灵的「普通蛋」和「异色蛋」官方就是同一张图，
        # 归一化到 item 数字 id 再比（蛋150701 / 异色蛋150701 视为同一张）
        def _icon_key(p):
            stem = Path(p).stem
            # ⚠ 必须带上目录：新赛季的图标命名是 nrc659 / nrc659（pet 和 shiny 同名），
            # 只按文件名判断会把异色图当成重复图丢掉 —— 这就是「异色一阶段不显示」的原因
            folder = Path(p).parent.name
            mm = re.search(r"(\d{4,})", stem)
            base = mm.group(1) if mm else stem
            if folder == "shiny" or stem.startswith("shiny"):
                return "shiny-" + base
            if folder == "pet" or stem.startswith("pet"):
                return "pet-" + base
            return folder + "-" + base

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

    def _shelf_tiles(self, items) -> List[tuple]:
        """货架图标格。

        **名字、价格、奖牌三个都一样才合并**（合成一格标 ×N）；
        缺任何一个不同（同种但价格不一样、或价格一样但奖牌不一样）就各自占一格，
        否则价格和牌子就看不出来了。
        排序：同种排在一起，种内按价格升序。
        """
        c = _card()
        if c is None or not c.available():
            return []
        groups: Dict[tuple, Dict[str, Any]] = {}
        for it in (items or []):
            it = it if isinstance(it, dict) else {}
            name = str(it.get("name") or "").strip()
            if not name:
                continue
            kind = re.sub(r"(的蛋|蛋)$", "", name) or name
            try:
                pts = int(it.get("points"))
            except (TypeError, ValueError):
                pts = None
            tags = tuple(str(x) for x in (it.get("tags") or []))
            k = (kind, pts, tags)
            g = groups.get(k)
            if g is None:
                groups[k] = {"name": name, "pts": pts, "tags": tags, "n": 1}
            else:
                g["n"] += 1
        order = sorted(groups.items(),
                       key=lambda kv: (kv[0][0],
                                       kv[0][1] if kv[0][1] is not None else 10 ** 9,
                                       kv[0][2]))
        out = []
        for _k, g in order:
            medal = "·".join(self.MEDAL_SHORT.get(x, x) for x in g["tags"]) or "无牌"
            if g["n"] > 1:
                medal += f" ×{g['n']}"
            cap = (f"{g['pts']} 分" if g["pts"] is not None else "—")
            # 三行：蛋名 / 价格 / 奖牌（光放个蛋看不出是啥）
            out.append((c.icon_path("egg", g["name"]), [str(g["name"]), cap, medal]))
        return out

    #: 角色资料缓存有效期（秒）
    ROCOM_TTL = 6 * 3600

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

    @staticmethod
    def _cand_text(c: Dict[str, Any]) -> str:
        """候选一行的文字：助手ID · 在售 N · 好评 N · 差评 N"""
        out = str(c.get("seller") or "")
        lst = c.get("listed")
        out += f" · 在售 {int(lst) if lst not in (None, '') else 0}"
        out += f" · 好评 {int(c.get('likes') or 0)}"
        out += f" · 差评 {int(c.get('dislikes') or 0)}"
        return out

    @staticmethod
    def _sort_candidates(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """同昵称候选排序：**有在售的排前面**，再按 在售 → 售出 → 好评 降序。"""
        cands = [c for c in (data.get("candidates") or []) if isinstance(c, dict)]

        def key(c: Dict[str, Any]):
            listed = int(c.get("listed") or 0)
            return (0 if listed > 0 else 1, -listed,
                    -int(c.get("likes") or 0), int(c.get("dislikes") or 0))

        return sorted(cands, key=key)

    def _merchant_rows(self, data: Dict[str, Any],
                       mark: Optional[Dict[str, Any]] = None,
                       flowers: Optional[List[Dict[str, Any]]] = None) -> List[tuple]:
        """商家卡的行。

        助手ID 只在表格里出现一次（以前标题下面还挂一遍，重复了）。
        游戏UID（含游戏内昵称）来自控制台 roles —— 是 getEggMarketMerchantHome
        里的 roles，一个助手ID 可以绑多个游戏角色。
        """
        m = data.get("merchant") or {}
        seller = str(data.get("seller") or m.get("seller") or "—")
        rows: List[tuple] = [("助手ID", seller)]
        skip_home = False

        roles = [r for r in (data.get("roles") or []) if isinstance(r, dict)]
        first = True
        for r in roles[:6]:
            gid = str(r.get("gameId") or "").strip()
            nm = str(r.get("name") or "").strip()
            if not gid and not nm:
                continue
            txt = f"{gid} · {nm}".strip(" ·") if gid else nm
            rows.append(("游戏UID" if first else "", txt))
            first = False
        if first:                      # 一个都没有
            rows.append(("游戏UID", "未采集"))
        # 角色资料（等级/注册/家园）已经拆到独立的「UID」命令里了，
        # 查ID 只留助手相关内容


        mark_txt = "无"
        if mark:
            mark_txt = "黑名单" if mark.get("t") == "黑" else "白名单"
            if mark.get("note"):
                mark_txt += f" · {mark['note']}"
        rows.append(("标记", mark_txt))

        items = data.get("listings") or []
        listed = m.get("listed")
        if listed in (None, "", 0) and items:
            listed = len(items)
        rows.append(("数据", f"上架 {listed if listed is not None else 0}"
                             f" · 售出 {m.get('sold') or 0}"
                             f" · 好评 {m.get('likes') or 0}"
                             f" · 差评 {m.get('dislikes') or 0}"))

        # 同一个昵称可能对应好多个助手（「访客」有 5000 多个）。
        # 候选只列助手ID；**有在售的排前面**，好让人一眼找到真正在卖的那个。
        cands = self._sort_candidates(data)
        if len(cands) > 1:
            first = True
            for c in cands[:8]:
                cid = str(c.get("seller") or "")
                if not cid or cid == seller:
                    continue
                rows.append(("同名候选" if first else "", self._cand_text(c)))
                first = False

        # 卖家自己在助手上填的备注/留言（他自己写的，别删）
        # 渲染器一格只显示一行，所以按字数切成多行，别截断
        _note = str(m.get("note") or "").strip()
        if _note:
            _chunk = 26
            for _i in range(0, len(_note), _chunk):
                rows.append(("卖家留言" if _i == 0 else "", _note[_i:_i + _chunk]))

        # 上次采集 / 上次在助手上操作（数据变动倒推）
        _seen = data.get("last_seen")
        if _seen:
            rows.append(("上次采集", self._fmt_ts_hm(_seen)))
        _act = []
        if self._cfg("show_activity_changes", False):
            _act = [a for a in (data.get("activity") or []) if isinstance(a, dict)]
        NAMES = {"sold": "售出", "likes": "好评", "followers": "粉丝",
                 "listed": "在售", "dislikes": "差评"}
        if _act:
            last = _act[-1]
            rows.append(("上次变动",
                         f"{NAMES.get(str(last.get('field')), last.get('field'))} "
                         f"{last.get('from')}→{last.get('to')}　"
                         f"{self._fmt_ts_hm(last.get('at'))}"))

        # 这个商家绑过的游戏ID 有没有换绑 / 改名
        for ch in [c for c in (data.get("uid_changes") or []) if isinstance(c, dict)][:6]:
            reason = str(ch.get("reason") or "")
            gid = str(ch.get("game") or "")
            now_sid = str(ch.get("now_seller") or "")
            old_sid = str(ch.get("old_seller") or "")
            if reason == "换绑":
                txt = f"{gid} {old_sid} → {now_sid or '—'}"
            else:
                txt = f"{gid} 「{ch.get('old_role') or ''}」改名"
            rows.append(("改绑记录" if reason == "换绑" else "改名记录", txt))

        # 花种不在 rows 里 —— 卡片下方有专门的色卡段（避免重复显示）
        return rows

    def _merchant_card(self, data: Dict[str, Any],
                       mark: Optional[Dict[str, Any]] = None,
                       avatar: Optional[str] = None,
                       flowers: Optional[List[Dict[str, Any]]] = None,
                       flower_imgs: Optional[Dict[Any, bytes]] = None) -> Optional[bytes]:
        """商家卡：头像 + 信息 + **在售花种（带色卡）** + 在售蛋（图标格）。"""
        try:
            from PIL import Image, ImageDraw, ImageFont
        except Exception:
            return None
        c = _card()
        if c is None or not c.available():
            return None

        kw = self._card_kwargs()
        base_w = int(kw.get("width") or 700)
        base_fs = int(kw.get("font_size") or 18)
        W = max(600, min(1100, base_w))
        PAD = int(base_fs * 1.0)

        try:
            theme = c._norm_theme(kw.get("theme") or "")
            bg = c._to_rgb(theme.get("背景", "#FCFAF6"))
            title_rgb = c._to_rgb(theme.get("标题", "#2C2A26"))
            body_rgb = c._to_rgb(theme.get("正文", "#4A4740"))
            sub_rgb = c._to_rgb(theme.get("次要", "#9A958C"))
            accent_rgb = c._to_rgb(theme.get("强调", "#C25828"))
            line_rgb = (232, 228, 220)
        except Exception:
            bg = (252, 250, 246)
            title_rgb, body_rgb = (44, 42, 38), (74, 71, 64)
            sub_rgb, accent_rgb = (154, 149, 140), (194, 88, 40)
            line_rgb = (232, 228, 220)

        try:
            fpath = c.find_font(str(kw.get("font_path") or ""))
            f_big = ImageFont.truetype(fpath, int(base_fs * 1.45)) if fpath else None
            f_name = ImageFont.truetype(fpath, base_fs) if fpath else None
            f_small = ImageFont.truetype(fpath, max(10, int(base_fs * 0.78))) if fpath else None
            f_tiny = ImageFont.truetype(fpath, max(9, int(base_fs * 0.68))) if fpath else None
        except Exception:
            f_big = f_name = f_small = f_tiny = None
        f_big = f_big or ImageFont.load_default()
        f_name = f_name or f_big
        f_small = f_small or f_name
        f_tiny = f_tiny or f_small

        m = data.get("merchant") or {}
        seller = str(data.get("seller") or m.get("seller") or "—")
        nick = str(m.get("nickname") or m.get("displayName") or seller)

        # ============ 信息行 ============
        info: List[tuple] = []
        stat = []
        for label, v in (("上架", m.get("listed")), ("售出", m.get("sold")),
                         ("好评", m.get("likes")), ("差评", m.get("dislikes"))):
            if v is not None:
                stat.append(f"{label} {v}")
        if stat:
            info.append(("数据", " · ".join(stat)))
        if mark:
            kind = "黑名单" if mark.get("t") == "黑" else "白名单"
            info.append(("标记", kind + (f" · {mark['note']}" if mark.get("note") else "")))
        else:
            info.append(("标记", "无"))
        if m.get("title"):
            info.append(("称号", str(m.get("title"))))

        roles = [r for r in (data.get("roles") or []) if isinstance(r, dict)]
        for r in roles[:6]:
            gid = str(r.get("gameId") or "").strip()
            nm = str(r.get("name") or "").strip()
            if not gid and not nm:
                continue
            info.append(("游戏UID", f"{gid} · {nm}".strip(" ·") if gid else nm))
        if not roles:
            info.append(("游戏UID", "未采集"))

        if m.get("note"):
            note = str(m["note"]).replace("\r", "")
            for k, ln in enumerate(note.split("\n")[:3]):
                info.append(("卖家留言" if k == 0 else "", ln[:60]))

        # ============ 尺寸 ============
        HEAD_H = int(base_fs * 5.4)
        ROW_H = int(base_fs * 1.85)
        SEC_H = int(base_fs * 2.2)

        fl = flowers or []
        FL_CW = int(base_fs * 7.2)
        FL_CH = int(FL_CW / 280 * 154)
        FL_RH = max(FL_CH + 14, int(base_fs * 3.4))
        fl_shown = fl[:8]
        fl_rows = len(fl_shown)
        flowers_h = (SEC_H + fl_rows * FL_RH + (int(base_fs * 1.2) if fl_rows else 0)
                     if fl_rows else 0)

        tiles = self._shelf_tiles(data.get("listings") or [])
        TILE = int(base_fs * 5.2)
        PER_ROW = 4
        MAX_ROWS = 2
        tiles_shown = tiles[:PER_ROW * MAX_ROWS]
        tile_rows = ((len(tiles_shown) + PER_ROW - 1) // PER_ROW
                     if tiles_shown else 0)
        tiles_h = (SEC_H + tile_rows * (TILE + int(base_fs * 1.3))
                   + ((int(base_fs * 1.8) + int(base_fs * 1.2))
                      if len(tiles) > len(tiles_shown) else 0)
                   if tile_rows else 0)

        info_h = ROW_H * len(info)
        H = PAD + HEAD_H + info_h + 12 + flowers_h + tiles_h + PAD

        img = Image.new("RGB", (W, H), bg)
        dr = ImageDraw.Draw(img)

        # ============ 头部 ============
        y = PAD
        head_size = int(HEAD_H * 0.72)
        drew_head = False
        if avatar:
            try:
                aim = Image.open(avatar).convert("RGBA")
                aim = aim.resize((head_size, head_size), Image.LANCZOS)
                mask = Image.new("L", (head_size, head_size), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, head_size, head_size), fill=255)
                img.paste(aim, (PAD, y + (HEAD_H - head_size) // 2), mask)
                drew_head = True
            except Exception:
                pass
        if not drew_head:
            try:
                egg_icon = c.icon_path("egg", "神奇蛋")
                if egg_icon:
                    eim = Image.open(egg_icon).convert("RGBA")
                    eim = eim.resize((head_size, head_size), Image.LANCZOS)
                    img.paste(eim, (PAD, y + (HEAD_H - head_size) // 2), eim)
                    drew_head = True
            except Exception:
                pass

        tx = PAD + (head_size + int(base_fs * 0.9) if drew_head else 0)
        dr.text((tx, y + int(HEAD_H * 0.16)), _strip_emoji(nick),
                font=f_big, fill=title_rgb)
        dr.text((tx, y + int(HEAD_H * 0.62)), f"助手ID {seller}",
                font=f_small, fill=sub_rgb)
        y += HEAD_H
        dr.line([(PAD, y), (W - PAD, y)], fill=line_rgb, width=1)
        y += 8

        # ============ 信息行 ============
        LABEL_W = int(base_fs * 4.6)
        for label, val in info:
            if label:
                dr.text((PAD + 4, y + 2), _strip_emoji(label),
                        font=f_small, fill=sub_rgb)
            if val:
                avail = W - PAD * 2 - LABEL_W
                cur, lines = "", []
                for ch in str(val):
                    if dr.textlength(cur + ch, font=f_name) > avail:
                        lines.append(cur)
                        cur = ch
                    else:
                        cur += ch
                if cur:
                    lines.append(cur)
                for k, ln in enumerate(lines[:2]):
                    dr.text((PAD + LABEL_W, y + 2 + k * int(base_fs * 1.1)),
                            _strip_emoji(ln), font=f_name, fill=body_rgb)
                if len(lines) > 2:
                    y += int(base_fs * 1.1)
            y += ROW_H
        y += 12

        # ============ 在售花种（带色卡）—— 放蛋货架前面 ============
        if fl_rows:
            dr.text((PAD + 4, y), _strip_emoji(f"在售花种 {len(fl)} 个"),
                    font=f_small, fill=accent_rgb)
            y += SEC_H
            imgs = flower_imgs or {}
            for b in fl_shown:
                cy = y + (FL_RH - FL_CH) // 2
                placed = False
                # 异色：画异色精灵图标（异色没有炫彩色卡）
                thumb = self._flower_thumb_path(b)
                if thumb:
                    try:
                        tim = Image.open(thumb).convert("RGBA")
                        side = min(FL_CH, FL_CW)
                        tim = tim.resize((side, side), Image.LANCZOS)
                        img.paste(tim, (PAD + (FL_CW - side) // 2,
                                        cy + (FL_CH - side) // 2), tim)
                        placed = True
                    except Exception:
                        placed = False
                raw = None
                if not placed:
                    raw = imgs.get(id(b)) or imgs.get(
                        (b.get("particle_id"), b.get("color_id")))
                if raw:
                    try:
                        import io as _io
                        oim = Image.open(_io.BytesIO(raw)).convert("RGBA")
                        oim = oim.resize((FL_CW, FL_CH), Image.LANCZOS)
                        img.paste(oim, (PAD, cy), oim)
                        placed = True
                    except Exception:
                        pass
                if not placed:
                    c1 = self._hex_to_rgb(b.get("c1"), (222, 222, 222))
                    c2 = self._hex_to_rgb(b.get("c2"), (245, 245, 245))
                    hw = FL_CW // 2
                    dr.rectangle([PAD, cy, PAD + hw - 2, cy + FL_CH], fill=c1)
                    dr.rectangle([PAD + hw + 2, cy, PAD + FL_CW, cy + FL_CH], fill=c2)

                tx2 = PAD + FL_CW + int(base_fs * 0.7)
                look = str(b.get("appearance_txt") or "未标注")
                sp = str(b.get("species") or b.get("name") or "")
                # 第一行：外观 + 精灵名
                dr.text((tx2, y + 2), _strip_emoji(look), font=f_small,
                        fill=accent_rgb)
                lw = dr.textlength(f"{look} ", font=f_small)
                dr.text((tx2 + lw, y + 2), _strip_emoji(sp),
                        font=f_name, fill=body_rgb)
                # 第二行：颜色（异色没有色卡 → 这行空着，不写「颜色未知」）
                _c2 = self._flower_color_text(b)
                if _c2:
                    dr.text((tx2, y + 4 + int(base_fs * 1.15)),
                            _strip_emoji(_c2), font=f_small, fill=sub_rgb)
                # 第三行：粒子 + 价格
                pn = str(b.get("particle_name") or "")
                price = f"{b.get('price')} 分" if b.get("price") is not None else "?"
                dr.text((tx2, y + 4 + int(base_fs * 2.2)),
                        _strip_emoji(f"{pn + ' · ' if pn else ''}{price}"),
                        font=f_small, fill=accent_rgb)
                y += FL_RH
            if len(fl) > fl_rows:
                dr.text((PAD + 4, y), f"…… 还有 {len(fl) - fl_rows} 个",
                        font=f_tiny, fill=sub_rgb)
                y += int(base_fs * 1.2)

        # ============ 在售蛋（图标格）============
        if tile_rows:
            dr.text((PAD + 4, y), _strip_emoji(f"在售蛋 {len(tiles)} 件"),
                    font=f_small, fill=accent_rgb)
            y += SEC_H
            for i, item in enumerate(tiles_shown):
                col = i % PER_ROW
                row = i // PER_ROW
                cx = PAD + col * (TILE + int(base_fs * 0.5))
                cy = y + row * (TILE + int(base_fs * 1.3))
                try:
                    path = item[0]
                    lines = item[1] if len(item) > 1 else []
                except Exception:
                    path, lines = item, []
                if path and str(path).lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    try:
                        im = Image.open(path).convert("RGBA")
                        im = im.resize((TILE, TILE), Image.LANCZOS)
                        img.paste(im, (cx, cy), im)
                    except Exception:
                        pass
                for k, ln in enumerate((lines or [])[:2]):
                    dr.text((cx, cy + TILE + 1 + k * int(base_fs * 0.85)),
                            _strip_emoji(str(ln))[:14], font=f_tiny, fill=body_rgb)
            y += tile_rows * (TILE + int(base_fs * 1.3))
            if len(tiles) > len(tiles_shown):
                y += int(base_fs * 1.8)          # 和蛋格说明拉开几行
                dr.text((PAD + 4, y), f"…… 还有 {len(tiles) - len(tiles_shown)} 种",
                        font=f_tiny, fill=sub_rgb)
                y += int(base_fs * 1.2)

        return c._png_bytes(img)

    def _text_card(self, text: str, icons=None) -> Optional[bytes]:
        """把任意一段文字画成卡片（和查蛋同款样式）。命中缓存直接返回。"""
        c = _card()
        if c is None or not c.available():
            return None
        text = _strip_emoji(text)
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

    #: 孵蛋预测接口（公开计算服务，不需要登录、不用 key）
    IDENTIFY_API = "https://cinene.cloud/egg/api/compute"

    async def _identify_probs(self, h: float, w: float):
        """问孵蛋预测接口要这颗蛋的**真实孵化概率**。

        接口：POST https://cinene.cloud/egg/api/compute
            {"attribute": "孵蛋预测", "data": "0.27 3.0680"}   ← 米 + 千克
            返回 {"result": "【0.27/3.0680】\n可能精灵：\n1. 呼呼猪 (69.8%)..."}

        返回 ({"top": {名字: %}, "special": {名字: %}}, 状态)：
            "ok" / "empty" / "down" / "off"
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
        """把概率接口给的精灵名换成本地图鉴的一条候选。

        接口给的是准确名字 → 严格匹配，本地没有就跳过（别拿模糊凑）。
        """
        rec = self._find_species(name, strict=True)
        if not rec:
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
            out: List[Dict[str, Any]] = []
            for nm, pct in sorted((probs.get("top") or {}).items(), key=lambda kv: -kv[1]):
                if pct < min_pct:
                    continue
                item = self._hit_from_name(nm)
                if item:
                    item["pct"] = pct
                    out.append(item)
            show_special = bool(self._cfg("identify_show_special", False))
            tail = "" if min_pct <= 0 else f" · 低于 {min_pct:g}% 的已隐藏"
            if out and show_special:
                # 开关打开：普通 + 特殊一起列
                extra2: List[Dict[str, Any]] = []
                for nm, pct in sorted((probs.get("special") or {}).items(),
                                      key=lambda kv: -kv[1]):
                    if pct < min_pct:
                        continue
                    item = self._hit_from_name(nm)
                    if item:
                        item["pct"] = pct
                        item["special"] = True
                        extra2.append(item)
                if extra2:
                    return out + extra2, f"实时孵化概率（含特殊精灵）{tail}"
                return out, f"实时孵化概率{tail}"
            if out:
                return out, f"实时孵化概率{tail}"
            if not show_special:
                return [], (f"实时概率里没有超过 {min_pct:g}% 的普通蛋候选"
                            f"（特殊精灵默认不显示，可在配置里打开 identify_show_special）")
            # 普通蛋池没有 → 看「特殊精灵」
            extra: List[Dict[str, Any]] = []
            for nm, pct in sorted((probs.get("special") or {}).items(), key=lambda kv: -kv[1]):
                if pct < min_pct:
                    continue
                item = self._hit_from_name(nm)
                if item:
                    item["pct"] = pct
                    extra.append(item)
            if extra:
                tail = "" if min_pct <= 0 else f" · 低于 {min_pct:g}% 的已隐藏"
                return extra, f"特殊精灵（不在普通蛋池）· 实时概率{tail}"
            return [], f"实时概率里没有超过 {min_pct:g}% 的候选"
        if state == "empty":
            return [], "接口没找到匹配的精灵（也可能刚好卡在区间边界，比如正好等于上限）"
        if state == "off":
            return local, "按本地区间匹配（实时概率已在配置里关闭）"
        return local, "概率接口不可用，下面是本地区间匹配（可能含低概率品种）"

    # ---------------------------------------------------------------- 新上架提醒
    def _cfg_watch_ids(self) -> List[str]:
        return [x.strip().upper()
                for x in str(self._cfg("watch_sellers", "") or "")
                .replace("，", ",").split(",") if x.strip()]

    def _legacy_watch(self) -> List[str]:
        """旧版（≤1.87，以及老插件 astrbot_plugin_lk_market_notify）写在
        `state["watch"]` 里的监控名单。

        **新版不再认它**。那个数组只在旧代码里生效：
          · 配置 `watch_sellers` 清空 → 动不到它
          · 发「新上架 删除」→ 动不到它（旧版根本没这个指令）
        于是表现为「配置清空了、列表也删了，它还在监控 Z10014514 拼命拉货架」。
        这里只用来提示 + 清理。
        """
        return [str(x).strip().upper() for x in (self.state.get("watch") or []) if x]

    def _drop_legacy_watch(self) -> List[str]:
        """把旧版残留的 watch 清掉（只清一次，会记日志）。返回清掉的内容。"""
        legacy = self._legacy_watch()
        if not legacy:
            return []
        self.state["watch"] = []
        cleaned = dict(self.state.get("watch_legacy_cleaned") or {})
        for sid in legacy:
            cleaned[sid] = time.time()
        self.state["watch_legacy_cleaned"] = cleaned
        _save_state(self.state)
        logger.warning(
            f"[市集推送] 检测到旧版残留 watch={legacy} —— 旧版代码只认这个数组，"
            f"清配置/发删除指令都动不到它，这就是「删了还在推」的真凶。"
            f"已清空，不再监控。要监控请用「新上架 添加 Z…」或配置 watch_sellers")
        return legacy

    def _watch(self) -> List[str]:
        """监控名单 = (配置 watch_sellers ∪ 新上架 添加) − 新上架 删除。

        删除必须能压过配置 —— 否则你在插件设置里填过的商家，用指令删了
        下一轮又被配置加回来，删了照样推。规则：

            watch_ops[sid]      = 新上架 添加 的时间
            watch_deleted[sid]  = 新上架 删除 的时间
            删除时间 > 添加时间 → 不在名单（哪怕配置里写了）

        旧版的 `state["watch"]` 数组**不参与**（见 `_legacy_watch`）。
        """
        self._drop_legacy_watch()
        cfg_ids = self._cfg_watch_ids()
        added = dict(self.state.get("watch_ops") or {})
        deleted = dict(self.state.get("watch_deleted") or {})

        def _f(d: Dict[str, Any], k: str) -> float:
            try:
                return float(d.get(k) or 0)
            except (TypeError, ValueError):
                return 0.0

        def _alive(sid: str) -> bool:
            a = _f(added, sid)
            d = _f(deleted, sid)
            if d > a:
                return False                 # 删得比加得晚 → 死了
            if a > 0:
                return True                  # 指令加过且没被删
            return sid in cfg_ids            # 只剩配置里那份

        out: List[str] = []
        for sid in list(added.keys()) + cfg_ids:
            if sid and sid not in out and _alive(sid):
                out.append(sid)
        return out

    def _set_watch(self, sid: str, on: bool) -> bool:
        """加/删监控。返回操作前它是否在名单里。"""
        sid = str(sid or "").strip().upper()
        if not sid:
            return False
        was = sid in self._watch()
        added = dict(self.state.get("watch_ops") or {})
        deleted = dict(self.state.get("watch_deleted") or {})
        now = time.time()
        # 老版本那个 list 也要清，否则 _alive() 会把它当「很早就加了」
        self.state["watch"] = [x for x in (self.state.get("watch") or [])
                               if str(x).strip().upper() != sid]
        if on:
            added[sid] = now
            deleted.pop(sid, None)
        else:
            deleted[sid] = now
            added.pop(sid, None)
        self.state["watch_ops"] = added
        self.state["watch_deleted"] = deleted
        _save_state(self.state)
        logger.info(f"[市集推送] 监控{'添加' if on else '删除'} {sid}"
                    f" → 现在监控 {len(self._watch())} 个")
        return was

    #: `listing_template` / `template` 填这些值 = 直接关掉那条推送。
    #: 注意：**留空不等于关闭** —— 留空是「用内置排版」，照样会推。
    #: 用户把模板内容删干净了还以为关了，就是踩这个坑。
    OFF_VALUES = ("关", "关闭", "不推", "关闭推送", "别推", "off", "false", "no", "none", "0", "-")

    def _tpl_off(self, key: str) -> bool:
        raw = str(self._cfg(key, "") or "").strip().lower()
        return raw in [x.lower() for x in self.OFF_VALUES]

    def _render_listing(self, seller: str, nickname: str, items: List[Dict[str, Any]],
                        total: int) -> str:
        tpl = str(self._cfg("listing_template", "") or "")
        lines = [f"🆕 新上架提醒 · {nickname or seller}（{seller}）"]
        for it in items[:10]:
            tags = "·".join(it.get("tags") or [])
            lock = " 🔒" if it.get("password") else ""
            lines.append(f"· {it.get('name')} {it.get('points')} 分"
                         + (f" [{tags}]" if tags else "") + lock)
        if len(items) > 10:
            lines.append(f"…… 还有 {len(items) - 10} 件")
        lines.append(f"当前在售：{total} 件")
        body = "\n".join(lines)
        return tpl.format(seller=seller, nickname=nickname or seller,
                          items=body, total=total) if tpl else body

    async def _listing_items(self, sid: str) -> List[Dict[str, Any]]:
        """拉一个商家的实时货架。

        注意：服务端不认 `page` 参数（第 2 页返回的跟第 1 页一样），
        所以别翻页 —— 翻了只会拿到重复数据。一次最多给 30 条。
        """
        data = await self._api(f"/api/merchants/listings?seller={sid}")
        if not data or not data.get("ok"):
            return []
        return [x for x in (data.get("items") or []) if isinstance(x, dict)]

    #: 已推记录保留时长（秒）。以前是 2 天，会害人：
    #: 货架接口一次只给**最新 30 条**，热门商家一上新，老挂单就掉出窗口；
    #: 掉出去 2 天后再回窗口 → id 早被清掉了 → 又被当成「新上架」推一遍。
    LISTING_KEEP = 30 * 86400

    @staticmethod
    def _listing_sig(sid: str, it: Dict[str, Any]) -> str:
        """同一颗蛋的「内容指纹」（换挂单 id 也不变）。

        卖家把同一颗蛋下架再重新上架，服务端会发新 id，
        只按 id 去重就会重复推 —— 这里再用内容兜一层。
        """
        raw = "|".join([sid] + [str(x) for x in (
            it.get("name"), it.get("points"), it.get("weight_g"),
            it.get("height_cm"), it.get("nature"),
            ",".join(it.get("tags") or []), it.get("note"))])
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]

    async def _poll_listings(self) -> int:
        """逐个监控商家拉实时货架，发现新挂单就推送。

        **同一件商品只推一次**，靠四件事保证：
          1. pushed_ids / pushed_sig 每次都从**磁盘**读（不信任内存副本）——
             AstrBot 热重载 / 起两个实例时，各记各的内存状态就会重复推
          2. **先写盘再推送** —— 推送过程中崩了也不会重推
          3. 还在货架上的挂单每轮「续命」，30 天内不掉记录 ——
             掉出 30 条窗口再回来也不会当成新上架
          4. 内容指纹兜底：同一颗蛋重新上架换了 id，同样不推
        """
        sent = 0
        if self._tpl_off("listing_template"):
            if not self.stats.get("listing_off_logged"):
                self.stats["listing_off_logged"] = 1
                logger.info("[市集推送] listing_template = 关 → 新上架提醒整条关掉，不推也不拉货架")
            return 0
        watch = self._watch()
        if not watch:
            return 0

        now = time.time()
        keep = self.LISTING_KEEP
        for sid in watch:
            items = await self._listing_items(sid)
            if not items:
                continue

            disk = _load_state()
            pushed: Dict[str, Any] = dict(disk.get("pushed_ids") or {})
            sigs: Dict[str, Any] = dict(disk.get("pushed_sig") or {})
            for k in [k for k, ts in pushed.items()
                      if now - float(ts or 0) > keep]:
                pushed.pop(k, None)
            for k in [k for k, ts in sigs.items()
                      if now - float(ts or 0) > keep]:
                sigs.pop(k, None)

            fresh: List[Dict[str, Any]] = []
            for it in items:
                lid = str(it.get("id") or "")
                if not lid:
                    continue
                sig = self._listing_sig(sid, it)
                if lid in pushed or sig in sigs:
                    # 见过：续命，别让 30 天清理把还在货架上的老挂单忘掉
                    pushed[lid] = now
                    sigs[sig] = now
                    continue
                fresh.append(it)
                pushed[lid] = now
                sigs[sig] = now

            # 先落盘，再推送
            disk["pushed_ids"] = pushed
            disk["pushed_sig"] = sigs
            self.state["pushed_ids"] = pushed
            self.state["pushed_sig"] = sigs
            _save_state(disk)

            if fresh:
                text = self._render_listing(sid, "", fresh, len(items))
                if await self._send(text, kind="listing"):
                    sent += 1
                    self.stats["sent"] += 1
                    logger.info(f"[市集推送] {sid} 新上架 {len(fresh)} 件"
                                f"（已推 id {len(pushed)} 个）")

        return sent

    # ---------------------------------------------------------------- 黑白名单
    def _marks(self) -> Dict[str, Any]:
        return self.state.setdefault("marks", {})

    def _set_mark(self, sid: str, kind: str, note: str = "", by: str = "") -> Dict[str, Any]:
        sid = sid.strip().upper()
        rec = {"t": kind, "note": note.strip(), "at": int(time.time()), "by": by}
        self._marks()[sid] = rec
        _save_state(self.state)
        return rec

    def _del_mark(self, sid: str) -> bool:
        sid = sid.strip().upper()
        if sid in self._marks():
            self._marks().pop(sid, None)
            _save_state(self.state)
            return True
        return False

    def _marks_of(self, kind: str) -> List[tuple]:
        return [(sid, rec) for sid, rec in self._marks().items()
                if rec.get("t") == kind]

    async def _seller_info(self, sid: str) -> Dict[str, Any]:
        """从控制台库里补一点信息（昵称/角色/在售）。"""
        if self._market_paused():
            return {}
        data = await self._api(f"/api/merchants/search?q={sid}&limit=1&auto=1")
        rows = (data or {}).get("rows") or []
        return rows[0] if rows else {}

    async def _live_merchant(self, q: str) -> Optional[Dict[str, Any]]:
        """实时查商家（和网页版同一接口），20 秒超时，失败再退回本地索引。"""
        try:
            data = await asyncio.wait_for(
                self._api("/api/merchant?q=" + urllib.parse.quote(str(q))),
                timeout=20)
            if data and data.get("ok"):
                return data
        except Exception as e:
            logger.warning(f"[市集推送] 实时查商家失败: {e}")
        row = await self._seller_info(q)
        if row:
            return {"ok": True, "seller": row.get("seller"),
                    "merchant": {"nickname": row.get("nickname"),
                                 "listed": row.get("listed"),
                                 "sold": row.get("sold"),
                                 "likes": row.get("likes"),
                                 "dislikes": row.get("dislikes")},
                    "listings": row.get("catalog") or []}
        return None

    # ---------------------------------------------------------------- 身高/体重 查蛋
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

    #: 查ID 里最多列多少个绑定角色（多的省略，避免卡片太长）
    LOOKUP_ROLE_LIMIT = 15

    def _merchant_block(self, d: Dict[str, Any],
                        mark: Optional[Dict[str, Any]] = None,
                        flowers: Optional[List[Dict[str, Any]]] = None) -> str:
        """名字 + 助手ID + 统计 + 绑定角色 + 蛋货架 + 花种货架。"""
        m = d.get("merchant") or {}
        seller = d.get("seller") or m.get("seller") or "—"
        nick = m.get("nickname") or m.get("displayName") or "—"

        def _v(x):
            return 0 if x in (None, "", "—") else x

        listed, sold = _v(m.get("listed")), _v(m.get("sold"))
        likes, dislikes = _v(m.get("likes")), _v(m.get("dislikes"))
        items = d.get("listings") or []
        if listed == 0 and items:
            listed = len(items)

        out = [f"{nick}（{seller}）"]
        if mark:
            kind = "黑名单" if mark.get("t") == "黑" else "白名单"
            out.append(f"标记：{kind}" + (f" · {mark['note']}" if mark.get("note") else ""))
        else:
            out.append("标记：无")
        out.append(f"上架 {listed} · 售出 {sold} · 好评 {likes} · 差评 {dislikes}")

        # 绑定角色（游戏 UID）
        roles = [r for r in (d.get("roles") or []) if isinstance(r, dict)]
        if roles:
            out.append(f"———— 绑定 {len(roles)} 个角色 ————")
            lim = int(getattr(self, "LOOKUP_ROLE_LIMIT", 15) or 15)
            for r in roles[:lim]:
                gid = str(r.get("gameId") or r.get("game_id") or "")
                nm = str(r.get("name") or "—")
                out.append(f"· {gid} {nm}" if gid else f"· {nm}")
            if len(roles) > lim:
                out.append(f"…… 还有 {len(roles) - lim} 个")
        elif d.get("seller"):
            out.append("———— 还没扫到绑定角色 ————")

        # 花种货架（外观放精灵名前面）
        fl = flowers or []
        if fl:
            out.append(f"———— 在售花种 {len(fl)} 个 ————")
            for b in fl[:12]:
                look = str(b.get("appearance_txt") or "未标注")
                sp = str(b.get("species") or b.get("name") or "")
                bits = []
                _ctxt = self._flower_color_text(b)
                if _ctxt:
                    bits.append(_ctxt)
                if b.get("particle_name"):
                    bits.append(str(b["particle_name"]))
                price = b.get("price")
                tail = f"{price} 分" if price is not None else "?"
                head = f"{look} {sp}".strip()
                body_txt = (" · ".join(bits) + " · ") if bits else ""
                out.append(f"· {head} · {body_txt}{tail}")
            if len(fl) > 12:
                out.append(f"…… 还有 {len(fl) - 12} 个")
        if items:
            out.append(f"———— 在售 {len(items)} 件 ————")
            for it in items[:15]:
                nm = it.get("name") or "—"
                nat = it.get("nature") or "—"
                tags = "·".join(it.get("tags") or []) or "无牌"
                lock = " 🔒" if it.get("password") else ""
                out.append(f"· {nm} · {nat} · {tags} · {it.get('points')} 分{lock}")
            if len(items) > 15:
                out.append(f"…… 还有 {len(items) - 15} 件")
        else:
            out.append("———— 当前没有在售 ————")

        return "\n".join(out)

    async def _describe_mark(self, sid: str, rec: Dict[str, Any]) -> str:
        info = await self._seller_info(sid)
        kind = "黑名单" if rec.get("t") == "黑" else "白名单"
        out = [f"{sid} {kind}"]
        if rec.get("note"):
            out.append(f"备注：{rec['note']}")
        if info and info.get("nickname"):
            out.append(f"平台昵称：{info['nickname']}")
            out.append(f"在售 {info.get('listed', 0)} · 售出 {info.get('sold', 0)}")
        if rec.get("at"):
            out.append("标记时间：" + time.strftime("%Y-%m-%d %H:%M",
                                                time.localtime(rec["at"])))
        return "\n".join(out)


    # ---------------------------------------------------------------- 花种渲染/命令
    def _render_flower_offers(self, species: str, offers: List[Dict[str, Any]]) -> str:
        """把某个花种的挂单渲染成文字。"""
        if not offers:
            return f"🌸 {species}\n市面上暂时没有在售的花种。"
        lines = [f"🌸 {species} · 市面 {len(offers)} 个"]
        for i, o in enumerate(offers, 1):
            b = self._flower_brief(o)
            head = [f"{b['appearance_txt']}", f"{self.FLOWER_TYPE.get(b['type'], b['type'])}"]
            price = b.get("price")
            _ctxt = self._flower_color_text(b)
            _left = ([_ctxt] if _ctxt else []) + head
            lines.append(f"{i}. " + " · ".join(_left)
                         + f" · {price if price is not None else '?'} 分")
            col = (f"   色卡 {b['c1']} / {b['c2']}"
                   if (b["c1"] or b["c2"]) and not self._flower_is_shiny(b) else "")
            if col:
                lines.append(col)
            if b["particle_name"]:
                lines.append(f"   粒子 {b['particle_name']}")
            seller_txt = b["seller"] or "—"
            if b.get("seller_id"):
                seller_txt = f"{seller_txt}（{b['seller_id']}）"
            lines.append(f"   卖家 {seller_txt}"
                         + (f" · 成团 {b['people']}/{b['min_people']}"
                            if b.get("min_people") else ""))
            if b.get("remark"):
                lines.append(f"   备注 {b['remark']}")
        return "\n".join(lines)

    def _flower_is_shiny(self, b: Dict[str, Any]) -> bool:
        """这条花种是不是异色（异色没有炫彩色卡）。"""
        return str(b.get("appearance") or "").strip().lower() == "shiny"

    #: 明确是粒子名的词（用来判断「这是在问粒子」，别拿「独角兽」误判）
    PARTICLE_HINT_WORDS = (
        "四角星", "五角星", "六角星", "三角星", "星星", "星型", "星形",
        "十字星", "星芒", "方块", "方形", "正方形", "方型", "矩形",
        "菱形", "四边形", "爱心", "心形", "心型", "桃心", "红心",
        "雪花", "闪光", "闪亮", "泡泡", "气泡", "花瓣", "圆点", "圆形",
        "十字", "月牙", "闪电", "三角形",
    )

    def _looks_particle(self, q: str) -> bool:
        """这个词是不是在明确问粒子（而不是精灵名）。

        必须用完整词判断 —— 用单字「角」会把「独角兽」当成粒子。
        """
        s = str(q or "").strip()
        if not s or len(s) > 6:
            return False
        if s in self.FLOWER_PARTICLES:
            return True
        for _canon, aliases in self.FLOWER_PARTICLE_ALIAS.items():
            if s in aliases:
                return True
        return any(w in s for w in self.PARTICLE_HINT_WORDS)

    def _flower_color_text(self, b: Dict[str, Any]) -> str:
        """颜色文字。

        异色本来就没有炫彩色卡 → 不给「颜色未知」这种误导性的东西，直接留空
        （外观那栏已经写了「异色」）；炫彩才显示颜色名。
        """
        if self._flower_is_shiny(b):
            return ""
        return str(b.get("color_name") or "颜色未知")

    def _flower_thumb_path(self, b: Dict[str, Any]) -> Optional[str]:
        """异色花种 → 异色精灵图标路径；炫彩/其它 → None（用色卡）。"""
        if not self._flower_is_shiny(b):
            return None
        c = _card()
        if c is None:
            return None
        nm = str(b.get("species") or b.get("name") or "").strip()
        if not nm:
            return None
        try:
            p = c.icon_path("shiny", nm)
            if p:
                return p
            return c.icon_path("pet", nm)
        except Exception:
            return None

    @staticmethod
    def _hex_to_rgb(v: str, fallback=(200, 200, 200)) -> tuple:
        """#RRGGBB → (r, g, b)。"""
        s = str(v or "").strip().lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if len(s) != 6:
            return fallback
        try:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        except Exception:
            return fallback

    @staticmethod
    def _draw_particle(dr, cx: int, cy: int, size: int, kind: str, color: tuple):
        """按粒子名画示意图形（四角星 / 方块 / 爱心 / 其它圆点）。"""
        h = size / 2.0
        if "四角星" in kind:
            dr.polygon([(cx, cy - h), (cx + h * 0.30, cy),
                        (cx, cy + h), (cx - h * 0.30, cy)], fill=color)
            dr.polygon([(cx - h, cy), (cx, cy + h * 0.30),
                        (cx + h, cy), (cx, cy - h * 0.30)], fill=color)
        elif "方块" in kind:
            dr.rectangle([cx - h * 0.72, cy - h * 0.72,
                          cx + h * 0.72, cy + h * 0.72], fill=color)
        elif "爱心" in kind:
            r = h * 0.46
            dr.ellipse([cx - r * 2.2, cy - r * 1.7,
                        cx - r * 0.2, cy + r * 0.3], fill=color)
            dr.ellipse([cx + r * 0.2, cy - r * 1.7,
                        cx + r * 2.2, cy + r * 0.3], fill=color)
            dr.polygon([(cx - r * 2.05, cy - r * 0.35),
                        (cx + r * 2.05, cy - r * 0.35),
                        (cx, cy + h * 0.95)], fill=color)
        else:
            dr.ellipse([cx - h * 0.62, cy - h * 0.62,
                        cx + h * 0.62, cy + h * 0.62], fill=color)

    async def _flower_card(self, species: str,
                            offers: List[Dict[str, Any]]) -> Optional[bytes]:
        """花种卡片：左边两个色块 + 粒子图形，右边颜色名 / 价格 / 卖家。

        配色跟蛋神里一致：用接口返回的 c1 / c2 两个色值画色块。
        """
        try:
            from PIL import Image, ImageDraw, ImageFont
        except Exception as e:
            logger.warning(f"[出图] 花种卡缺 Pillow: {e}")
            return None
        c = _card()
        if c is None or not c.available():
            return None

        kw = self._card_kwargs()
        base_w = int(kw.get("width") or 700)
        base_fs = int(kw.get("font_size") or 18)
        W = max(560, min(1100, base_w))

        rows = [self._flower_brief(o) for o in offers]
        ROW_H = int(base_fs * 5.2)          # 每行高度随字号缩放
        HEAD_H = int(base_fs * 3.6)
        PAD = int(base_fs * 0.9)
        SW = int(base_fs * 3.1)             # 单个色块边长
        GAP = max(3, int(base_fs * 0.16))   # 两色块间距

        H = HEAD_H + ROW_H * len(rows) + PAD

        # 主题背景色
        try:
            theme = c._norm_theme(kw.get("theme") or "")
            bg = c._to_rgb(theme.get("背景", "#FCFAF6"))
            card_bg = c._to_rgb(theme.get("卡片", "#FFFFFF"))
            title_rgb = c._to_rgb(theme.get("标题", "#2C2A26"))
            body_rgb = c._to_rgb(theme.get("正文", "#4A4740"))
            sub_rgb = c._to_rgb(theme.get("次要", "#9A958C"))
            accent_rgb = c._to_rgb(theme.get("强调", "#C25828"))
        except Exception:
            bg, card_bg = (252, 250, 246), (255, 255, 255)
            title_rgb, body_rgb = (44, 42, 38), (74, 71, 64)
            sub_rgb, accent_rgb = (154, 149, 140), (194, 88, 40)

        img = Image.new("RGB", (W, H), bg)
        dr = ImageDraw.Draw(img)

        # 字体
        try:
            fpath = c.find_font(str(kw.get("font_path") or ""))
            f_title = ImageFont.truetype(fpath, int(base_fs * 1.35)) if fpath else None
            f_name = ImageFont.truetype(fpath, base_fs) if fpath else None
            f_small = ImageFont.truetype(fpath, max(10, int(base_fs * 0.78))) if fpath else None
        except Exception:
            f_title = f_name = f_small = ImageFont.load_default()

        def F(f, fallback=None):
            return f if f is not None else (fallback or ImageFont.load_default())

        f_title = F(f_title)
        f_name = F(f_name, f_title)
        f_small = F(f_small, f_name)

        # 标题
        dr.text((PAD, int(HEAD_H * 0.34)),
                _strip_emoji(f"🌸 {species} · {len(rows)} 个花种"),
                font=f_title, fill=title_rgb)
        dr.line([(PAD, HEAD_H - 6), (W - PAD, HEAD_H - 6)], fill=(232, 228, 220), width=1)

        for i, b in enumerate(rows):
            y = HEAD_H + i * ROW_H
            # 隔行底色
            if i % 2 == 0:
                dr.rectangle([PAD - 6, y + 2, W - PAD + 6, y + ROW_H - 4],
                             fill=card_bg)

            c1 = self._hex_to_rgb(b["c1"], (222, 222, 222))
            sx = PAD
            sy = y + (ROW_H - SW) // 2

            # 异色 → 异色精灵图标；炫彩 → 官方色卡图
            card_img = None
            placed = False
            _thumb = self._flower_thumb_path(b)
            if _thumb:
                try:
                    _tim = Image.open(_thumb).convert("RGBA")
                    _side = min(SW * 2 + GAP, ROW_H - 8)
                    _tim = _tim.resize((_side, _side), Image.LANCZOS)
                    img.paste(_tim, (sx + (SW * 2 + GAP - _side) // 2,
                                     y + (ROW_H - _side) // 2), _tim)
                    placed = True
                except Exception:
                    placed = False
            if not placed:
                card_img = await self._chroma_card_bytes(b.get("particle_id"),
                                                         b.get("color_id"))
            if card_img:
                try:
                    import io as _io
                    oim = Image.open(_io.BytesIO(card_img)).convert("RGBA")
                    # 按比例缩放到色卡位（高 SW，宽按原图比例）
                    tw2 = int(SW * 2 + GAP)
                    th2 = int(tw2 * oim.height / max(1, oim.width))
                    if th2 > ROW_H - 8:
                        th2 = ROW_H - 8
                        tw2 = int(th2 * oim.width / max(1, oim.height))
                    oim = oim.resize((max(1, tw2), max(1, th2)), Image.LANCZOS)
                    img.paste(oim, (sx, y + (ROW_H - th2) // 2), oim)
                    placed = True
                except Exception as e:
                    logger.warning(f"[花种] 贴官方色卡图失败: {type(e).__name__}: {e}")
            if not placed:
                # 兜底：用色值画两个方块
                c2 = self._hex_to_rgb(b["c2"], (245, 245, 245))
                dr.rectangle([sx, sy, sx + SW, sy + SW], fill=c1,
                             outline=(225, 222, 215), width=1)
                dr.rectangle([sx + SW + GAP, sy, sx + SW * 2 + GAP, sy + SW], fill=c2,
                             outline=(225, 222, 215), width=1)

            tx = sx + SW * 2 + GAP + int(base_fs * 0.75)

            # 第一行：颜色名 + 外观 + 类型
            name = self._flower_color_text(b) or b.get("species") or b.get("name") or ""
            tag = f"{b['appearance_txt']} · {self.FLOWER_TYPE.get(b['type'], b['type'])}"
            dr.text((tx, y + int(ROW_H * 0.13)), name, font=f_name, fill=body_rgb)
            # 外观标签右对齐
            tw = dr.textlength(tag, font=f_small)
            dr.text((W - PAD - tw, y + int(ROW_H * 0.17)), tag,
                    font=f_small, fill=accent_rgb)

            # 第二行：色值
            dr.text((tx, y + int(ROW_H * 0.42)),
                    f"{b['c1'] or '—'} / {b['c2'] or '—'}",
                    font=f_small, fill=sub_rgb)

            # 第三行：粒子 + 价格 + 卖家
            py = y + int(ROW_H * 0.70)
            if b.get("particle_name"):
                # 粒子小图形
                psz = max(10, int(base_fs * 0.62))
                pcx = tx + psz // 2
                pcy = py + psz // 2
                self._draw_particle(dr, pcx, pcy, psz, b["particle_name"], c1)
                dr.text((tx + psz + 6, py), b["particle_name"],
                        font=f_small, fill=body_rgb)
                off = psz + 6 + dr.textlength(b["particle_name"] + "　　", font=f_small)
            else:
                off = 0
            price = f"{b['price']} 分" if b.get("price") is not None else "?"
            dr.text((tx + off, py), price, font=f_small, fill=accent_rgb)
            if b.get("seller"):
                who = b["seller"]
                if b.get("seller_id"):
                    who = f"{who}（{b['seller_id']}）"
                dr.text((tx + off + dr.textlength(price + "　", font=f_small), py),
                        who, font=f_small, fill=sub_rgb)

        return c._png_bytes(img)


    #: 全市场花种挂单快照缓存：(时间戳, [brief...])
    _FLOWER_SNAP: tuple = (0, None)
    _FLOWER_SNAP_TTL = 30

    #: 常见粒子名（接口里的 particles.n）
    FLOWER_PARTICLES = ("四角星", "五角星", "六角星", "三角星", "方块", "爱心",
                        "星星", "菱形", "雪花", "闪光", "泡泡", "花瓣",
                        "圆点", "正方形", "三角形", "十字", "月牙", "闪电")

    #: 粒子的同义说法 —— 用户说「菱形」「方形」其实指的就是「方块」
    FLOWER_PARTICLE_ALIAS = {
        "四角星": ("四角星", "星星", "星型", "星形", "十字星", "四角",
                   "星芒", "四边形星", "星"),
        "方块": ("方块", "方形", "正方形", "矩形", "菱形", "四边", "四方",
                 "方格", "方型", "四边形", "四边形", "正方形块", "方"),
        "爱心": ("爱心", "心形", "桃心", "心型", "心心", "红心", "心"),
        "星星": ("星星", "星"),
        "菱形": ("菱形", "斜方", "方"),
        "雪花": ("雪花", "雪", "六角"),
        "闪光": ("闪光", "闪", "闪亮"),
        "泡泡": ("泡泡", "泡", "气泡"),
        "花瓣": ("花瓣", "花"),
        "圆点": ("圆点", "圆", "圆型", "圆形"),
    }

    async def _flower_snapshot(self, force: bool = False) -> List[Dict[str, Any]]:
        """把市场上所有花种挂单拉一遍（缓存 30 秒）。"""
        now = time.time()
        ts, cache = self._FLOWER_SNAP
        if not force and cache is not None and now - ts < self._FLOWER_SNAP_TTL:
            return cache
        catalog = await self._flower_catalog()
        out: List[Dict[str, Any]] = []
        for it in (catalog or []):
            pid = it.get("petId")
            if not pid:
                continue
            species = str(it.get("name") or "")
            for o in await self._flower_offers(pid):
                b = self._flower_brief(o)
                b["species"] = species or b.get("name") or ""
                out.append(b)
        self._FLOWER_SNAP = (now, out)
        return out

    def _flower_line(self, b: Dict[str, Any]) -> str:
        """一条花种的简短说明。外观（炫彩/异色）放精灵名前面。

        异色没有色卡 → 不写「颜色未知」。
        """
        look = str(b.get("appearance_txt") or "未标注")
        sp = str(b.get("species") or b.get("name") or "")
        bits = []
        _ctxt = self._flower_color_text(b)
        if _ctxt:
            bits.append(_ctxt)
        if b.get("particle_name"):
            bits.append(str(b["particle_name"]))
        price = b.get("price")
        tail = f"{price} 分" if price is not None else "?"
        seller = str(b.get("seller") or "").strip()
        body = " · ".join(bits)
        line = f"· {look} {sp}"
        if body:
            line += f" · {body}"
        line += f" · {tail}"
        if seller:
            line += f" · {seller}"
        return line

    #: 相似度阈值：字符覆盖率 >= 这个就算匹配
    FLOWER_SIM_MIN = 0.5

    @staticmethod
    def _flower_sim(key: str, target: str) -> float:
        """两个名字有多像（0~1）：按「用户输入的字有多少被目标覆盖」算。"""
        a = set(str(key or "").replace(" ", "").replace("-", "").replace("·", ""))
        b = set(str(target or "").replace(" ", "").replace("-", "").replace("·", ""))
        if not a or not b:
            return 0.0
        return len(a & b) / len(a)

    def _flower_particle_alias(self, key: str, real_parts) -> str:
        """把用户的说法映射到市场里真实存在的粒子名。

        例：市场里有「方块」，用户说「菱形/方形/正方形」都算方块。
        """
        k = str(key or "").strip()
        if not k:
            return ""
        # ① 完全相同的先返回
        if k in real_parts:
            return k
        # ② 查同义词表：找到包含 k 的那一组，再看组里哪个在市场里
        for canon, aliases in self.FLOWER_PARTICLE_ALIAS.items():
            if k in aliases or any(a == k for a in aliases):
                # 优先用规范名，其次组里任意一个
                if canon in real_parts:
                    return canon
                for a in aliases:
                    if a in real_parts:
                        return a
        # ③ 反向：k 是市场某个粒子名的同义词
        for real in real_parts:
            aliases = self.FLOWER_PARTICLE_ALIAS.get(real) or ()
            if k in aliases:
                return real
        return ""

    def _flower_pick_similar(self, key: str, candidates,
                             min_sim: float = -1.0) -> List[str]:
        """从候选名里挑出跟 key 像的（覆盖率 >= 阈值），按相似度降序。

        min_sim 不传就用 FLOWER_SIM_MIN（0.5，适合颜色）。
        粒子要用更高的阈值（0.85）—— 否则「五角星」会被匹配成「四角星」，
        那明明是两种不同的粒子。
        """
        th = self.FLOWER_SIM_MIN if min_sim < 0 else min_sim
        scored = []
        for c in candidates:
            s = self._flower_sim(key, c)
            if s >= th:
                scored.append((s, c))
        scored.sort(key=lambda x: -x[0])
        return [c for _s, c in scored]

    async def _flower_by_trait(self, event: Any, q: str):
        """按粒子 / 颜色 / 异色筛选花种。"""
        key = q.strip()
        low = key.lower()
        # 先分清是「接口坏了」还是「市场真空」
        _cat = await self._flower_catalog()
        if _cat is None:
            return await self._say(
                event, "花种数据暂时没拉到，过一会儿再试。")
        if not _cat:
            return await self._say(
                event, "花种市场当前没有挂单（0 个品种）。\n"
                       "有人上架就能查到；也可以用「花种订阅」等通知。")
        snap = await self._flower_snapshot()
        if not snap:
            return await self._say(event, "花种市场当前没有挂单。")

        hits: List[Dict[str, Any]] = []
        kind = ""
        # ① 异色 / 炫彩
        if low in ("异色", "shiny", "异色花种", "异色炫彩"):
            kind = "异色"
            hits = [b for b in snap if b.get("appearance") == "shiny"]
            if not hits:
                looks = sorted({str(b.get("appearance_txt") or "")
                                for b in snap} - {""})
                return await self._say(
                    event,
                    f"🌸 现在市场上没有异色的花种。\n"
                    f"（当前有的是：{' / '.join(looks) or '无'}）\n"
                    f"花种订阅 粒子/颜色/精灵 可以蹲，上架了自动推给你")
        elif low in ("炫彩", "colorful", "炫彩花种"):
            kind = "炫彩"
            hits = [b for b in snap if b.get("appearance") == "colorful"]
            if not hits:
                return await self._say(event, "🌸 现在市场上没有炫彩的花种。")
        else:
            # ② 粒子：先查同义词（菱形/方形 → 方块），再按**高阈值**相似度
            real_parts = sorted({str(b.get("particle_name") or "")
                                 for b in snap} - {""})
            use = self._flower_particle_alias(key, real_parts)
            if not use:
                # 只在市场里真有的粒子名之间比，阈值 0.85（五角星 ≠ 四角星）
                picked = self._flower_pick_similar(key, real_parts, min_sim=0.85)
                if picked:
                    use = picked[0]
            if not use and real_parts:
                # 看着像在问粒子（星/角/方/心/花/泡…），但市场没有 → 明确说
                if self._looks_particle(key):
                    return await self._say(
                        event,
                        f"🌸 市场里没有「{key}」这种粒子。\n"
                        f"（当前只有：{' / '.join(real_parts)}）\n"
                        f"想看全部花种发「花种」")
            if use:
                kind = (f"粒子「{use}」" if use == key
                        else f"粒子「{use}」（找的是「{key}」）")
                hits = [b for b in snap if use in str(b.get("particle_name") or "")]

            # ③ 颜色：按相似度（浅橙青 → 浅橙蓝 这种也算）
            if not hits:
                real_colors = sorted({str(b.get("color_name") or "")
                                      for b in snap} - {""})
                picked_c = self._flower_pick_similar(key, real_colors)
                if picked_c:
                    # 取最像的那几个（同分的都算）
                    top_s = self._flower_sim(key, picked_c[0])
                    near = [c for c in picked_c
                            if self._flower_sim(key, c) >= top_s - 0.001]
                    kind = (f"颜色「{picked_c[0]}」"
                            if len(near) == 1 else
                            f"颜色「{key}」相近的 {len(near)} 种")
                    hits = [b for b in snap
                            if str(b.get("color_name") or "") in near]

            # ④ 卖家名（包含即算，比如「花种 玩家ede0」）
            if not hits:
                sellers = {str(b.get("seller") or "") for b in snap} - {""}
                sim_sellers = [s for s in sellers if key in s or s in key]
                if sim_sellers:
                    kind = f"卖家「{sim_sellers[0]}」"
                    hits = [b for b in snap
                            if str(b.get("seller") or "") in sim_sellers]
                elif len(key) >= 2:
                    # 卖家名也支持相似度（打错一两个字也能中）
                    near_s = self._flower_pick_similar(key, list(sellers))
                    if near_s:
                        kind = f"卖家「{near_s[0]}」"
                        hits = [b for b in snap
                                if str(b.get("seller") or "") == near_s[0]]

        if not hits:
            # 最后兜底：给出市场里有哪些粒子 / 颜色可选
            parts = sorted({str(b.get("particle_name") or "") for b in snap} - {""})
            colors = sorted({str(b.get("color_name") or "") for b in snap} - {""})
            looks = sorted({str(b.get("appearance_txt") or "") for b in snap} - {""})
            lines = [f"「{q}」不是已知的精灵、粒子、颜色，也没有这个外观。",
                     "",
                     "当前市场里有的："]
            if parts:
                lines.append(f"· 粒子：{' / '.join(parts)}")
            if colors:
                lines.append(f"· 颜色：{' / '.join(colors[:12])}"
                             + (" …" if len(colors) > 12 else ""))
            if looks:
                lines.append(f"· 外观：{' / '.join(looks)}")
            lines.append("")
            lines.append("用法：花种 罗隐 / 花种 四角星 / 花种 浅橙青 / 花种 异色")
            return await self._say(event, "\n".join(lines))

        hits.sort(key=lambda b: (b.get("price") is None, b.get("price") or 0))
        lim = 20
        title = f"{kind}的花种 · {len(hits)} 个"
        lines = [f"🌸 {title}"]
        for b in hits[:lim]:
            lines.append(self._flower_line(b))
        if len(hits) > lim:
            lines.append(f"…… 还有 {len(hits) - lim} 个")
        lines.append("")
        lines.append("发「花种 精灵名」看那只精灵的色卡")
        text = "\n".join(lines)

        # 出色卡图（每行左边贴官方色卡）
        png = None
        if self._image_reply_on():
            try:
                png = await self._flower_multi_card(title, hits[:lim])
            except Exception as e:
                logger.warning(f"[出图] 花种列表卡失败: {type(e).__name__}: {e}")
        return await self._reply(event, text, png)

    @staticmethod
    def _chain_alias_set(rec: Dict[str, Any]) -> set:
        """把图鉴记录的进化链拆成名字集合（含去掉括号说明的版本）。

        花种市场挂的往往是**最高阶段**（比如「白金独角兽」），
        而图鉴主记录是**一阶段**（「小独角兽」），名字对不上。
        所以要用整条进化链去匹配。
        """
        names = set()
        species = str(rec.get("精灵") or "").strip()
        if species:
            names.add(species)

        def _add(raw: str):
            s = str(raw or "").strip()
            if not s:
                return
            names.add(s)
            base = re.sub(r"[（(].*?[)）]", "", s).strip()
            if base:
                names.add(base)

        for src_key in ("进化链", "_chain", "chain"):
            chain = str(rec.get(src_key) or "")
            if not chain:
                continue
            for part in re.split(r"\s*[→>→]\s*", chain):
                _add(part)
        for v in (rec.get("variants") or []):
            if isinstance(v, dict):
                _add(v.get("精灵"))
                _add(v.get("蛋名"))
                _add(str(v.get("蛋名") or "").replace("蛋", ""))
        _add(rec.get("蛋名"))
        return {x for x in names if x}

    async def _flower_by_species(self, event: Any, rec: Dict[str, Any],
                                 query: str):
        """按精灵查：支持进化链任意阶段（小独角兽 ←→ 白金独角兽）。"""
        species = str(rec.get("精灵") or query)
        aliases = self._chain_alias_set(rec)
        pet_ids: List[int] = []
        for v in (rec.get("variants") or [rec]):
            pid = v.get("精灵ID") or v.get("petConfId") or v.get("基础ID")
            if pid:
                pet_ids.append(int(pid))
        catalog = await self._flower_catalog()
        if catalog is None:
            return await self._say(event,
                                   "花种数据暂时没拉到，过一会儿再试。")
        if not catalog:
            return await self._say(
                event, "花种市场当前没有挂单（0 个品种）。\n有人上架就能查到。")

        # 用整条进化链的名字去对 catalog
        if not pet_ids:
            for it in catalog:
                pid = it.get("petId")
                if not pid:
                    continue
                nm = str(it.get("name") or "").strip()
                base = re.sub(r"[（(].*?[)）]", "", nm).strip()
                if nm in aliases or base in aliases:
                    pet_ids.append(int(pid))
        # 还没中就退回包含匹配
        if not pet_ids:
            for it in catalog:
                pid = it.get("petId")
                if not pid:
                    continue
                nm = str(it.get("name") or "")
                base = re.sub(r"[（(].*?[)）]", "", nm).strip()
                if any(a and (a in nm or a in base or nm in a or base in a)
                       for a in aliases):
                    pet_ids.append(int(pid))
        pet_ids = list(dict.fromkeys(pet_ids))
        if not pet_ids:
            if not catalog:
                return await self._say(
                    event,
                    f"🌸 {species}\n"
                    "花种数据暂时没拉到，过一会儿再试。\n")
            chain = str(rec.get("进化链") or "")
            extra = f"\n（它整条进化链：{chain}）" if chain else ""
            return await self._say(
                event, f"🌸 {species}\n市场上现在没有它的花种。{extra}\n"
                       f"（当前花种市场只有：{'、'.join(str(x.get('name')) for x in catalog[:10])}）")
        offers: List[Dict[str, Any]] = []
        for pid in pet_ids:
            offers.extend(await self._flower_offers(pid))
        # 标题优先用**市场里挂的名字**（蝎子王），别用图鉴一阶段名（碎晶蝎）
        _disp = ""
        for _o in offers:
            # 原始 offer 里名字在 flower.name
            _s = str((_o.get("flower") or {}).get("name") or "")
            if _s:
                _disp = _s
                break
        species = _disp or species
        text = self._render_flower_offers(species, offers)
        png = None
        if offers and self._image_reply_on():
            try:
                png = await self._flower_card(species, offers)
            except Exception as e:
                logger.warning(f"[出图] 花种卡异常: {type(e).__name__}: {e}")
        return await self._reply(event, text, png)

    async def _flower_all(self, event: Any):
        """「花种」不带参数：列出当前花种市场的所有品种。"""
        catalog = await self._flower_catalog()
        if catalog is None:
            return await self._say(
                event, "花种数据暂时没拉到，过一会儿再试。")
        if not catalog:
            return await self._say(
                event, "花种市场当前没有挂单（0 个品种）。\n"
                       "有人上架就能查到；也可以用「花种订阅」等通知。")
        items = [x for x in catalog if x.get("petId")]
        if not items:
            return await self._say(event, "花种市场现在没有任何品种。")
        items.sort(key=lambda x: (-(int(x.get("offers") or 0)),
                                  int(x.get("minPrice") or 999999)))
        lim = 25
        lines = [f"花种市场 · {len(items)} 个品种 · 共 "
                 f"{sum(int(x.get('offers') or 0) for x in items)} 个挂单"]
        for i, it in enumerate(items[:lim], 1):
            nm = str(it.get("name") or "?")
            offers = int(it.get("offers") or 0)
            low = it.get("minPrice")
            lines.append(f"{i}. {nm} · {offers} 个挂单"
                         + (f" · 最低 {low} 分" if low is not None else ""))
        if len(items) > lim:
            lines.append(f"…… 还有 {len(items) - lim} 个品种")
        lines.append("")
        lines.append("花种 精灵名     看那只精灵的色卡")
        lines.append("花种 四角星     看有这种粒子的花种")
        lines.append("花种 浅橙青     看这种颜色的花种")
        lines.append("花种 异色       看异色花种")
        lines.append("订阅：花种订阅 颜色/粒子/精灵")
        return await self._say(event, "\n".join(lines))

    #: 同一条消息只让一个花种入口响应（否则 command + regex 会各回一张图）
    @staticmethod
    def _flower_claim(event: Any) -> bool:
        if getattr(event, "_lk_flower_done", False):
            return False
        try:
            event._lk_flower_done = True
        except Exception:
            pass
        return True

    async def _flower_multi_card(self, title: str,
                                 briefs: List[Dict[str, Any]]) -> Optional[bytes]:
        """多条花种的色卡图：每行左边贴官方色卡图，右边写精灵/颜色/粒子/价格。"""
        try:
            from PIL import Image, ImageDraw, ImageFont
        except Exception:
            return None
        c = _card()
        if c is None or not c.available():
            return None

        kw = self._card_kwargs()
        base_w = int(kw.get("width") or 700)
        base_fs = int(kw.get("font_size") or 18)
        W = max(560, min(1100, base_w))

        rows = briefs
        ROW_H = int(base_fs * 4.4)
        HEAD_H = int(base_fs * 3.4)
        PAD = int(base_fs * 0.9)
        CARD_W = int(ROW_H * 1.7)          # 色卡图宽
        CARD_H = int(CARD_W / 280 * 154)   # 官方图 280×154 比例

        H = HEAD_H + ROW_H * len(rows) + PAD
        try:
            theme = c._norm_theme(kw.get("theme") or "")
            bg = c._to_rgb(theme.get("背景", "#FCFAF6"))
            card_bg = c._to_rgb(theme.get("卡片", "#FFFFFF"))
            title_rgb = c._to_rgb(theme.get("标题", "#2C2A26"))
            body_rgb = c._to_rgb(theme.get("正文", "#4A4740"))
            sub_rgb = c._to_rgb(theme.get("次要", "#9A958C"))
            accent_rgb = c._to_rgb(theme.get("强调", "#C25828"))
        except Exception:
            bg, card_bg = (252, 250, 246), (255, 255, 255)
            title_rgb, body_rgb = (44, 42, 38), (74, 71, 64)
            sub_rgb, accent_rgb = (154, 149, 140), (194, 88, 40)

        img = Image.new("RGB", (W, H), bg)
        dr = ImageDraw.Draw(img)

        try:
            fpath = c.find_font(str(kw.get("font_path") or ""))
            f_title = ImageFont.truetype(fpath, int(base_fs * 1.3)) if fpath else None
            f_name = ImageFont.truetype(fpath, base_fs) if fpath else None
            f_small = ImageFont.truetype(fpath, max(10, int(base_fs * 0.76))) if fpath else None
        except Exception:
            f_title = f_name = f_small = None
        f_title = f_title or ImageFont.load_default()
        f_name = f_name or f_title
        f_small = f_small or f_name

        dr.text((PAD, int(HEAD_H * 0.32)), _strip_emoji(title),
                font=f_title, fill=title_rgb)
        dr.line([(PAD, HEAD_H - 6), (W - PAD, HEAD_H - 6)],
                fill=(232, 228, 220), width=1)

        import io as _io
        for i, b in enumerate(rows):
            y = HEAD_H + i * ROW_H
            if i % 2 == 0:
                dr.rectangle([PAD - 6, y + 2, W - PAD + 6, y + ROW_H - 4],
                             fill=card_bg)

            # 左：异色 → 异色图标；炫彩 → 官方色卡图
            cx, cy = PAD, y + (ROW_H - CARD_H) // 2
            placed = False
            thumb = self._flower_thumb_path(b)
            if thumb:
                try:
                    tim = Image.open(thumb).convert("RGBA")
                    side = min(CARD_H, CARD_W)
                    tim = tim.resize((side, side), Image.LANCZOS)
                    img.paste(tim, (cx + (CARD_W - side) // 2,
                                    cy + (CARD_H - side) // 2), tim)
                    placed = True
                except Exception:
                    placed = False
            img_bytes = None
            if not placed:
                img_bytes = await self._chroma_card_bytes(b.get("particle_id"),
                                                          b.get("color_id"))
            if img_bytes:
                try:
                    oim = Image.open(_io.BytesIO(img_bytes)).convert("RGBA")
                    oim = oim.resize((CARD_W, CARD_H), Image.LANCZOS)
                    img.paste(oim, (cx, cy), oim)
                    placed = True
                except Exception:
                    pass
            if not placed:
                # 兜底：两个色块
                c1 = self._hex_to_rgb(b.get("c1"), (222, 222, 222))
                c2 = self._hex_to_rgb(b.get("c2"), (245, 245, 245))
                hw = CARD_W // 2
                dr.rectangle([cx, cy, cx + hw - 2, cy + CARD_H], fill=c1)
                dr.rectangle([cx + hw + 2, cy, cx + CARD_W, cy + CARD_H], fill=c2)

            tx = cx + CARD_W + int(base_fs * 0.8)

            # 第一行：精灵名 + 外观
            sp = str(b.get("species") or b.get("name") or "?")
            look = b.get("appearance_txt") or "未标注"
            dr.text((tx, y + int(ROW_H * 0.10)), sp, font=f_name, fill=body_rgb)
            tw = dr.textlength(look, font=f_small)
            dr.text((W - PAD - tw, y + int(ROW_H * 0.15)), look,
                    font=f_small, fill=accent_rgb)

            # 第二行：颜色（异色没有色卡 → 不写「颜色未知」）
            _c3 = self._flower_color_text(b)
            if _c3:
                dr.text((tx, y + int(ROW_H * 0.40)), _strip_emoji(_c3),
                        font=f_small, fill=sub_rgb)

            # 第三行：粒子 + 价格 + 卖家
            py = y + int(ROW_H * 0.66)
            pname = str(b.get("particle_name") or "")
            off = 0
            if pname:
                psz = max(10, int(base_fs * 0.6))
                self._draw_particle(dr, tx + psz // 2, py + psz // 2,
                                    psz, pname, self._hex_to_rgb(b.get("c1")))
                dr.text((tx + psz + 6, py), pname, font=f_small, fill=body_rgb)
                off = psz + 6 + dr.textlength(pname + "　　", font=f_small)
            price = f"{b.get('price')} 分" if b.get("price") is not None else "?"
            dr.text((tx + off, py), price, font=f_small, fill=accent_rgb)
            seller = str(b.get("seller") or "")
            if seller:
                dr.text((tx + off + dr.textlength(price + "　", font=f_small), py),
                        seller, font=f_small, fill=sub_rgb)

        return c._png_bytes(img)

    async def _flower_reply(self, event: Any, query: str):
        """「花种 xxx」智能识别：

        xxx 可以是
          · 精灵名（任意进化阶段）→ 看那只精灵的花种色卡
          · 粒子名（四角星/方块/爱心…）→ 看有这种粒子的花种
          · 颜色名（浅橙青/青紫…）→ 看这种颜色的花种
          · 异色 / 炫彩 → 看该外观的花种
        """
        if not await self._perm_gate(event, "flower"):
            return
        q = str(query or "").strip()
        if not q:
            return await self._flower_all(event)

        low = q.lower()

        # ① 异色 / 炫彩（先判，这俩是明确的枚举）
        if low in ("异色", "shiny", "异色花种", "异色炫彩",
                   "炫彩", "colorful", "炫彩花种"):
            return await self._flower_by_trait(event, q)

        # ② 图鉴**精确**匹配优先（月牙雪熊 / 蝎子王 这种不能再被当成粒子）
        _exact = self._find_species(q, strict=True)
        if _exact:
            return await self._flower_by_species(event, _exact, q)

        # ③ 粒子 / 颜色：跟市场里真实存在的名字比
        #    必须放在**模糊**精灵名前面 —— 否则「星星」会被匹配成「星星眼」，
        #    「方」会被匹配成「机械方方」。
        snap = await self._flower_snapshot()
        if snap:
            parts = sorted({str(b.get("particle_name") or "") for b in snap} - {""})
            if parts:
                # 同义词（菱形/方形/星星…）直接认
                if self._flower_particle_alias(q, parts):
                    return await self._flower_by_trait(event, q)
                # 明确在问粒子（四角星/五角星/方块…）→ 交给 trait
                # 注意用完整词，不能用单字「角」——否则「独角兽」会被当成粒子
                if self._looks_particle(q):
                    return await self._flower_by_trait(event, q)
                best = max(parts, key=lambda x: self._flower_sim(q, x))
                if self._flower_sim(q, best) >= 0.85:
                    return await self._flower_by_trait(event, q)
            colors = sorted({str(b.get("color_name") or "") for b in snap} - {""})
            if colors:
                best = max(colors, key=lambda x: self._flower_sim(q, x))
                if self._flower_sim(q, best) >= 0.5:
                    return await self._flower_by_trait(event, q)

        # ④ 图鉴模糊匹配（进化链任意阶段）
        rec = self._flower_match_pet(q)
        if rec:
            return await self._flower_by_species(event, rec, q)

        # ④.5 图鉴里没有、但市场里真有这个品种（比如「黑猫巫师」这种只在花种市场出现的）
        #      不补这一步就会出现「推了却查不到」：推送读的是 catalog，查询读的是图鉴。
        try:
            cat = await self._flower_catalog() or []
        except Exception:
            cat = []
        names = [str(x.get("name") or "").strip() for x in cat if x.get("name")]
        cand = ""
        if q in names:
            cand = q
        elif names:
            sub = [n for n in names if q in n or n in q]
            if sub:
                cand = max(sub, key=lambda n: self._flower_sim(q, n))
            else:
                best = max(names, key=lambda n: self._flower_sim(q, n))
                if self._flower_sim(q, best) >= 0.6:
                    cand = best
        if cand:
            logger.info(f"[花种] {q!r} 图鉴里没有，按市场品种 {cand!r} 查")
            return await self._flower_by_species(
                event, {"精灵": cand, "蛋名": f"{cand}的蛋", "进化链": cand,
                        "variants": []}, q)

        # ⑤ 都没命中 → 让筛选逻辑给「市场里有什么」的提示
        return await self._flower_by_trait(event, q)

    # ---------------------------------------------------------------- 指令：异色（按赛季 + 蛋组）
    #: 赛季表：{"season": {精灵: "S5"}, "detail": {...}}（build_seasons.py 生成）
    _SEASONS: Optional[Dict[str, Any]] = None

    @classmethod
    def _season_table(cls) -> Dict[str, Any]:
        if cls._SEASONS is not None:
            return cls._SEASONS
        try:
            fp = _PLUGIN_DIR / "seasons.json"
            cls._SEASONS = json.loads(fp.read_text(encoding="utf-8-sig"))
        except Exception as e:
            logger.warning(f"[赛季] seasons.json 加载失败: {type(e).__name__}: {e}")
            cls._SEASONS = {"season": {}, "detail": {}}
        return cls._SEASONS

    def _season_of(self, name: str) -> str:
        t = self._season_table()
        return str((t.get("season") or {}).get(name) or "")

    #: 赛季花名册：{"S4": {名字: {pet_id, has_shiny, groups, no}}}（build_season_roster.py 生成）
    _ROSTER: Optional[Dict[str, Any]] = None

    @classmethod
    def _roster_table(cls) -> Dict[str, Any]:
        if cls._ROSTER is not None:
            return cls._ROSTER
        try:
            fp = _PLUGIN_DIR / "season_roster.json"
            cls._ROSTER = json.loads(fp.read_text(encoding="utf-8-sig"))
        except Exception as e:
            logger.warning(f"[赛季] season_roster.json 加载失败: {type(e).__name__}: {e}")
            cls._ROSTER = {}
        return cls._ROSTER

    #: 官方异色记录本：{"seasons":[{id,name,pets,eggs}], "pets":[{name,season,stage,types,number}]}
    _SHINY_BOOK: Optional[Dict[str, Any]] = None

    @classmethod
    def _shiny_book(cls) -> Dict[str, Any]:
        if cls._SHINY_BOOK is not None:
            return cls._SHINY_BOOK
        out: Dict[str, Any] = {"seasons": [], "pets": []}
        try:
            fp = _PLUGIN_DIR / "shiny_book.json"
            if fp.is_file():
                d = json.loads(fp.read_text(encoding="utf-8-sig"))
                out["seasons"] = d.get("seasons") or []
                out["pets"] = d.get("pets") or []
        except Exception as e:
            logger.warning(f"[异色记录本] shiny_book.json 读取失败: {type(e).__name__}: {e}")
        cls._SHINY_BOOK = out
        return out

    def _book_seasons(self) -> List[Dict[str, Any]]:
        """[{"id":"none","name":"常驻","pets":19}, {"id":"S1","name":"暗夜拾光",…}]"""
        return [s for s in (self._shiny_book().get("seasons") or []) if isinstance(s, dict)]

    def _book_season_name(self, sid: str) -> str:
        """S5 → 圣龙宝藏；none → 常驻"""
        for s in self._book_seasons():
            if str(s.get("id")) == str(sid):
                return str(s.get("name") or "")
        return ""

    _BOOK_BY_NAME: Optional[Dict[str, Dict[str, Any]]] = None

    def _book_by_name(self) -> Dict[str, Dict[str, Any]]:
        """异色记录本：名字 → 条目（查常驻/奇遇用）。"""
        cls = type(self)
        if cls._BOOK_BY_NAME is not None:
            return cls._BOOK_BY_NAME
        out: Dict[str, Dict[str, Any]] = {}
        for r in (self._shiny_book().get("pets") or []):
            nm = str((r or {}).get("name") or "")
            if nm and nm not in out:
                out[nm] = r
        cls._BOOK_BY_NAME = out
        return out

    #: 特殊精灵果实（活动 / 特殊途径的果实，不算在赛季常驻里）。配置 grid_special_pets 可覆盖
    SPECIAL_PETS: tuple = ("云梦豚", "长江豚", "火红尾", "雅丹鬃")

    def _special_pets(self) -> set:
        raw = str(self._cfg("grid_special_pets", "") or "")
        names = [x.strip() for x in re.split(r"[,，、/\s]+", raw) if x.strip()]
        return set(names) if names else set(self.SPECIAL_PETS)

    def _book_rows(self, season: str = "") -> List[Dict[str, Any]]:
        """名单行：**以官方全量表的 season_id + has_shiny 为准**，记录本只提供赛季名。

        之前直接信记录本的 "season"，结果把官方标成 S5 的精灵（锤头鹳 / 小翼龙 / 伊雷龙 …）
        当成「常驻」，S1~S5 也出现过季节不符。现在：
          · 常驻 = 官方 season_id 0 且有异色
          · S1~S5 = 官方 season_id 对应且 has_shiny
          · 特殊精灵果实（云梦豚 / 火红尾 …）不算常驻
          · 记录本里有、官方表里没有的名字，才回退用记录本的赛季
        """
        pets = self._pets_table()
        book = self._book_by_name()
        sp = self.ranges.get("species") or {}
        cmap = self._chain_map()
        special = self._special_pets()
        want = str(season or "").strip().upper()
        if want in ("S0", "NONE", "常驻"):
            want = "S0"

        out: List[Dict[str, Any]] = []
        seen = set()

        def sid_of(info: Dict[str, Any]) -> str:
            try:
                n = int(info.get("season") or 0)
            except (TypeError, ValueError):
                n = 0
            return f"S{n}" if n >= 1 else "S0"

        for nm, info in pets.items():
            seen.add(nm)                     # 官方表里出现过的名字，一律不再走记录本兜底
            if not info.get("has_shiny"):
                continue
            if nm in special or (cmap.get(nm) in special):
                continue
            sid = sid_of(info)
            if want and sid != want:
                continue
            out.append(self._row_of(nm, sid, info))

        # 官方表里没有的名字（老数据才有的），回退用记录本的赛季
        for nm, rec in book.items():
            if nm in seen or nm in special:
                continue
            sid = str(rec.get("season") or "none")
            sid = "S0" if sid in ("none", "S0") else sid
            if want and sid != want:
                continue
            out.append(self._row_of(nm, sid, {}))
        return out

    def _row_of(self, nm: str, sid: str, info: Dict[str, Any]) -> Dict[str, Any]:
        """造一条名单行（蛋组 / 同乘 / 蛋名 都按名字补）。"""
        sp = self.ranges.get("species") or {}
        rec = sp.get(nm) or {}
        groups = [g for g in (info.get("groups") or []) if g]
        ride = info.get("ride")
        if ride is None and rec.get("同乘") is not None:
            ride = bool(rec.get("同乘"))
        try:
            stage = int(info.get("stage") or rec.get("形态数") or 0)
        except (TypeError, ValueError):
            stage = 0
        return {
            "season": sid,
            "name": nm,
            "group": " / ".join(groups) or "未分组",
            "groups": groups,
            "marks": ["异色", "异色蛋"],
            "leader": stage >= 4 or bool((self._book_by_name().get(nm) or {}).get("leader")),
            "ride": ride,
            "no": str(info.get("no") or (self._book_by_name().get(nm) or {}).get("number") or ""),
            "stage": stage,
            "egg": str(rec.get("蛋名") or f"{nm}的蛋"),
        }

    def _shiny_report(self, season: str = "") -> str:
        """异色名单的文字版（出图失败时兜底），和网格图同一份数据（官方异色记录本）。"""
        if season and season != "S0":
            rows_res, rows_enc = [], self._book_rows(season)
        elif season == "S0":
            rows_res, rows_enc = self._book_rows("S0"), []
        else:
            rows_res = self._book_rows("S0")
            rows_enc = [r for r in self._book_rows("") if r.get("season") != "S0"]
        if not rows_res and not rows_enc:
            return (f"{season} 没有登记到异色精灵" if season
                    else "没有登记到异色精灵（shiny_book.json 里没数据）")

        label = self._season_label(season)
        lines = [f"✨ 异色精灵 · {label} · 常驻 {len(rows_res)} + 奇遇 {len(rows_enc)}"]

        def dump(title: str, rows: List[Dict[str, Any]]) -> None:
            if not rows:
                return
            grouped: Dict[str, List[str]] = {}
            for r in rows:
                disp = str(r.get("name") or "")
                if r.get("leader"):
                    disp += "（首领化）"
                grouped.setdefault(str(r.get("group") or "未分组"), []).append(disp)
            lines.append("")
            lines.append(f"—————— {title} · {len(rows)} 只 ——————")
            for g in sorted(grouped, key=lambda x: (-len(grouped[x]), x)):
                items = grouped[g]
                lines.append(f"【{g}】{len(items)} 只")
                buf = "、".join(items)
                if len(buf) <= 110:
                    lines.append("  " + buf)
                else:
                    half = (len(items) + 1) // 2
                    lines.append("  " + "、".join(items[:half]))
                    lines.append("  " + "、".join(items[half:]))

        dump("常驻（S0）", rows_res)
        dump(f"奇遇（{season or 'S1~S5'}）", rows_enc)
        lines.append("")
        lines.append("名单：官方异色记录本 · 图标：wiki")
        lines.append("用法：异色 / 异色 s5 / s3 异色 / 异色 s5 2（翻页） / 异色 s5 文字")
        return "\n".join(lines)

    # ---------------------------------------------------------------- 网格图
    #: 赛季名（网格图大字旁边的副标题）。配置 grid_season_titles 可以逐行补
    SEASON_TITLE = {"S0": "常驻", "S5": "圣龙宝藏"}

    def _season_titles(self) -> Dict[str, str]:
        out = dict(self.SEASON_TITLE)
        for line in str(self._cfg("grid_season_titles", "") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            for sep in ("=", "＝", ":", "："):
                if sep in line:
                    k, _, v = line.partition(sep)
                    k, v = k.strip().upper(), v.strip()
                    if k and v:
                        out[k] = v
                    break
        return out

    def _season_label(self, season: str) -> str:
        """S5 → 'S5 · 圣龙宝藏'（赛季名优先取官方异色记录本）。"""
        s = str(season or "").strip().upper()
        if not s:
            return "全部赛季"
        if s == "S0":
            return "常驻（S0）"
        t = self._book_season_name(s) or self._season_titles().get(s) or ""
        return f"{s} · {t}" if t else s

    def _int_cfg(self, key: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int(float(self._cfg(key, default)))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    def _grid_enabled(self) -> bool:
        v = self._cfg("grid_enabled", True)
        if isinstance(v, str):
            return v.strip().lower() not in ("", "0", "false", "no", "off")
        return bool(v)

    def _grid_engine(self) -> str:
        """auto（先 HTML 再 Pillow）| html | pillow"""
        s = str(self._cfg("grid_engine", "auto") or "auto").strip().lower()
        return s if s in ("auto", "html", "pillow") else "auto"

    def _grid_theme_text(self) -> str:
        return "\n".join(x for x in (
            str(self._cfg("card_theme", "") or ""),
            str(self._cfg("grid_theme", "") or "")) if x)

    def _grid_sort_key(self, r: Dict[str, Any]):
        """同赛季内按蛋组聚在一起，组内按图鉴号 —— 图上颜色成片，好看也好找。"""
        try:
            n = int(re.sub(r"\D", "", str(r.get("no") or "")) or 0)
        except (TypeError, ValueError):
            n = 0
        return (str(r.get("season") or ""), str(r.get("group") or ""), n,
                str(r.get("name") or ""))

    #: 名字 / 形态名 → 进化链首段（一阶段名）。来自图鉴的「进化链」字段
    _CHAIN_MAP: Optional[Dict[str, str]] = None

    @classmethod
    def _chain_map_clear(cls) -> None:
        cls._CHAIN_MAP = None

    def _chain_map(self) -> Dict[str, str]:
        """名字 / 形态名 → 进化链首段（=`一阶段` 那只的名字）。

        图鉴里 `进化链` 的写法是 `地银链 → 地底银鳞 → 地绡银龙`，
        二阶段 / 三阶段（地底银鳞、地绡银龙）本身不单独收进图鉴，只有链上出现，
        所以整条链都要能从这串字里认出来。括号里的形态名去掉括号再登记一次。
        """
        cls = type(self)
        if cls._CHAIN_MAP is not None:
            return cls._CHAIN_MAP
        out: Dict[str, str] = {}
        for nm, rec in (self.ranges.get("species") or {}).items():
            chain = str(rec.get("进化链") or "").strip()
            if not chain:
                continue
            parts = [x.strip() for x in re.split(r"[→>]|/|、", chain) if x.strip()]
            if not parts:
                continue
            head = re.sub(r"[（(].*?[)）]", "", parts[0]).strip() or nm
            base = re.sub(r"[（(].*?[)）]", "", nm).strip() or nm
            out.setdefault(base, head)
            out.setdefault(nm, head)
            for tk in parts:
                t = re.sub(r"[（(].*?[)）]", "", tk).strip()
                if t:
                    out.setdefault(t, head)
        # chains.json：wiki 精灵页「进化」块里抓的链（覆盖 ranges.json 没有的新精灵，
        # 比如 缇塔 → 声波缇塔、地银链 → 地底银鳞 → 地绡银龙）
        try:
            fp = _PLUGIN_DIR / "chains.json"
            if fp.is_file():
                d = json.loads(fp.read_text(encoding="utf-8-sig"))
                for nm, head in (d.get("members") or {}).items():
                    if nm and head:
                        # wiki 的进化块最全，直接覆盖（ranges 里有「长江豚→长江豚」这种自环）
                        out[str(nm)] = str(head)
        except Exception as e:
            logger.warning(f"[进化链] chains.json 读取失败: {type(e).__name__}: {e}")
        cls._CHAIN_MAP = out
        return out

    def _shiny_grid_groups(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """把「同一只精灵的多个形态 / 多个进化阶段」并成一条。

        花名册是一行一个名字，所以同一只精灵会被拆成好几行：
          · 同一个图鉴号的形态   伊兰亚龙 / 伊兰龙 都是 204；月亮砣 / 满月砣 都是 238
          · 一条进化链的各个阶段 地银链 → 地底银鳞 → 地绡银龙（471/472/473）
        这两种都算同一只，并成一张卡：**只显示第一阶段**那一只。

        代表名取「一阶段」：进化链首段 > stage 最小 > 图鉴里有记录 > 花名册原顺序。
        """
        items = [r for r in (rows or []) if str(r.get("name") or "").strip()]
        if not items:
            return []
        cmap = self._chain_map()
        sp = self.ranges.get("species") or {}
        # 用「行下标」做并查集：不同赛季可以同名不同只（缇塔 S0 是 464 号、S5 是 229 号），
        # 按名字做键会把它们错并成一只
        parent: Dict[int, int] = {i: i for i in range(len(items))}

        def find(x: int) -> int:
            root = x
            while parent.get(root, root) != root:
                root = parent[root]
            while parent.get(x, x) != x:      # 路径压缩
                nxt = parent[x]
                parent[x] = root
                x = nxt
            return root

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        # ① 同一赛季 + 同一个图鉴号 = 同一只的形态
        by_dex: Dict[tuple, List[int]] = {}
        for i, r in enumerate(items):
            no = str(r.get("no") or "").strip()
            if no:
                by_dex.setdefault((str(r.get("season") or ""), no), []).append(i)
        for idxs in by_dex.values():
            for i in idxs[1:]:
                union(idxs[0], i)
        # ② 同一赛季 + 同一条进化链 = 同一只的不同阶段
        by_chain: Dict[tuple, List[int]] = {}
        for i, r in enumerate(items):
            head = cmap.get(str(r.get("name")))
            if head:
                by_chain.setdefault((str(r.get("season") or ""), head), []).append(i)
        for idxs in by_chain.values():
            for i in idxs[1:]:
                union(idxs[0], i)

        order = {i: i for i in range(len(items))}
        buckets: Dict[int, List[Dict[str, Any]]] = {}
        for i, r in enumerate(items):
            buckets.setdefault(find(i), []).append(r)

        out: List[Dict[str, Any]] = []
        for members in buckets.values():
            def rank(r: Dict[str, Any]):
                nm = str(r.get("name") or "")
                head = cmap.get(nm)
                base = re.sub(r"[（(].*?[)）]", "", nm).strip() or nm
                return (0 if (head and head == base) else 1,      # 链首（一阶段）
                        int(r.get("stage") or 9),                 # 阶段越小越靠前
                        0 if nm in sp else 1,                     # 图鉴里有记录优先
                        order.get(nm, 0))                         # 花名册原顺序
            rep = sorted(members, key=rank)[0]
            nm = str(rep.get("name") or "")
            marks: List[str] = []
            for r in members:
                for k in (r.get("marks") or []):
                    if k not in marks:
                        marks.append(k)
            rec = sp.get(nm) or {}
            # 蛋名 / 同乘 只认一阶段那只（二三阶段不下蛋）
            egg = str(rec.get("蛋名") or "")
            ride = rec.get("同乘")
            if not egg or ride is None:
                for r in members:
                    rc = sp.get(str(r.get("name") or "")) or {}
                    if not egg and rc.get("蛋名"):
                        egg = str(rc["蛋名"])
                    if ride is None and rc.get("同乘") is not None:
                        ride = rc.get("同乘")
            if not egg:
                egg = f"{nm}的蛋"
            groups: List[str] = []
            for r in members:
                for x in (r.get("groups") or []):
                    if x and x not in groups:
                        groups.append(x)
            no = str(rep.get("no") or "")
            if not no:
                for r in members:
                    if str(r.get("no") or ""):
                        no = str(r["no"])
                        break
            out.append({
                "name": nm,
                "no": no,
                "groups": groups,
                "group": str(rep.get("group") or ""),
                "marks": marks,
                "leader": any(bool(r.get("leader")) for r in members),
                "ride": ride,
                "egg": egg,
                "members": [str(r.get("name") or "") for r in members],
                "season": str(rep.get("season") or ""),
            })
        return out

    def _grid_avatar_mode(self) -> str:
        """头像用哪个图：stage1（一阶段，默认）| shiny（异色立绘）"""
        s = str(self._cfg("grid_avatar", "stage1") or "stage1").strip().lower()
        return s if s in ("stage1", "shiny") else "stage1"

    def _grid_ride_mode(self) -> str:
        """同乘怎么标：yes（只标可同乘，默认）| both（可/不可都标）| off（不标）"""
        s = str(self._cfg("grid_show_ride", "yes") or "yes").strip().lower()
        return s if s in ("both", "yes", "off") else "yes"

    #: 系别表：{"colors": {系: {tone, accent}}, "pets": {名字: [系…]}}（tools/pull_wiki_elements.py 生成）
    _ELEMENTS: Optional[Dict[str, Any]] = None

    @classmethod
    def _elements_table(cls) -> Dict[str, Any]:
        if cls._ELEMENTS is not None:
            return cls._ELEMENTS
        out: Dict[str, Any] = {"colors": {}, "pets": {}, "chain": {}}
        try:
            fp = _PLUGIN_DIR / "elements.json"
            if fp.is_file():
                d = json.loads(fp.read_text(encoding="utf-8-sig"))
                for k in ("colors", "pets", "chain"):
                    out[k] = d.get(k) or {}
        except Exception as e:
            logger.warning(f"[系别] elements.json 读取失败: {type(e).__name__}: {e}")
        cls._ELEMENTS = out
        return out

    def _elem_types(self, name: str) -> List[str]:
        """精灵名 → 系别列表（没有就退到一阶段那只 / 进化链兜底）。"""
        t = self._elements_table()
        pets = t.get("pets") or {}
        nm = str(name or "").strip()
        if not nm:
            return []
        got = pets.get(nm)
        if got:
            return list(got)
        head = self._chain_map().get(nm)
        for alt in (head, (self._elements_table().get("chain") or {}).get(nm)):
            if not alt:
                continue
            if isinstance(alt, str) and pets.get(alt):
                return list(pets[alt])
            if isinstance(alt, list) and alt:
                return list(alt)
        return []

    @staticmethod
    def _elem_label(types: List[str]) -> str:
        """['龙系','翼系'] → '龙/翼'；['火系'] → '火系'。"""
        names = [str(x).replace("系", "").strip() for x in (types or [])[:2] if x]
        names = [x for x in names if x]
        if not names:
            return ""
        return names[0] + "系" if len(names) == 1 else "/".join(names)

    @staticmethod
    def _elem_key(cols: Dict[str, Any], t: str) -> Dict[str, Any]:
        """系别配色表的键两种写法都认：官方接口给「龙」，wiki CSS 给「龙系」。"""
        s = str(t or "").strip()
        if not s:
            return {}
        return (cols.get(s) or cols.get(s + "系")
                or cols.get(s[:-1] if s.endswith("系") else s) or {})

    def _elem_color(self, types: List[str]) -> str:
        """系别主色（wiki 官方 CSS 里的 --sd-class-accent）。"""
        cols = self._elements_table().get("colors") or {}
        for t in (types or []):
            c = self._elem_key(cols, t)
            if c.get("accent"):
                return str(c["accent"])
        return ""

    def _elem_tone(self, types: List[str]) -> str:
        cols = self._elements_table().get("colors") or {}
        for t in (types or []):
            c = self._elem_key(cols, t)
            if c.get("tone"):
                return str(c["tone"])
        return ""

    def _grid_elem_tint(self) -> float:
        """系别色占卡片底色的比例：1=满色，0=纯白。默认 0.80（淡淡一层系别色）。"""
        try:
            v = float(self._cfg("grid_elem_tint", 0.62))
        except (TypeError, ValueError):
            v = 0.62
        return max(0.0, min(1.0, v))

    @staticmethod
    def _mix_hex(hex_color: str, ratio: float, base: str = "#FFFFFF") -> str:
        """把 hex_color 按 ratio 混进 base（ratio 越大越接近 hex_color）。"""
        def rgb(s: str):
            s = str(s or "").strip().lstrip("#")
            if len(s) != 6:
                return None
            try:
                return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                return None
        a, b = rgb(hex_color), rgb(base)
        if not a or not b:
            return ""
        t = max(0.0, min(1.0, float(ratio)))
        return "#%02X%02X%02X" % tuple(int(round(a[i] * t + b[i] * (1 - t))) for i in range(3))

    def _bins_of(self, name: str) -> Optional[int]:
        """这只精灵（一阶段那颗蛋）的区间数。取不到返回 None。"""
        rec = (self.ranges.get("species") or {}).get(str(name or "")) or {}
        for v in (rec.get("variants") or [rec]):
            if "异色" in str(v.get("蛋名") or ""):
                continue
            n = v.get("区间数")
            if n in (None, ""):
                continue
            try:
                return int(n)
            except (TypeError, ValueError):
                continue
        n = rec.get("区间数")
        try:
            return int(n) if n not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _shiny_grid_cards(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """名单行 → 网格图卡片。

        **一只精灵一张卡**（形态 / 进化阶段已经在 `_shiny_grid_groups` 并掉了）：

            头像   = 一阶段精灵图，外圈 = 系别色，**左下角盖系别章**（龙/翼）
            底色   = 系别色调淡（龙系 → 淡红）
            环绕   = 精灵蛋、异色蛋
            标签   = 精灵组别（蛋组）+ 同乘
        """
        c, g = _card(), _grid()
        if c is None or g is None:
            return []
        avatar_mode = self._grid_avatar_mode()
        ride_mode = self._grid_ride_mode()
        out: List[Dict[str, Any]] = []
        for grp in self._shiny_grid_groups(rows):
            nm = str(grp.get("name") or "").strip()
            if not nm:
                continue
            marks = list(grp.get("marks") or [])
            members = [x for x in (grp.get("members") or []) if x]
            st1 = self._stage1_path(nm)
            shiny = c.icon_path("shiny", nm)
            if avatar_mode == "shiny" and shiny:
                main = shiny
            else:
                main = st1 or c.icon_path("pet", nm) or shiny
            sats: List[Dict[str, Any]] = []
            # 一阶段立绘缩略图排在**最后**：卡片只取前 2 枚，前两枚必须是蛋和异色蛋
            st1_sat: Optional[Dict[str, Any]] = None
            if avatar_mode == "shiny" and st1 and main and Path(st1).name != Path(main).name:
                st1_sat = {"icon": st1}
            egg_name = str(grp.get("egg") or f"{nm}的蛋")
            egg_icon = c.icon_path("egg", egg_name)
            if egg_icon and Path(egg_icon).stem == "magic_egg":
                egg_icon = None                  # 通用占位白蛋不画进徽标
            if egg_icon:
                # 不指定 ring → 渲染器用这只精灵的系别色（整张卡颜色统一）
                sats.append({"icon": egg_icon, "text": "蛋"})
            # 异色蛋徽标：**按图标库有没有这张图来决定**，不看 marks。
            # （蛋组查询的行没有 marks，以前就会被漏掉 —— 蛋组卡片上就只剩一个蛋）
            # 图标库里的键有四种写法：异色XX的蛋 / 异色XX蛋 / 异色XX / XX的异色蛋，
            # 逐个试（只试前一种的话，大部分精灵都配不上异色蛋图）
            se = (c.icon_path("egg", f"异色{egg_name}")
                  or c.icon_path("egg", f"异色{nm}的蛋")
                  or c.icon_path("egg", f"异色{nm}蛋")
                  or c.icon_path("egg", f"异色{nm}")
                  or c.icon_path("egg", f"{nm}的异色蛋"))
            if se and (not egg_icon or Path(se).name != Path(egg_icon).name):
                sats.append({"icon": se, "text": "异色蛋"})
            # 徽标槽位**只放蛋**：一阶段立绘缩略图不再进槽位（头像本身已经区分了普通/异色立绘，
            # 放进来只会出现「蛋 + 立绘」这种不成对的组合，例如云梦豚）
            _ = st1_sat

            # 系别（左下角章 + 卡片底色）
            types = self._elem_types(nm)
            elem_txt = self._elem_label(types)
            elem_color = self._elem_color(types)
            tone = self._elem_tone(types) or "#4B5563"
            tint = self._grid_elem_tint()
            card_bg = self._mix_hex(elem_color, tint) if elem_color else ""
            card_line = self._mix_hex(elem_color, min(1.0, tint + 0.20)) if elem_color else ""
            # 卡片上了系别色，小字不能再是浅灰（看不清）：拿底色压暗当字色，对比度稳过 4.5
            sub_fg = (self._mix_hex(card_bg, 0.26, "#14110E")
                      if (card_bg and tint >= 0.30) else "")
            # 组别标签的字再压深一档（原来偏淡，看不清）
            tag_fg = (self._mix_hex(card_bg, 0.06, "#0A0806")
                      if card_bg else "")

            # 标签：精灵组别（蛋组）+ 同乘；异色 / 异色蛋 不再占标签位
            tags: List[Dict[str, str]] = []
            gs = [x for x in (grp.get("groups") or [])
                  if x and x not in ("未发现", "待定", "未分组")]
            for gp in gs[:2]:
                tags.append({"text": str(gp), "kind": "group",
                              "fg": tag_fg or sub_fg or tone, "bg": "#FFFFFF"})
            ride = grp.get("ride")
            if ride_mode == "both" and ride is not None:
                tags.append(g.tag("可同乘" if ride else "不可同乘",
                                  "ride" if ride else "noride"))
            elif ride_mode == "yes" and ride:
                tags.append(g.tag("可同乘", "ride"))
            if not tags:
                tags.append(g.tag(str(grp.get("group") or "未分组"), "group"))

            sub_parts: List[str] = []
            if grp.get("no"):
                sub_parts.append(f"图鉴 {grp['no']}")
            if len(members) > 1:
                # 合并进来的形态 / 阶段名字直接写出来（缇塔/声波缇塔），别让人以为漏了
                shown = [m for m in members[:3] if m]
                extra = len(members) - len(shown)
                if shown:
                    sub_parts.append("/".join(shown) + (f"+{extra}" if extra > 0 else ""))
            if grp.get("leader"):
                sub_parts.append("首领化")
            out.append(g.card(
                name=nm,
                sub=" · ".join(sub_parts),
                bins=self._bins_of(nm),
                icon=main,
                ring=g.ring_color(grp.get("group") or (gs[0] if gs else "")),
                tags=tags,
                # 最多 2 枚（普通蛋 + 异色蛋）。第 3/4 枚（一阶段图）会压到左下系别胶囊，
                # layout-checker 实测压掉 57.6%，所以只画两枚
                sats=sats[:2],
                elem=elem_txt,
                elem_color=elem_color,
                bg=card_bg,
                border=card_line,
                sub_fg=sub_fg,
            ))
        return out

    async def _as_png(self, res: Any) -> Optional[bytes]:
        """html_render 的返回值统一成 PNG 字节（bytes/本地路径/file:///http/data:）。"""
        if isinstance(res, (bytes, bytearray)):
            return bytes(res) or None
        s = str(res or "").strip()
        if not s:
            return None
        if s.startswith("data:"):
            try:
                return base64.b64decode(s.split(",", 1)[1])
            except Exception:
                return None
        if s.startswith("file://"):
            try:
                s = urllib.parse.unquote(s[7:])
                if re.match(r"^/[A-Za-z]:", s):     # file:///C:/x → C:/x
                    s = s[1:]
            except Exception:
                return None
        if s.startswith(("http://", "https://")):
            try:
                sess = await self._session_get()
                async with sess.get(
                        s, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status != 200:
                        return None
                    return await r.read()
            except Exception as e:
                logger.warning(f"[网格图] 截图下载失败: {type(e).__name__}: {e}")
                return None
        try:
            p = Path(s)
            if p.is_file():
                return p.read_bytes()
        except Exception:
            pass
        return None

    async def _html_grid_png(self, kw: Dict[str, Any]) -> Optional[bytes]:
        """走 AstrBot 的 html_render（Playwright 截图 templates/grid.html）。"""
        hr = getattr(self, "html_render", None)
        if not callable(hr):
            return None
        g = _grid()
        try:
            tmpl = (_PLUGIN_DIR / "templates" / "grid.html").read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"[网格图] 模板读取失败: {type(e).__name__}: {e}")
            return None
        try:
            data = g.html_data(**kw)
        except Exception as e:
            logger.warning(f"[网格图] 模板数据整理失败: {type(e).__name__}: {e}")
            return None
        opts = {"type": "png", "full_page": True, "omit_background": False,
                "scale": "device", "animations": "disabled"}
        tmo = float(self._int_cfg("grid_timeout", 30, 5, 120))
        res = None
        # 不同 AstrBot 版本的 html_render 参数不一样，挨个试
        for call in (lambda: hr(tmpl, data, return_url=False, options=opts),
                     lambda: hr(tmpl, data, options=opts)):
            try:
                res = await asyncio.wait_for(call(), timeout=tmo)
                break
            except TypeError:
                continue
            except asyncio.TimeoutError:
                logger.warning(f"[网格图] html_render 超时（>{tmo:.0f}s）")
                return None
            except Exception as e:
                logger.warning(f"[网格图] html_render 失败: {type(e).__name__}: {e}")
                return None
        return await self._as_png(res)

    # ---------------------------------------------------------------- 网格图缓存
    #: 内存 LRU（张数）。热点图（蛋组 / 异色）反复发，命中就 0 ms
    _GRID_MEM: "OrderedDict[str, bytes]" = OrderedDict()
    _GRID_MEM_MAX = 64
    #: 磁盘缓存留在 data/grid_cache/，重启后也能直接命中（不用再渲一遍）
    _GRID_DISK_MAX = 192

    #: 卡片缓存：蛋组 / 异色的 cards 只算一次（图标路径检查很费磁盘 IO）
    _GRID_CARDS: "OrderedDict[str, Any]" = OrderedDict()
    _GRID_CARDS_MAX = 48

    @staticmethod
    def _render_sig() -> str:
        """渲染器指纹：grid.py / 模板改了就变，旧缓存自动失效。

        （之前漏了这个 —— 改完画法后，缓存键没变，发出去还是旧图）
        """
        parts = []
        for n in ("grid.py", "templates/grid.html"):
            try:
                parts.append(os.path.getmtime(_PLUGIN_DIR / n))
            except Exception:
                parts.append(0)
        # 只认渲染器文件本身的 mtime：版本号变了但没动画法时，缓存不该失效
        # （以前带 VERSION，每升一版整库图全作废，预生成等于白跑）
        return hashlib.sha1(json.dumps(parts, default=str).encode("utf-8")).hexdigest()[:12]

    def _grid_sig(self) -> str:
        """影响卡片外观的配置 + 数据文件修改时间。任意一项变了，卡片缓存就失效。"""
        cfg = [
            str(self._cfg("grid_avatar", "stage1")),
            str(self._cfg("grid_show_ride", "yes")),
            str(self._cfg("grid_elem_tint", 0.62)),
            str(self._cfg("card_theme", "")),
            str(self._cfg("grid_theme", "")),
            str(self._cfg("grid_special_pets", "")),
        ]
        stamps = []
        for n in ("elements.json", "pets.json", "chains.json", "shiny_book.json",
                  "season_roster.json", "ranges.json"):
            try:
                stamps.append(Path(_PLUGIN_DIR / n).stat().st_mtime)
            except Exception:
                stamps.append(0)
        raw = json.dumps([cfg, stamps, self._render_sig()],
                         ensure_ascii=False, default=str)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _grid_cards_cache_get(self, key: str):
        cls = type(self)
        hit = cls._GRID_CARDS.get(key)
        if hit is not None:
            cls._GRID_CARDS.move_to_end(key)
        return hit

    def _grid_cards_cache_put(self, key: str, val) -> None:
        cls = type(self)
        cls._GRID_CARDS[key] = val
        cls._GRID_CARDS.move_to_end(key)
        while len(cls._GRID_CARDS) > cls._GRID_CARDS_MAX:
            cls._GRID_CARDS.popitem(last=False)

    def _group_cards(self, group: str) -> List[Dict[str, Any]]:
        """蛋组 → 全部卡片（不分常驻/奇遇，一张图装完）。带缓存，第二次起 0 ms。"""
        key = f"g|{group}|{self._grid_sig()}"
        hit = self._grid_cards_cache_get(key)
        if hit is not None:
            return hit
        rows = self._group_rows(group)
        val = self._shiny_grid_cards(sorted(rows, key=self._grid_sort_key))
        self._grid_cards_cache_put(key, val)
        return val

    def _grid_key(self, kw: Dict[str, Any]) -> str:
        """缓存键：内容 + 版式 + 引擎。配置一改，键就变，不会拿到旧图。"""
        payload = {k: kw.get(k) for k in (
            "header", "subtitle", "footer", "page", "total_pages", "total",
            "width", "cols", "font_size", "font_path", "theme")}
        payload["engine"] = self._grid_engine()
        payload["render"] = self._render_sig()
        payload["panels"] = kw.get("panels") or [{"cards": kw.get("cards") or []}]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _grid_cache_path(self, key: str) -> Optional[Path]:
        try:
            return _data_dir() / "grid_cache" / f"{key}.png"
        except Exception:
            return None

    def _grid_cache_get(self, key: str) -> Optional[bytes]:
        cls = type(self)
        hit = cls._GRID_MEM.get(key)
        if hit is not None:
            cls._GRID_MEM.move_to_end(key)
            return hit
        fp = self._grid_cache_path(key)
        try:
            if fp is not None and fp.is_file():
                data = fp.read_bytes()
                if data[:8] == b"\x89PNG\r\n\x1a\n":
                    self._grid_cache_put_mem(key, data)
                    return data
        except Exception:
            pass
        return None

    @classmethod
    def _grid_cache_put_mem(cls, key: str, data: bytes) -> None:
        cls._GRID_MEM[key] = data
        cls._GRID_MEM.move_to_end(key)
        while len(cls._GRID_MEM) > cls._GRID_MEM_MAX:
            cls._GRID_MEM.popitem(last=False)

    def _grid_cache_put(self, key: str, data: bytes) -> None:
        self._grid_cache_put_mem(key, data)
        if not bool(self._cfg("grid_cache", True)):
            return
        fp = self._grid_cache_path(key)
        try:
            if fp is None:
                return
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(data)
            files = sorted(fp.parent.glob("*.png"), key=lambda x: x.stat().st_mtime)
            for old in files[:-(type(self)._GRID_DISK_MAX)]:
                try:
                    old.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass

    async def _grid_png(self, *, header: str,
                        cards: Optional[List[Dict[str, Any]]] = None,
                        panels: Optional[List[Dict[str, Any]]] = None,
                        subtitle: str = "", footer: str = "",
                        page: int = 1, total_pages: int = 1,
                        total: int = 0, use_cache: bool = True) -> Optional[bytes]:
        """两条引擎一个出口：先查缓存，再试 HTML（CSS Grid），最后 Pillow 复刻。

        cards  —— 单栏
        panels —— 两栏（左常驻 / 右奇遇），`[{"title","color","cards"}, …]`
        """
        g = _grid()
        if g is None:
            return None
        kw: Dict[str, Any] = {
            "header": header,
            "cards": cards or [],
            "panels": panels,
            "subtitle": subtitle,
            "footer": footer,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "width": self._int_cfg("grid_width", 1080, 640, 1400),
            "cols": self._int_cfg("grid_cols", 4, 2, 6),
            "font_size": self._int_cfg("card_font_size", 18, 10, 40),
            "font_path": str(self._cfg("card_font_path", "") or ""),
            "theme": self._grid_theme_text(),
        }
        key = ""
        if use_cache and bool(self._cfg("grid_cache", True)):
            key = self._grid_key(kw)
            hit = self._grid_cache_get(key)
            if hit:
                return hit

        engine = self._grid_engine()
        png = None
        if engine in ("auto", "html"):
            try:
                png = await self._html_grid_png(kw)
            except Exception as e:
                logger.warning(f"[网格图] HTML 引擎异常: {type(e).__name__}: {e}")
                png = None
            if not png and engine == "html":
                logger.warning("[网格图] 指定 HTML 引擎但没渲染出来，改用 Pillow 复刻版")
        if not png:
            if not g.available():
                return None
            try:
                png = await asyncio.to_thread(lambda: g.render_grid(**kw))
            except Exception as e:
                logger.warning(f"[网格图] Pillow 渲染失败: {type(e).__name__}: {e}")
                return None
        if png and key:
            self._grid_cache_put(key, png)
        return png

    # ---------------------------------------------------------------- 分页 / 预生成
    def _build_page_panels(self, cards_res: List[Dict[str, Any]],
                           cards_enc: List[Dict[str, Any]], *, per: int
                           ) -> List[List[Dict[str, Any]]]:
        """把卡片排成每页的 panels。**不画栏目标题**，两栏之间只留一条竖虚线。"""
        cap2 = max(2, per // 2)                  # 两栏时每栏最多 per/2 只
        pages: List[List[Dict[str, Any]]] = []
        if cards_res and cards_enc:
            pages.append([{"title": "", "cards": cards_res[:cap2]},
                          {"title": "", "cards": cards_enc[:cap2]}])
            tail = cards_res[cap2:] + cards_enc[cap2:]
            for i in range(0, len(tail), per):
                pages.append([{"title": "", "cards": tail[i:i + per]}])
        else:
            seq = cards_enc or cards_res
            for i in range(0, max(1, len(seq)), per):
                pages.append([{"title": "", "cards": seq[i:i + per]}])
        return pages or [[{"title": "", "cards": []}]]

    async def _paginate_pngs(self, *, header: str,
                             cards_res: List[Dict[str, Any]],
                             cards_enc: List[Dict[str, Any]],
                             per: Optional[int] = None,
                             page: int = 0,
                             max_pages: Optional[int] = None,
                             all_pages: bool = False
                             ) -> Tuple[List[bytes], int, List[int], int]:
        """排页 + 渲染，返回 (png 列表, 总页数, 实际渲的页码, 总只数)。

        per 传进来就是「一页最多几张」；传 len(cards) 就强制一张图装完。
        """
        per = max(2, int(per if per else self._int_cfg("grid_per_page", 20, 4, 40)))
        total = len(cards_res) + len(cards_enc)
        pages_panels = self._build_page_panels(cards_res, cards_enc, per=per)
        pages = max(1, len(pages_panels))
        cap = self._int_cfg("grid_max_pages", 3, 0, 12) if max_pages is None else max_pages
        if page and page > 0:
            want = [min(page, pages)]
        elif all_pages:
            want = list(range(1, pages + 1))
        else:
            want = list(range(1, (pages if cap <= 0 else min(pages, cap)) + 1))

        pngs: List[bytes] = []
        for p in want:
            sub = f"第 {p} / {pages} 页 · 共 {total} 只" if pages > 1 else f"共 {total} 只"
            if cards_res and cards_enc:
                sub += f"（常驻 {len(cards_res)} + 奇遇 {len(cards_enc)}）"
            png = await self._grid_png(
                header=header, panels=pages_panels[p - 1],
                subtitle=sub, footer="", page=p, total_pages=pages, total=total)
            if png:
                pngs.append(png)
        return pngs, pages, want, total

    async def _emit_panels(self, event: Any, *, header: str, text: str,
                           cards_res: List[Dict[str, Any]],
                           cards_enc: List[Dict[str, Any]],
                           caption_lead: str, page: int = 0,
                           total_forms: int = 0):
        """两栏网格图出口（左常驻 / 右奇遇）。页面上不写栏目标题和底部说明。"""
        if not cards_res and not cards_enc:
            return await self._say(event, text)
        pngs, pages, want, total = await self._paginate_pngs(
            header=header, cards_res=cards_res, cards_enc=cards_enc, page=page)
        if not pngs:
            return await self._say(event, text)

        caption = f"{caption_lead}｜共 {total} 只"
        if total_forms and total_forms > total:
            caption += f"（{total_forms} 个形态）"
        if cards_res and cards_enc:
            caption += f"（常驻 {len(cards_res)} · 奇遇 {len(cards_enc)}）"
        if pages > 1:
            caption += f"｜第 {want[0]}-{want[-1]} / {pages} 页"
        r = self._result_images(event, pngs, caption)
        if r is not None:
            return r
        return event.plain_result(text)

    def _shiny_cards_pair(self, season: str = ""
                          ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """异色：常驻 / 奇遇 的卡片（名单以官方异色记录本为准）。带缓存。"""
        key = f"s|{season}|{self._grid_sig()}"
        hit = self._grid_cards_cache_get(key)
        if hit is not None:
            return hit
        # 指定赛季 → 只出这个赛季（单栏）；不指定 → 左常驻 / 右全部奇遇
        if season and season != "S0":
            rows_res: List[Dict[str, Any]] = []
            rows_enc = self._book_rows(season)
        elif season == "S0":
            rows_res, rows_enc = self._book_rows("S0"), []
        else:
            rows_res = self._book_rows("S0")
            rows_enc = [r for r in self._book_rows("")
                        if str(r.get("season")) != "S0"]
        cards_res = self._shiny_grid_cards(sorted(rows_res, key=self._grid_sort_key))
        cards_enc = self._shiny_grid_cards(sorted(rows_enc, key=self._grid_sort_key))
        self._grid_cards_cache_put(key, (cards_res, cards_enc))
        return cards_res, cards_enc

    async def _prewarm_grids(self) -> None:
        """启动预生成：所有蛋组、各赛季异色图先渲好（这些内容基本不变）。"""
        if not bool(self._cfg("grid_prewarm", True)):
            return
        if not (self._grid_enabled() and self._image_reply_on() and _grid() is not None):
            logger.info("[网格图] 预生成跳过（网格图关着或没 Pillow）")
            return
        t0 = time.time()
        done = 0
        failed = 0

        # ① 所有蛋组：一张图装完（跟在线查询完全一样，才能命中缓存）
        try:
            groups = sorted(self._all_groups())
        except Exception as e:
            groups = []
            logger.warning(f"[网格图] 蛋组列表读取失败: {type(e).__name__}: {e}")
        for grp in groups:
            try:
                cards = self._group_cards(grp)
                if cards:
                    pngs, _pages, _want, _total = await self._paginate_pngs(
                        header=f"蛋组 · {grp} · {len(cards)} 只",
                        cards_res=cards, cards_enc=[], per=len(cards), all_pages=True)
                    done += len(pngs)
            except Exception as e:
                failed += 1
                logger.warning(f"[网格图] 预生成蛋组「{grp}」失败: {type(e).__name__}: {e}")
            await asyncio.sleep(0)

        # ② 异色：常驻 + S1~S5 + 全部
        for season in ("S0", "S1", "S2", "S3", "S4", "S5", ""):
            try:
                c_res, c_enc = self._shiny_cards_pair(season)
                pngs, _pages, _want, _total = await self._paginate_pngs(
                    header=f"异色精灵 · {self._season_label(season)}",
                    cards_res=c_res, cards_enc=c_enc, all_pages=True)
                done += len(pngs)
            except Exception as e:
                failed += 1
                logger.warning(f"[网格图] 预生成异色「{season or '全部'}」失败: "
                               f"{type(e).__name__}: {e}")
            await asyncio.sleep(0)

        cost = time.time() - t0
        logger.info(f"[网格图] 预生成完成：{done} 张 / {len(groups) + 7} 组 · "
                    f"用时 {cost:.1f}s · 失败 {failed}")

    def _split_resident(self, rows: List[Dict[str, Any]]):
        """按赛季把行拆成（常驻, 奇遇）。"""
        res = [r for r in rows if str(r.get("season") or "S0") == "S0"]
        enc = [r for r in rows if str(r.get("season") or "S0") != "S0"]
        return res, enc

    def _shiny_args(self, raw: str):
        """「异色 s5 2」→ (赛季, 页码, 是否强制文字)。"""
        s = str(raw or "").lstrip("/#").strip()
        want_text = bool(re.search(r"(文字|文本|纯文字)", s))
        s = re.sub(r"(纯文字|文字|文本)", " ", s)
        season = ""
        m = re.search(r"(?:[sS]\s*([1-5])|第?\s*([1-5一二三四五])\s*赛季?)", s)
        rest = s
        if m:
            g = m.group(1) or m.group(2)
            n = self.CN_NUM.get(g) or (int(g) if str(g).isdigit() else 0)
            season = f"S{n}" if n else ""
            rest = (s[:m.start()] + " " + s[m.end():]).strip()
        elif ("全部" in s) or ("所有" in s):
            rest = re.sub(r"(全部|所有)", " ", s).strip()
        pm = re.search(r"(?:第\s*)?(\d{1,3})\s*页?", rest)
        page = int(pm.group(1)) if pm else 0
        return season, page, want_text

    async def _shiny_result(self, event: Any, season: str = "", page: int = 0):
        """异色名单：左常驻 / 右奇遇的网格图，出不了就发原来的文字版。"""
        text = self._shiny_report(season)
        # 指定赛季 → **只发这个赛季**（单栏，不再把常驻塞在左边）；
        # 不指定 → 左常驻 / 右全部奇遇
        if season and season != "S0":
            rows_res, rows_enc = [], self._book_rows(season)
        elif season == "S0":
            rows_res, rows_enc = self._book_rows("S0"), []
        else:
            rows_res = self._book_rows("S0")
            rows_enc = [r for r in self._book_rows("") if str(r.get("season")) != "S0"]
        if not rows_res and not rows_enc:
            return await self._say(event, text)
        if not (self._grid_enabled() and self._image_reply_on() and _grid() is not None):
            return await self._say(event, text)

        cards_res, cards_enc = self._shiny_cards_pair(season)
        label = self._season_label(season)
        return await self._emit_panels(
            event, header=f"异色精灵 · {label}", text=text,
            cards_res=cards_res, cards_enc=cards_enc,
            caption_lead=f"异色精灵 · {label}", page=page,
            total_forms=len(self._book_rows(season)) if season else
            len([r for r in self._book_rows("") if r.get("season") != "S0"]))

    #: 中文数字 → 赛季号
    CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}

    @filter.regex(r"^\s*[/#]?\s*(?:(?:查询|查)\s*)?(?:异色|闪光)\s*(?:(?:[sS]\s*[1-5]|第?\s*[1-5一二三四五]\s*赛季?)|全部|所有)?\s*(?:(?:第\s*)?\d{1,3}\s*页?)?\s*(?:纯文字|文字|文本)?\s*$")
    async def on_shiny_by_season(self, event: AstrMessageEvent):
        """异色 / 异色 s5 / 异色 s5 2 / 异色全部 3 —— 网格图看异色精灵（分页）"""
        raw = str(getattr(event, "message_str", "") or "")
        season, page, want_text = self._shiny_args(raw)
        if not await self._perm_gate(event, "market"):
            return
        if want_text:
            yield await self._say(event, self._shiny_report(season))
            return
        yield await self._shiny_result(event, season, page)

    @filter.regex(r"^\s*[/#]?\s*[sS]\s*[1-5]\s*(?:异色|闪光)\s*(?:(?:第\s*)?\d{1,3}\s*页?)?\s*(?:纯文字|文字|文本)?\s*$")
    async def on_shiny_by_season2(self, event: AstrMessageEvent):
        """s5 异色 / s5 异色 2（赛季写前面）"""
        raw = str(getattr(event, "message_str", "") or "")
        season, page, want_text = self._shiny_args(raw)
        if not await self._perm_gate(event, "market"):
            return
        if want_text:
            yield await self._say(event, self._shiny_report(season))
            return
        yield await self._shiny_result(event, season, page)

    @filter.command("花种")
    async def cmd_flower(self, event: AstrMessageEvent):
        """花种 —— 列全部品种；花种 罗隐 —— 看这只精灵的花种"""
        # 保险：消息里必须真有「花种」两个字，避免被别的入口误派发过来
        _raw = str(getattr(event, "message_str", "") or "").lstrip("/#").strip()
        if "花种" not in _raw:
            return
        if not self._flower_claim(event):
            return
        arg = self._cmd_args(event)
        if not arg:
            m = re.match(r"^\s*[/#]?\s*花\s*种\s*(.+)$",
                         str(getattr(event, "message_str", "") or ""))
            arg = m.group(1).strip() if m else ""
        if not arg:
            yield await self._flower_all(event)
            return
        yield await self._flower_reply(event, arg)

    @filter.regex(r"^\s*[/#]?\s*花\s*种\s*[\u4e00-\u9fa5A-Za-z0-9·]{2,20}\s*$")
    async def on_flower_loose(self, event: AstrMessageEvent):
        """兜底：/花种罗隐 这种不加空格的写法。"""
        if not self._flower_claim(event):
            return
        raw = str(getattr(event, "message_str", "") or "").lstrip("/#").strip()
        m = re.match(r"^花\s*种\s*(.+)$", raw)
        if not m:
            return
        yield await self._flower_reply(event, m.group(1).strip())

    # ---------------------------------------------------------------- 花种订阅
    #: 订阅结构： state["flower_subs"] = [{"kind": "color|particle|species", "value": "...", "at": ts}]
    FLOWER_KIND = {
        "颜色": "color", "色": "color", "color": "color",
        "粒子": "particle", "part": "particle", "particle": "particle",
        "精灵": "species", "宠物": "species", "species": "species", "pet": "species",
    }

    def _flower_subs(self) -> List[Dict[str, Any]]:
        subs = self.state.get("flower_subs")
        return subs if isinstance(subs, list) else []

    def _save_flower_subs(self, subs: List[Dict[str, Any]]) -> None:
        self.state["flower_subs"] = subs[-200:]
        _save_state(self.state)

    @filter.command("花种订阅")
    async def cmd_flower_sub(self, event: AstrMessageEvent):
        """花种订阅 [颜色/粒子/精灵] [值] —— 有匹配的新花种就推送"""
        if not await self._perm_gate(event, "flower"):
            return
        parts = self._cmd_parts(event)
        if len(parts) < 2:
            subs = self._flower_subs()
        if not await self._perm_gate(event, "flower"):
            return
        parts = self._cmd_parts(event)
        if len(parts) < 2:
            subs = self._flower_subs()
            if not subs:
                yield await self._say(
                    event,
                    "还没订阅花种。用法：\n"
                    "花种订阅 颜色 浅橙青\n"
                    "花种订阅 粒子 四角星\n"
                    "花种订阅 精灵 罗隐\n"
                    "花种订阅 列表       看当前订阅\n"
                    "花种订阅 删除 1      删掉第 1 条")
                return
            lines = [f"花种订阅（{len(subs)} 条）"]
            for i, s in enumerate(subs, 1):
                k = {"color": "颜色", "particle": "粒子", "species": "精灵"}.get(
                    str(s.get("kind")), str(s.get("kind")))
                lines.append(f"{i}. {k} · {s.get('value')}")
            lines.append("删除：花种订阅 删除 1")
            yield await self._say(event, "\n".join(lines))
            return

        sub = parts[1]
        if sub in ("列表", "list"):
            subs = self._flower_subs()
            if not subs:
                yield await self._say(event, "还没有任何花种订阅")
                return
            lines = [f"花种订阅（{len(subs)} 条）"]
            for i, s in enumerate(subs, 1):
                k = {"color": "颜色", "particle": "粒子", "species": "精灵"}.get(
                    str(s.get("kind")), str(s.get("kind")))
                lines.append(f"{i}. {k} · {s.get('value')}")
            yield await self._say(event, "\n".join(lines))
            return

        if sub in ("删除", "删", "del", "remove"):
            subs = self._flower_subs()
            if len(parts) < 3 or not parts[2].isdigit():
                yield await self._say(event, "用法：花种订阅 删除 1（序号看「花种订阅 列表」）")
                return
            idx = int(parts[2]) - 1
            if not (0 <= idx < len(subs)):
                yield await self._say(event, f"序号超出范围（当前 {len(subs)} 条）")
                return
            gone = subs.pop(idx)
            self._save_flower_subs(subs)
            yield await self._say(event, f"已删除订阅：{gone.get('value')}")
            return

        if sub in ("清空", "clear"):
            self._save_flower_subs([])
            yield await self._say(event, "已清空全部花种订阅")
            return

        kind = self.FLOWER_KIND.get(sub)
        if not kind:
            yield await self._say(
                event, "第二个词只能是：颜色 / 粒子 / 精灵\n"
                       "例：花种订阅 颜色 浅橙青")
            return
        value = " ".join(parts[2:]).strip()
        if not value:
            yield await self._say(event, f"格式：花种订阅 {sub} 要订阅的内容")
            return

        # 精灵订阅：验证一下图鉴里有
        if kind == "species":
            rec = self._find_species(value)
            if not rec:
                yield await self._say(event, f"图鉴里没有「{value}」，换个名字试试")
                return
            value = str(rec.get("精灵") or value)

        subs = self._flower_subs()
        if any(s.get("kind") == kind and s.get("value") == value for s in subs):
            yield await self._say(event, f"已经订阅过「{value}」了")
            return
        subs.append({"kind": kind, "value": value,
                     "at": int(time.time())})
        self._save_flower_subs(subs)
        ktxt = {"color": "颜色", "particle": "粒子", "species": "精灵"}[kind]
        yield await self._say(
            event, f"✅ 已订阅{ktxt}：{value}\n"
                   f"有匹配的新花种上架就会推送（当前共 {len(subs)} 条订阅）")

    # ---------------------------------------------------------------- 花种（炫彩花种市场）
    #: 花种走 /miniapp/api/grocery/flowers/*，不是 /cloud/*
    FLOWER_CATALOG_API = "/miniapp/api/grocery/flowers/catalog"
    FLOWER_OFFERS_API = "/miniapp/api/grocery/flowers/offers"

    #: 外观类型 → 中文
    FLOWER_APPEARANCE = {
        "colorful": "炫彩",
        "shiny": "异色",
        "": "未标注",
    }
    #: 花种类型
    FLOWER_TYPE = {
        "destined": "命定",
        "ordinary": "普通",
    }
    #: 交易状态
    FLOWER_STATE = {
        "selling": "售票中",
        "formed": "已成团",
        "held": "待成团",
        "locked": "已确认上车",
        "confirmed": "已成团",
        "completed": "交易完成",
        "cancelled": "已取消",
        "awaiting_relist": "待重新上架",
        "refunded": "已退款",
    }

    #: 花种目录缓存：(时间戳, 列表)。花种接口有频率限制（429 RATE_LIMITED），
    #: 轮询一次要扫 40+ 个品种，不缓存就会把自己限流 —— 一被限流，
    #: 「花种 xxx」查询也会跟着查不到东西。
    _FLOWER_CAT_CACHE: tuple = (0.0, None)
    FLOWER_CAT_TTL = 90

    async def _flower_catalog(self, force: bool = False) -> Optional[List[Dict[str, Any]]]:
        """花种品种列表：每个品种带挂单数和最低价。

        **必须翻页**：服务端把 pageSize 强制成 12（传 50/100 都没用），
        只拉第 1 页会漏掉后半部分（比如蝎子王在第 2 页）。

        带 90 秒缓存 + 429 退避：命中限流时优先用上次的缓存，
        实在没有才返回 None（调用方据此区分「接口坏了」和「市场真空」）。
        """
        now = time.time()
        ts, cached = self._FLOWER_CAT_CACHE
        if not force and cached is not None and now - ts < self.FLOWER_CAT_TTL:
            return cached
        out: List[Dict[str, Any]] = []
        page = 1
        failed = False
        limited = False
        while page <= 30:
            try:
                data = await self._api(
                    f"/api/grocery/flowers/catalog?page={page}&pageSize=12")
            except Exception as e:
                logger.warning(f"[花种] 取目录失败(page {page}): "
                               f"{type(e).__name__}: {e}")
                failed = True
                break
            if not isinstance(data, dict):
                logger.warning("[花种] 目录接口返回异常")
                failed = True
                break
            if not data.get("ok", True):
                err = str(data.get("error") or "")[:80]
                code = str(data.get("code") or "")
                if "RATE_LIMITED" in code or "频繁" in err or data.get("http") == 429:
                    limited = True
                    logger.warning(f"[花种] 被限流了（{code or '429'}），本轮先跳过；"
                                   f"90 秒内复用上次目录")
                else:
                    logger.warning(f"[花种] 目录接口报错: {err}")
                failed = True
                break
            items = [x for x in (data.get("items") or []) if isinstance(x, dict)]
            out.extend(items)
            try:
                pages = int(data.get("pages") or 1)
            except (TypeError, ValueError):
                pages = 1
            if page >= pages or not items:
                break
            page += 1
        if limited:
            self.state["flower_limited_at"] = int(now)
            return list(cached) if cached else None
        if failed and not out:
            return list(cached) if cached else None
        self._FLOWER_CAT_CACHE = (now, out)
        return out                 # 可能是空列表（市场真没货）

    async def _flower_offers(self, pet_id: Any) -> List[Dict[str, Any]]:
        """某个花种的全部挂单（同样翻页，服务端 pageSize 上限 12）。"""
        out: List[Dict[str, Any]] = []
        page = 1
        while page <= 30:
            try:
                data = await self._api(
                    f"/api/grocery/flowers/offers?petId={int(pet_id)}"
                    f"&page={page}&pageSize=12")
            except Exception as e:
                logger.warning(f"[花种] 取挂单失败: {type(e).__name__}: {e}")
                break
            if not isinstance(data, dict):
                break
            if not data.get("ok", True):
                err = str(data.get("error") or "")[:60]
                code = str(data.get("code") or "")
                if "RATE_LIMITED" in code or "频繁" in err or data.get("http") == 429:
                    logger.warning(f"[花种] 取挂单被限流（{code or '429'}）")
                    self.state["flower_limited_at"] = int(time.time())
                break
            items = [x for x in (data.get("items") or []) if isinstance(x, dict)]
            out.extend(items)
            try:
                pages = int(data.get("pages") or 1)
            except (TypeError, ValueError):
                pages = 1
            if page >= pages or not items:
                break
            page += 1
        return out

    @classmethod
    def _flower_brief(cls, offer: Dict[str, Any]) -> Dict[str, Any]:
        """把一条挂单压成推送/展示要用的字段。"""
        f = offer.get("flower") or {}
        gi = f.get("glassInfo") or {}
        colors = gi.get("colors") or {}
        parts = gi.get("particles") or {}
        appearance = str(f.get("appearanceType") or "")
        return {
            "id": str(offer.get("id") or ""),
            "pet_id": f.get("petId"),
            "name": str(f.get("name") or ""),
            "seller": str(offer.get("sellerName") or ""),
            "seller_id": str(offer.get("sellerId") or ""),
            "price": offer.get("price"),
            "state": str(offer.get("state") or ""),
            "people": offer.get("people"),
            "min_people": offer.get("minPeople"),
            "max_people": offer.get("maxPeople"),
            "type": str(f.get("type") or ""),
            "appearance": appearance,
            "appearance_txt": cls.FLOWER_APPEARANCE.get(appearance, appearance or "未标注"),
            "star": f.get("star"),
            "nature": str(f.get("nature") or ""),
            "color_name": str(colors.get("n") or ""),
            "c1": str(colors.get("c1") or ""),
            "c2": str(colors.get("c2") or ""),
            "particle_name": str(parts.get("n") or ""),
            "particle_card": str(parts.get("card") or ""),
            "color_id": gi.get("color_id"),
            "particle_id": gi.get("particle_id"),
            "remark": str(offer.get("remark") or ""),
            "cutoff_at": offer.get("cutoffAt"),
            "departure_at": offer.get("departureAt"),
        }

    #: 官方色卡图缓存：(particle, color) → webp 字节
    _CHROMA_CACHE: Dict[tuple, bytes] = {}
    _CHROMA_CACHE_MAX = 200

    async def _chroma_card_bytes(self, particle: Any, color: Any):
        """从控制台拿官方色卡图（控制台已把 156 张全量下到本地）。"""
        try:
            p = int(particle)
            c = int(color)
        except (TypeError, ValueError):
            return None
        key = (p, c)
        hit = self._CHROMA_CACHE.get(key)
        if hit is not None:
            return hit
        try:
            sess = await self._session_get()
            async with sess.get(
                    f"{self.api_base}/api/chroma/card/{p}/{c}",
                    timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
        except Exception as e:
            logger.warning(f"[花种] 取色卡图失败 {p}/{c}: {type(e).__name__}: {e}")
            return None
        if not data or len(data) < 200:
            return None
        if len(self._CHROMA_CACHE) >= self._CHROMA_CACHE_MAX:
            try:
                self._CHROMA_CACHE.pop(next(iter(self._CHROMA_CACHE)))
            except Exception:
                pass
        self._CHROMA_CACHE[key] = data
        return data

    def _flower_species_name(self, pet_id: Any, fallback: str = "") -> str:
        """petId 换成图鉴里的精灵名（任意阶段都能查到一阶段）。"""
        if fallback:
            rec = self._find_species(fallback, strict=True) or self._find_species(fallback)
            if rec:
                return str(rec.get("精灵") or fallback)
        return fallback or f"petId={pet_id}"

    def _flower_match_pet(self, query: str) -> Optional[Dict[str, Any]]:
        """「花种 xxx」里的 xxx → 图鉴记录（支持进化链任意阶段名）。"""
        q = str(query or "").strip()
        if not q:
            return None
        return self._find_species(q)

    #: 帮助菜单分段 —— 按权限拼（没权限的段不显示）
    MENU_HEAD = "🥚 洛克小肥鱼 · 可用指令"

    MENU_BASIC = (
        "—————— 查蛋 ——————\n"
        "查询 火花               区间数/蛋组/进化链/身高体重/过线值\n"
        "查蛋 火花               跟「查询」完全一样\n"
        "/查询火花               不加空格也行（前面带斜线）\n"
        "查询 火神               进化链上任意名字都行\n"
        "查询 鸭吉吉             多形态会列出每种规格\n"
        "查询 拟人组             按蛋组列全部精灵（一阶段名称）\n"
        "查询 组                 看有哪些蛋组、各多少只\n"
        "0.32 2.12               身高体重查蛋（身高按米算）\n"
        "34/3.617                身高厘米 + 体重千克（斜杠也行）\n"
        "34 3617                 整数体重按克算\n"
        "（查蛋会显示真实孵化概率，低于 1% 默认隐藏）"
    )

    MENU_MARKET = (
        "—————— 市场价 ——————\n"
        "查询 火花 最近          最近成交：4 种牌子各 10 笔\n"
        "（查蛋时会顺带显示实时行情）"
    )

    MENU_SHINY = (
        "—————— 异色名单（4 列网格图）——————\n"
        "异色                     全部赛季的异色精灵\n"
        "异色 s5                  只看 S5（头像=异色立绘，四角小图标=一阶段/蛋/异色蛋）\n"
        "异色 s5 2                翻到第 2 页（一页 16 只）\n"
        "异色全部 3               全部赛季的第 3 页\n"
        "异色 s5 文字             强制发文字版名单\n"
        "（头像外圈颜色 = 蛋组；紫=异色 · 青=异色蛋 · 橙=首领化）"
    )

    MENU_FLOWER = (
        "—————— 花种（炫彩花种市场）——————\n"
        "花种                     列市场上所有品种\n"
        "花种 罗隐                看这只精灵的色卡（进化链任意阶段）\n"
        "花种 四角星              看有这种粒子的花种\n"
        "花种 浅橙青              看这种颜色的花种\n"
        "花种 异色                看异色花种（炫彩同理）\n"
        "花种 玩家名              看这个卖家上架的花种\n"
        "花种订阅 颜色/粒子/精灵   订阅，新上架自动推\n"
        "花种订阅 列表 / 删除 1"
    )

    MENU_ID = (
        "—————— 查助手（商家）——————\n"
        "查ID Z10014514          昵称/绑定角色/上架/售出/好评差评\n"
        "查询ID xxx              和 查ID 完全一样"
    )

    MENU_MARKS = (
        "—————— 名单 ——————\n"
        "标 Z10014514 黑 备注     加黑名单（备注可省略）\n"
        "标 Z10014514 白 备注     加白名单\n"
        "取消标 Z10014514         移出名单\n"
        "黑名单 / 白名单          列全部"
    )

    MENU_WATCH = (
        "—————— 新上架提醒 / 推送开关 ——————\n"
        "新上架 添加 Z10014514    开始监控这个商家\n"
        "新上架 删除 Z10014514    取消监控\n"
        "新上架 列表              看监控名单 + 三个开关状态\n"
        "蛋推送状态               看所有开关 + 最近 5 条推送从哪发出来的\n"
        "蛋推送群关 / 蛋推送群开  只开关群聊推送（私聊照收）\n"
        "蛋推送买关 / 蛋推送买开  只开关「买到的蛋」播报\n"
        "蛋推送新关 / 蛋推送新开  只开关新上架提醒\n"
        "蛋推送花关 / 蛋推送花开  只开关花种上新\n"
        "蛋推送关 / 蛋推送开      推送总开关（三条管线全停）"
    )

    MENU_PUSH = (
        "—————— 推送 ——————\n"
        "蛋推送绑定 / 解绑 / 测试 / 状态 / 重置"
    )

    MENU_TAIL = (
        "—————— 其它 ——————\n"
        "UID 503671642           用游戏 UID 查等级/在线/家园/收集度\n"
        "帮助 / 菜单              看这张卡片\n"
        "回复默认都是图片         配置里关掉 reply_as_image 就发文字"
    )

    #: 完整版（管理员看的，全列出）
    MENU = "\n".join([MENU_HEAD, MENU_BASIC, MENU_MARKET, MENU_SHINY, MENU_FLOWER,
                     MENU_ID, MENU_MARKS, MENU_WATCH, MENU_PUSH, MENU_TAIL])

    async def _menu_for(self, event: Any) -> str:
        """按这个人的权限拼菜单：没权限的功能整段不显示。"""
        parts = [self.MENU_HEAD, self.MENU_BASIC]
        if await self._perm_allowed(event, "market"):
            parts.append(self.MENU_MARKET)
            parts.append(self.MENU_SHINY)
        if await self._perm_allowed(event, "flower"):
            parts.append(self.MENU_FLOWER)
        if await self._perm_allowed(event, "lookup_id"):
            parts.append(self.MENU_ID)
        if await self._perm_allowed(event, "marks"):
            parts.append(self.MENU_MARKS)
        if await self._perm_allowed(event, "watch"):
            parts.append(self.MENU_WATCH)
        if await self._perm_allowed(event, "push"):
            parts.append(self.MENU_PUSH)

        tail = self.MENU_TAIL
        # 管理员才看得到的：诊断 / 改绑改名记录
        if self._is_admin(event):
            tail = tail.replace(
                "帮助 / 菜单              看这张卡片\n",
                "帮助 / 菜单              看这张卡片\n"
                "诊断                     管理员用：查图片链路哪里挂了\n"
                "改绑记录 / 改名记录      看绑定角色有没有被换绑 / 改名\n")
        parts.append(tail)

        return "\n".join(parts)

    FORMAT_HINTS = {
        "查询": "格式：查询 火花（查蛋 也一样）\n进化链上任意名字都行：查询 火神 / 查询 焰火\n记不全也行：查询 翡翠母 / 查询 水母\n打拼音首字母也行：查询 fcsm（翡翠水母）/ 查询 dle（豆粒鹅）\n查蛋组：查询 拟人组 / 查询 拟人 / 查询 海洋\n查最近成交：查询 火花 最近",
        "查蛋": "格式：查蛋 火花\n和「查询」完全一样，斜杠可加可不加",
        "查ID": "格式：查ID Z10014514\n也可以用平台昵称：查ID 咕咕",
        "查询ID": "格式：查询ID Z10014514（和 查ID 一样）",
        "新上架": "格式：\n新上架 添加 Z10014514\n新上架 删除 Z10014514\n新上架 列表",
        "标": "格式：标 Z10014514 黑 备注写这里\n（第二项只能是「黑」或「白」）",
        "黑名单": "直接发「黑名单」就能列出全部\n加人：标 Z10014514 黑 备注",
        "白名单": "直接发「白名单」就能列出全部\n加人：标 Z10014514 白 备注",
    }

    # ---------------------------------------------------------------- 指令

    # ---------------------------------------------------------------- 指令：新上架监控
    #: 「新上架 删除Z10014514」「新上架删除 Z10014514」这种不加空格/多加空格的写法
    _WATCH_LOOSE_RE = re.compile(
        r"^\s*[/#]?\s*新\s*上\s*架\s*(添加|加|删除|删|取消|停止|列表|名单)?\s*([Zz]\d{5,12})?\s*$")

    @filter.command("新上架")
    async def cmd_watch(self, event: AstrMessageEvent):
        """新上架 添加 Z10014514 / 新上架 删除 Z10014514 / 新上架 列表"""
        if not await self._perm_gate(event, "watch"):
            return
        args = self._cmd_parts(event)
        sub = (args[1].strip() if len(args) > 1 else "")
        sub_low = sub.lower()
        target = (args[2].strip().upper() if len(args) > 2 else "")
        hint = self.FORMAT_HINTS["新上架"]

        # 没空格 / 多空格：从整条消息里抠出动作和 Z 号
        m = self._WATCH_LOOSE_RE.match(str(getattr(event, "message_str", "") or ""))
        if m:
            act, zid = m.group(1) or "", m.group(2) or ""
            if zid and not target:
                target = zid.upper()
            if act:
                sub, sub_low = act, act.lower()      # 动作以整条消息抠出来的为准

        # 新上架 列表
        if sub_low in ("列表", "名单", "list", "查看", "ls", "状态") or sub in ("列表", "名单"):
            watch = self._watch()
            cfg_ids = set(self._cfg_watch_ids())
            added = set((self.state.get("watch_ops") or {}).keys())
            lines = [self._push_switch_text(), ""]
            if not watch:
                lines.append("监控名单是空的（配置 watch_sellers 也没填）")
                lines.append("加一个：新上架 添加 Z10014514")
            else:
                lines.append(f"新上架监控（{len(watch)} 个）")
                for sid in watch:
                    src = "指令" if sid in added else ("配置" if sid in cfg_ids else "旧记录")
                    lines.append(f"· {sid}（{src}）")
                lines.append("加：新上架 添加 Z10014514 / 删：新上架 删除 Z10014514")
            lines.append("停推送：新上架 关（只停新上架）/ 蛋推送关（全停）/ 蛋推送群关（只停群）")
            yield await self._say(event, "\n".join(lines))
            return

        # 推送开关：新上架 关 / 新上架 开
        if sub_low in ("关", "关闭", "停", "off", "暂停") and not target:
            self._set_push_switch("listing_push_off", False)
            yield await self._say(event, "🔴 新上架提醒已关（购买推送不受影响）\n" + self._push_switch_text())
            return
        if sub_low in ("开", "开启", "on", "恢复") and not target:
            self._set_push_switch("listing_push_off", True)
            yield await self._say(event, "🟢 新上架提醒已开\n" + self._push_switch_text())
            return

        # 新上架 清空（配置里填的也一起停掉）
        if sub_low in ("清空", "全删", "clear", "停止全部") and not target:
            for sid in self._watch():
                self._set_watch(sid, False)
            yield await self._say(event, "🗑 监控名单已清空（配置 watch_sellers 里的也停掉了）\n"
                                          + self._push_switch_text())
            return

        # 允许「新上架 Z10014514」直接当添加
        if re.fullmatch(r"[Zz]\d{5,12}", sub) and not target:
            target, sub_low = sub.upper(), "添加"

        if not target:
            yield await self._say(event, hint)
            return

        if sub_low in ("添加", "加", "add", "监控", "订阅"):
            was = self._set_watch(target, True)
            n = len(self._watch())
            yield await self._say(event,
                                  (f"{target} 已经在监控里了（共 {n} 个）" if was
                                   else f"✅ 已开始监控 {target}\n监控名单：{n} 个"))
            return

        if sub_low in ("删除", "删", "del", "remove", "取消", "停止", "退订"):
            was = self._set_watch(target, False)
            n = len(self._watch())
            yield await self._say(event,
                                  (f"🗑 已停止监控 {target}，之后不再推它的新上架\n"
                                   f"（配置里的 watch_sellers 也盖得住，"
                                   f"剩下的监控：{n} 个）" if was
                                   else f"{target} 本来就不在监控里（现在 {n} 个）"))
            return

        yield await self._say(event, hint)

    # ---------------------------------------------------------------- 指令：推送开关
    @filter.regex(
        r"^\s*[/#]?\s*蛋\s*推\s*送\s*(群\s*聊?|买|购买|播报|新上架|上新|新|花种|花)?"
        r"\s*(关|开|状态|停止|恢复|关闭|开启)\s*$")
    async def on_push_switch(self, event: AstrMessageEvent):
        """蛋推送关 / 蛋推送开 / 蛋推送群关 / 蛋推送买关 / 蛋推送新关 / 蛋推送花关 / 蛋推送状态

        三条管线分别管：购买播报（买）、新上架提醒（新）、花种订阅（花）。
        **监控名单只影响「新上架提醒」**，名单清空后还在收到「买到的蛋」播报，
        就是「购买播报」那条在发 —— 发 `蛋推送买关` 或 `蛋推送关`。
        """
        if not await self._perm_gate(event, "push"):
            return
        raw = str(getattr(event, "message_str", "") or "").strip().lstrip("/#")
        m = re.search(
            r"蛋\s*推\s*送\s*(群\s*聊?|买|购买|播报|新上架|上新|新|花种|花)?"
            r"\s*(关|开|状态|停止|恢复|关闭|开启)\s*$", raw)
        if not m:
            yield await self._say(event, self._push_switch_text() + "\n\n" + self._push_log_text())
            return
        what = (m.group(1) or "").replace(" ", "")
        act = m.group(2)
        if act == "状态":
            yield await self._say(event, self._push_switch_text() + "\n\n" + self._push_log_text())
            return
        on = act in ("开", "恢复", "开启")

        # 哪条管线
        if what in ("群", "群聊"):
            key, name = "group_off", "群聊推送"
        elif what in ("买", "购买", "播报"):
            key, name = "purchase_off", "购买播报"
        elif what in ("新", "新上架", "上新"):
            key, name = "listing_push_off", "新上架提醒"
        elif what in ("花", "花种"):
            key, name = "flower_push_off", "花种订阅"
        else:
            key, name = "push_off", "推送总开关"

        self._set_push_switch(key, on)
        if key == "push_off" and on:
            # 「蛋推送开」= 真的全部恢复：顺手把三条管线的分开关也打开，
            # 否则用户以为开了其实还在关着（单条管线关着的时候最坑）
            for k in self.KIND_SWITCH.values():
                if float(self.state.get(k) or 0):
                    self._set_push_switch(k, True)
        head = (f"🟢 {name} 已开" if on else f"🔴 {name} 已关")
        tail = {
            "group_off": "（之后只发私聊）" if not on else "（群聊恢复接收）",
            "purchase_off": "（买到的蛋不再播报）" if not on else "（买到的蛋恢复播报）",
            "listing_push_off": "（监控名单留着也不推）" if not on else "",
            "flower_push_off": "（花种上新不推）" if not on else "",
            "push_off": "（三条管线全停，轮询还在跑）" if not on else "（恢复推送）",
        }.get(key, "")
        yield await self._say(event, f"{head}{tail}\n\n" + self._push_switch_text()
                             + "\n\n" + self._push_log_text())


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
    COMMAND_WORDS = ("查询ID", "查询", "查蛋", "查ID", "标", "取消标", "黑名单", "白名单",
                     "新上架", "帮助", "菜单", "蛋推送",
                     "花种", "花种订阅", "家园", "UID", "查UID", "游戏ID",
                     "改绑记录", "改名记录", "诊断")

    def _looks_like_command(self, text: str) -> bool:
        t = str(text or "").lstrip()
        if self._ID_CMD_RE.match(t):
            return True
        return any(t.startswith(w) for w in self.COMMAND_WORDS)

    # ---------------------------------------------------------------- 指令：黑白名单
    @filter.command("标")
    async def cmd_mark(self, event: AstrMessageEvent):
        if not await self._perm_gate(event, "marks"):
            return
        """标 Z10014514 黑 欠钱不还"""
        args = self._cmd_parts(event)
        if len(args) < 3:
            yield await self._say(event, "用法：标 Z10014514 黑 备注写这里\n（黑=黑名单，白=白名单，备注可省略）")
            return
        sid = args[1].upper()
        kind = args[2]
        if kind not in ("黑", "白"):
            yield await self._say(event, "第二个参数只能是「黑」或「白」")
            return
        note = " ".join(args[3:])
        by = getattr(event, "unified_msg_origin", "") or ""
        rec = self._set_mark(sid, kind, note, by)
        yield await self._say(event, await self._describe_mark(sid, rec))

    @filter.regex(r"^\s*[/#]?\s*标\s*[Zz]\d{5,12}\s*(黑|白).*$")
    async def on_mark_loose(self, event: AstrMessageEvent):
        """兜底入口：不带斜线的「标 Z10026057 黑 备注」。"""
        if not self._sp_dedupe(event):
            return
        m = self._MARK_LOOSE_RE.match(
            str(getattr(event, "message_str", "") or "").lstrip("/#").strip())
        if not m:
            return
        sid = m.group(1).upper()
        kind = m.group(2)
        note = (m.group(3) or "").strip()
        by = str(getattr(event, "unified_msg_origin", "") or "")
        rec = self._set_mark(sid, kind, note, by)
        yield await self._say(event, await self._describe_mark(sid, rec))

    @filter.command("取消标")
    async def cmd_unmark(self, event: AstrMessageEvent):
        if not await self._perm_gate(event, "marks"):
            return
        args = self._cmd_parts(event)
        if len(args) < 2:
            yield await self._say(event, "用法：取消标 Z10014514")
            return
        sid = args[1].upper()
        yield await self._say(event, f"已取消 {sid} 的标记" if self._del_mark(sid)
                                 else f"{sid} 不在名单里")

    @filter.command("黑名单")
    async def cmd_blacklist(self, event: AstrMessageEvent):
        if not await self._perm_gate(event, "marks"):
            return
        rows = self._marks_of("黑")
        if not rows:
            yield await self._say(event, "黑名单是空的")
            return
        lines = [f"黑名单（{len(rows)} 个）"]
        for sid, rec in rows[:30]:
            info = await self._seller_info(sid)
            nick = (info or {}).get("nickname") or "—"
            note = f" · {rec['note']}" if rec.get("note") else ""
            lines.append(f"· {nick}（{sid}）{note}")
        if len(rows) > 30:
            lines.append(f"…… 还有 {len(rows) - 30} 个")
        yield await self._say(event, "\n".join(lines))

    @filter.command("白名单")
    async def cmd_whitelist(self, event: AstrMessageEvent):
        if not await self._perm_gate(event, "marks"):
            return
        rows = self._marks_of("白")
        if not rows:
            yield await self._say(event, "白名单是空的")
            return
        lines = [f"白名单（{len(rows)} 个）"]
        for sid, rec in rows[:30]:
            info = await self._seller_info(sid)
            nick = (info or {}).get("nickname") or "—"
            note = f" · {rec['note']}" if rec.get("note") else ""
            lines.append(f"· {nick}（{sid}）{note}")
        if len(rows) > 30:
            lines.append(f"…… 还有 {len(rows) - 30} 个")
        yield await self._say(event, "\n".join(lines))

    @filter.command("帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield await self._say(event, await self._menu_for(event))

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
        _wm = await self._perm_allowed(event, "market")
        _t, _p = await self._species_payload(rec, with_market=_wm)
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

    @filter.regex(r"^\s*[/#]?\s*[Zz]\d{5,12}\s*(黑|白)?\s*.*$")
    async def on_bare_zid(self, event: AstrMessageEvent):
        """直接发 Z10014514 查商家；发「Z10014514 黑 备注」加名单。

        AstrBot 派发前会把 / 剥掉，所以 /Z10014514 也会走到这里。
        """
        raw = (getattr(event, "message_str", "") or "").strip().lstrip("/#").strip()
        # 空格可有可无：Z10011581黑傻狗一个 / Z10011581 黑 傻狗一个 都认
        m = re.match(r"^([Zz]\d{5,12})\s*(黑|白)?\s*(.*)$", raw)
        if not m:
            return
        sid = m.group(1).upper()
        kind = m.group(2)
        note = (m.group(3) or "").strip()
        if kind:
            by = getattr(event, "unified_msg_origin", "") or ""
            rec = self._set_mark(sid, kind, note, by)
            yield await self._say(event, await self._describe_mark(sid, rec))
            return
        # 没带黑/白 → 当查商家处理
            by = getattr(event, "unified_msg_origin", "") or ""
            rec = self._set_mark(sid, parts[1], note, by)
            yield await self._say(event, await self._describe_mark(sid, rec))
            return
        # 单纯 Z 号 → 查商家
        # 和「查ID」走同一条路：头像 / 游戏UID / 货架图标格全都要有。
        # （以前这里自己抄了一份文字实现，所以直接发 Z 号出来的还是旧样式）
        res = await self._lookup_id_result(event, sid)
        if res is not None:
            yield res


    # ---------------------------------------------------------------- 管理员判定
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
        for fn in ("get_sender_id", "get_sender"):
            try:
                f = getattr(event, fn, None)
                if callable(f):
                    v = f()
                    if v:
                        return str(v)
            except Exception:
                pass
        try:
            obj = getattr(event, "message_obj", None)
            sender = getattr(obj, "sender", None)
            if sender is not None:
                # 对象属性
                for attr in ("user_id", "uin", "id", "qq"):
                    v = getattr(sender, attr, None)
                    if v:
                        return str(v)
                # dict 形式
                if isinstance(sender, dict):
                    for k in ("user_id", "uin", "id", "qq"):
                        v = sender.get(k)
                        if v:
                            return str(v)
        except Exception:
            pass
        # 最后从 umo 里抠
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        m = re.search(r"(?:FriendMessage|PrivateMessage|GroupMessage)[:_\s]*(\d{5,15})",
                      umo)
        if m:
            return m.group(1)
        parts = [p for p in re.split(r"[:\s_]+", umo) if p.isdigit()]
        return parts[-1] if parts else ""

    def _is_admin(self, event) -> bool:
        admins = self._admin_ids()
        sid = self._sender_id(event)
        if not admins:
            # 没配管理员：只允许私聊里查（相对安全）
            return self._is_private(event)
        return sid in admins

    #: 市场控制台管理员 QQ 的默认值（只有他们能看换绑 / 改名记录）
    DEFAULT_CONSOLE_ADMIN_QQ = "1877811952"
    #: 控制台管理员名单缓存（秒）
    CONSOLE_ADMIN_TTL = 600

    async def _console_admin_ids(self) -> List[str]:
        """市场控制台管理员 QQ 名单。

        取值顺序：市场控制台配置（`admin_qq` / `console_admin_qq` / `管理员QQ:` 任意一行）
        → 插件配置 `console_admin_qq` → 内置默认值。多个用逗号分隔。
        """
        cached = getattr(self, "_console_admin_cache", None)
        if cached and time.time() - cached[0] < self.CONSOLE_ADMIN_TTL:
            return cached[1]
        ids: List[str] = []
        try:
            data = await self._api("/api/config")
            text = str((data or {}).get("text") or "") if isinstance(data, dict) else ""
            for line in text.splitlines():
                m = re.match(r"^\s*(admin_qq|console_admin_qq|管理员\s*qq)\s*[:：]\s*(.+?)\s*$",
                             line, re.I)
                if m:
                    ids.extend(x for x in re.split(r"[,\[\]\"'\s]+", m.group(2)) if x)
        except Exception:
            pass
        base = str(self._cfg("console_admin_qq", self.DEFAULT_CONSOLE_ADMIN_QQ) or "")
        for ch in ("，", "；", ";", " ", "\n", "\t"):
            base = base.replace(ch, ",")
        ids.extend(base.split(","))
        out: List[str] = []
        for x in ids:
            x = str(x).strip().strip("\"'“”").lstrip("+")
            if x.isdigit() and x not in out:
                out.append(x)
        if not out:
            out = [self.DEFAULT_CONSOLE_ADMIN_QQ]
        try:
            self._console_admin_cache = (time.time(), out)
        except Exception:
            pass
        return out

    # ---------------------------------------------------------------- 功能权限（控制台配置）
    #: 权限配置缓存：(时间戳, dict)
    _PERM_CACHE: tuple = (0, {})

    async def _perm_config(self, force: bool = False) -> Dict[str, Any]:
        """从控制台读 permissions 配置（缓存 60 秒）。"""
        now = time.time()
        ts, cache = self._PERM_CACHE
        if not force and cache and now - ts < 20:
            return cache
        try:
            data = await self._api("/api/permissions")
            perm = (data or {}).get("permissions") if isinstance(data, dict) else None
            if isinstance(perm, dict):
                self._PERM_CACHE = (now, perm)
                return perm
        except Exception as e:
            logger.warning(f"[权限] 读控制台权限失败: {type(e).__name__}: {e}")
        return cache or {}

    async def _perm_allowed(self, event: Any, feature: str) -> bool:
        """这个会话/这个人能不能用 feature。

        控制台 permissions.enabled=False → 全放开
        QQ 在 admins → 放行
        群号在 allowed_groups / QQ 在 allowed_users → 放行
        feature 不在 restricted → 放行
        """
        perm = await self._perm_config()
        if not perm or not bool(perm.get("enabled")):
            return True
        restricted = [str(x) for x in (perm.get("restricted") or [])]
        if feature not in restricted:
            return True
        # 管理员
        admins = {str(x).strip() for x in (perm.get("admins") or []) if str(x).strip()}
        qq = self._sender_id(event)
        if qq and qq in admins:
            return True
        # 群号
        gid = self._group_id(event)
        if gid and gid in {str(x).strip() for x in (perm.get("allowed_groups") or [])}:
            return True
        # QQ
        if qq and qq in {str(x).strip() for x in (perm.get("allowed_users") or [])}:
            return True
        return False

    @staticmethod
    def _group_id(event: Any) -> str:
        """取群号；私聊返回空。"""
        obj = getattr(event, "message_obj", None)
        gid = getattr(obj, "group_id", None) if obj is not None else None
        if gid:
            return str(gid)
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        m = re.search(r"GroupMessage[:\s]+(\d{5,15})", umo)
        if m:
            return m.group(1)
        m = re.search(r"group[:\s_]+(\d{5,15})", umo, re.I)
        return m.group(1) if m else ""

    #: 受保护名单缓存：(时间戳, {"items": set, "notify": bool})
    _PROTECT_CACHE: tuple = (0, None)

    async def _protected_set(self) -> tuple:
        """从控制台拉受保护名单（ZID / UID / 昵称），缓存 60 秒。"""
        now = time.time()
        ts, cache = self._PROTECT_CACHE
        if cache is not None and now - ts < 20:
            return cache
        data = None
        try:
            data = await self._api("/api/permissions/protected")
        except Exception as e:
            logger.warning(f"[保护] 拉名单失败: {type(e).__name__}: {e}")
        if isinstance(data, dict) and data.get("ok"):
            items = {str(x).strip().upper() for x in (data.get("protected") or [])
                     if str(x).strip()}
            cache = {"items": items, "notify": bool(data.get("notify", True))}
        else:
            cache = cache or {"items": set(), "notify": True}
        self._PROTECT_CACHE = (now, cache)
        return cache

    async def _protected_hit(self, *queries: str) -> Optional[str]:
        """query 里有没有命中受保护名单？命中返回命中的那一项。"""
        cache = await self._protected_set()
        items = cache.get("items") or set()
        if not items:
            return None
        for q in queries:
            s = str(q or "").strip().upper()
            if not s:
                continue
            if s in items:
                return s
        return None

    async def _protected_notify(self, event: Any, query: str, hit: str) -> None:
        """给管理员发一条「谁查了受保护对象」。"""
        cache = await self._protected_set()
        if not cache.get("notify", True):
            return
        who = self._sender_id(event) or "未知"
        gid = self._group_id(event)
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        where = f"群 {gid}" if gid else "私聊"
        text = (
            "🔔 有人查询了受保护对象\n"
            f"查询内容：{query}\n"
            f"命中名单：{hit}\n"
            f"查询者：{who}（{where}）\n"
            f"会话：{umo}\n"
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        text = _strip_emoji(text)
        # 发给所有管理员私聊
        sent = 0
        for admin in self._admin_ids():
            for chain in self._chains(text):
                for prefix in self._platform_prefixes():
                    try:
                        ret = await self.context.send_message(
                            f"{prefix}:FriendMessage:{admin}", chain)
                        if ret is not False:
                            sent += 1
                            break
                    except Exception:
                        continue
                if sent:
                    break
        if sent:
            logger.info(f"[保护] 已通知管理员 {sent} 人：{query}")
        else:
            logger.warning(f"[保护] 通知管理员失败（query={query} who={who}）")

    def _platform_prefixes(self) -> List[str]:
        """拼 umo 用的平台前缀候选。"""
        out: List[str] = []
        try:
            p = self._platform()
            if p:
                out.append(p)
        except Exception:
            pass
        for p in ("aiocqhttp", "qq_official", "gewechat"):
            if p not in out:
                out.append(p)
        return out

    async def _perm_gate(self, event: Any, feature: str) -> bool:
        """权限拦一下：不许就回一句提示并返回 False。"""
        if await self._perm_allowed(event, feature):
            return True
        perm = await self._perm_config()
        admins = "、".join(str(x) for x in (perm.get("admins") or [])[:3]) or "管理员"
        await self._say(
            event,
            f"🔒 这个功能需要授权\n"
            f"「{feature}」只对指定的群/QQ 开放，或者找 {admins} 开通。\n"
            f"（纯图鉴查询不受限制：查询 火花 / 查蛋 火花 照常用）")
        return False

    async def _is_console_admin(self, event) -> bool:
        """当前发消息的人是不是市场控制台管理员。"""
        ids = await self._console_admin_ids()
        sid = self._sender_id(event)
        return bool(sid) and sid in ids


    # ---------------------------------------------------------------- 查ID
    #: 查ID / 查询ID：命令词大小写随便，中间空格随便，后面有没有空格都行
    _ID_CMD_RE = re.compile(r"^\s*[/#]?\s*(?:查\s*[Ii][Dd]|查询\s*[Ii][Dd])\s*(.*)$")
    #: 「标 Z10026057 黑 备注」宽松匹配（不带斜线也能用）
    _MARK_LOOSE_RE = re.compile(
        r"^\s*标\s*([Zz]\d{5,12})\s*(黑|白)\s*(.*)$")

    def _id_arg_from_text(self, raw: str) -> str:
        """从原始消息里抠参数：查IDZ100 / 查 id z100 / /查询ID xxx 都认。"""
        s = str(raw or "").strip()
        prefix = str(self._cfg("wake_prefix", "/") or "/")
        if prefix and s.startswith(prefix):
            s = s[len(prefix):].strip()
        if s.startswith("#"):
            s = s.lstrip("#").strip()
        m = self._ID_CMD_RE.match(s)
        return m.group(1).strip() if m else ""

    def _id_dedupe(self, event: Any) -> bool:
        """命令入口和宽松正则入口可能都命中同一条消息，保证只回一次。"""
        if getattr(event, "_lk_lookup_id_done", False):
            return False
        try:
            event._lk_lookup_id_done = True
        except Exception:
            pass
        return True

    async def _lookup_id_result(self, event: Any, q: str):
        """查ID 的核心：ID 大小写归一 + 纯数字补 Z 重试。"""
        q = str(q or "").strip()

        # ① 先查受保护名单：**必须在权限检查之前**
        #    否则没权限的人查了受保护对象会直接返回，管理员收不到通知。
        if q:
            _prot = await self._protected_hit(q)
            if _prot:
                await self._protected_notify(event, q, _prot)
                if not await self._perm_allowed(event, "lookup_id"):
                    # 连查ID 权限都没有：只回权限提示（不给任何信息）
                    return await self._say(
                        event,
                        "🔒 这个功能需要授权\n"
                        "「lookup_id」只对指定的群/QQ 开放，或者找管理员开通。\n"
                        "（纯图鉴查询不受限制：查询 火花 / 查蛋 火花 照常用）")
                return await self._say(
                    event, "这个商家信息不对外提供。\n（有疑问请联系管理员）")

        # ② 再走权限
        if not await self._perm_gate(event, "lookup_id"):
            return
        if not q:
            return await self._say(event, 
                "格式：查ID Z10014514\n也可以用昵称：查ID 咕咕\n（大小写、空格都随意）")
        if self._market_paused():
            return await self._say(event, self.PAUSED_HINT)

        # 助手ID 大小写都行，统一成大写再查
        qid = q.upper() if re.fullmatch(r"[Zz]\d{5,12}", q) else q
        # 60 秒短缓存：同名反复查（比如翻列表核对）就不用每次等控制台 0.5 秒
        _ck = "m:" + qid
        _hit = _MERCHANT_CACHE.get(_ck)
        if _hit and time.time() - _hit[0] < 60:
            data = _hit[1]
        else:
            data = await self._api("/api/merchant?q=" + urllib.parse.quote(qid))
            if isinstance(data, dict) and data.get("ok"):
                _MERCHANT_CACHE[_ck] = (time.time(), data)
                while len(_MERCHANT_CACHE) > 64:
                    _MERCHANT_CACHE.pop(next(iter(_MERCHANT_CACHE)))
        note = ""
        ok = isinstance(data, dict) and data.get("ok")
        # 实时接口不认游戏ID（要"先建索引"）→ 退一步用索引搜索（游戏ID / 昵称都能搜）
        if not ok:
            try:
                row = await self._seller_info(qid)
            except Exception:
                row = None
            sid = str((row or {}).get("seller") or "").strip()
            if sid:
                alt = await self._api("/api/merchant?q=" + urllib.parse.quote(sid))
                if isinstance(alt, dict) and alt.get("ok"):
                    data = alt
                    note = f"（按{row.get('match') or '索引'}找到 {sid}）"
                    ok = True
        # 纯数字查不到就补个 Z 再试：玩家常把助手ID 前面的 Z 忘了写
        if not ok and re.fullmatch(r"\d{5,12}", qid):
            alt = await self._api("/api/merchant?q=" + urllib.parse.quote("Z" + qid))
            if isinstance(alt, dict) and alt.get("ok"):
                data = alt
                note = f"（你发的是纯数字，按助手ID Z{qid} 查到了）"

        if isinstance(data, dict) and data.get("ok"):
            mark = self._marks().get((data.get("seller") or "").upper())
            # 顺手查这个商家在花种市场挂的单
            _sid = str(data.get("seller") or "").upper()
            _flowers: List[Dict[str, Any]] = []
            if _sid:
                try:
                    _snap = await self._flower_snapshot()
                    # seller 是昵称，seller_id 才是 ZID
                    _flowers = [b for b in _snap
                                if str(b.get("seller_id") or "").upper() == _sid]
                    _flowers.sort(key=lambda b: (b.get("price") is None,
                                                 b.get("price") or 0))
                except Exception as e:
                    logger.warning(f"[查ID] 拉花种失败: {type(e).__name__}: {e}")
            # 预拉花种的官方色卡图（异步，卡片里要贴）
            _flower_imgs: Dict[Any, bytes] = {}
            for _b in _flowers[:8]:
                try:
                    _im = await self._chroma_card_bytes(_b.get("particle_id"),
                                                        _b.get("color_id"))
                    if _im:
                        _flower_imgs[id(_b)] = _im
                except Exception:
                    pass
            out = self._merchant_block(data, mark, _flowers)
            text = out + ("\n" + note if note else "")
            _cands = self._sort_candidates(data)
            if len(_cands) > 1:
                others = [c for c in _cands[:8]
                          if str(c.get("seller") or "") != (data.get("seller") or "")]
                if others:
                    text += "\n———— 同名候选（共 %d 个，有在售的排前面）————" % len(_cands)
                    for c in others:
                        text += f"\n· {self._cand_text(c)}"
            _chs = [c for c in (data.get("uid_changes") or []) if isinstance(c, dict)][:6]
            if _chs:
                # uid_changes 只有 game / seller / name / first / last，没有新名字：
                # 有改名条目时补查一次记录，把「旧名 → 新名」对上
                _nm = {}
                if any(c.get("reason") == "改名" for c in _chs):
                    try:
                        _rc = await self._api(
                            "/api/roles/changes?limit=50&reason="
                            + urllib.parse.quote("改名"))
                        for it in ((_rc or {}).get("items") or []):
                            if isinstance(it, dict) and it.get("game"):
                                _nm[str(it.get("game"))] = (
                                    str(it.get("old_role") or "—"),
                                    str(it.get("now_role") or "—"))
                    except Exception:
                        pass
                text += "\n———— 绑定变更 ​————"
                for ch in _chs:
                    g = str(ch.get("game") or "")
                    tm = self._fmt_ts_hm(ch.get("last") or ch.get("at"))
                    tail = f"　{tm}" if tm else ""
                    if ch.get("reason") == "换绑":
                        text += (f"\n· 换绑 {g} → "
                                 f"{ch.get('now_seller') or ch.get('seller') or '—'}{tail}")
                    else:
                        old, now = _nm.get(g, (str(ch.get("name") or "—"), "—"))
                        text += f"\n· 改名 {g} 「{old}」→「{now}」{tail}"
            png = None
            if self._image_reply_on():
                try:
                    avatar = await self._avatar_path(
                        str(data.get("seller") or ""),
                        (data.get("merchant") or {}).get("avatar") or "")
                    png = await asyncio.to_thread(
                        self._merchant_card, data, mark, avatar,
                        _flowers, _flower_imgs)
                    if png is None:
                        # 渲染器说"画不了"（Pillow / card.py 缺失），得让人知道
                        text += "\n（图片没出来：卡片渲染器不可用，发 /诊断 看详情）"
                except Exception as e:
                    logger.warning(f"[出图] 商家卡失败: {type(e).__name__}: {e}")
                    # 别把错误吞掉 —— 光看文字根本不知道为什么没图
                    text += f"\n（图片没出来：{type(e).__name__}: {str(e)[:60]}）"
            return await self._reply(event, text, png)
        if isinstance(data, dict) and data.get("blocked_by"):
            return await self._say(event, 
                f"查不到「{q}」：这个商家屏蔽了你的账号（{'、'.join(data['blocked_by'])}）。\n"
                f"换个没被屏蔽的账号再查；挂单本身在市集还能看到。")
        err = str((data or {}).get("error") or "") if isinstance(data, dict) else ""
        if err:
            # 「还没建索引」其实是没查到，不是出错，措辞跟着走
            if "索引" in err:
                return await self._say(event, 
                    f"没查到商家「{q}」。\n{err}\n如果他是精灵名，用：查询 {q}")
            return await self._say(event, f"查不到「{q}」：{err[:60]}")

        mark = self._marks().get(qid.upper())
        if mark:
            return await self._say(event, await self._describe_mark(qid.upper(), mark))
        return await self._say(event, f"没查到商家「{q}」。\n如果他是精灵名，用：查询 {q}")

    @filter.command("查ID")
    async def cmd_lookup_id(self, event: AstrMessageEvent):
        """查ID Z10014514 / 查ID z10014514 / 查ID 咕咕"""
        if not self._id_dedupe(event):
            return
        yield await self._lookup_id_result(
            event, self._id_arg_from_text(getattr(event, "message_str", "")))

    @filter.command("查询ID")
    async def cmd_lookup_id_alias(self, event: AstrMessageEvent):
        """查ID 的别名，行为完全一样"""
        if not self._id_dedupe(event):
            return
        yield await self._lookup_id_result(
            event, self._id_arg_from_text(getattr(event, "message_str", "")))

    @filter.regex(r"^\s*[/#]?\s*(?:查\s*[Ii][Dd]|查询\s*[Ii][Dd])\s*.*$")
    async def on_lookup_id_loose(self, event: AstrMessageEvent):
        """兜底入口：查id / 查 ID / 查IDZ100 这些 filter.command 派发不到的写法。"""
        if not self._id_dedupe(event):
            return
        yield await self._lookup_id_result(
            event, self._id_arg_from_text(getattr(event, "message_str", "")))

    # ---------------------------------------------------------------- 精灵查询
    #: 命令词：查询 / 查蛋，中间空格随便。「查ID / 查询ID」不抢。
    _SP_CMD_RE = re.compile(r"^\s*(?:查\s*询|查\s*蛋)(?!\s*[Ii][Dd])(.*)$")
    #: 尾巴上的「最近 / 历史」→ 改成查成交明细
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

    #: 官方精灵表（pets.json）：名字 → 记录（赛季 / 图鉴号 / 阶段 / 异色 / 同乘 / 蛋组 / 系别）
    _PETS_TABLE: Optional[Dict[str, Dict[str, Any]]] = None

    @classmethod
    def _pets_table(cls) -> Dict[str, Dict[str, Any]]:
        if cls._PETS_TABLE is not None:
            return cls._PETS_TABLE
        out: Dict[str, Dict[str, Any]] = {}
        try:
            fp = _PLUGIN_DIR / "pets.json"
            if fp.is_file():
                d = json.loads(fp.read_text(encoding="utf-8-sig"))
                for r in (d.get("pets") or []):
                    nm = str(r.get("name") or "")
                    if not nm:
                        continue
                    cur = out.get(nm)
                    if cur is None:
                        out[nm] = r
                        continue
                    # 同名多条：优先「有异色」→ 再优先「正式赛季(≥1)」→ 再优先有蛋组 → 最后 pet_id 大
                    # （补录的常驻记录 pet_id 是 1300017x，比正式记录大，不能只按 id 比）
                    def _rank(x):
                        try:
                            s = int(x.get("season") or 0)
                        except (TypeError, ValueError):
                            s = 0
                        return (1 if x.get("has_shiny") else 0,
                                1 if s >= 1 else 0,
                                1 if (x.get("groups") or []) else 0,
                                int(x.get("pet_id") or 0))
                    if _rank(r) > _rank(cur):
                        out[nm] = r
        except Exception as e:
            logger.warning(f"[蛋组] pets.json 读取失败: {type(e).__name__}: {e}")
        cls._PETS_TABLE = out
        return out

    def _group_rows(self, group: str) -> List[Dict[str, Any]]:
        """某个蛋组的精灵 → 网格图的行（跟异色名单同一套结构，卡片画法完全一样）。"""
        heads = self._group_heads(group)
        pets = self._pets_table()
        sp = self.ranges.get("species") or {}
        out: List[Dict[str, Any]] = []
        for nm, dex in heads.items():
            info = pets.get(nm) or {}
            rec = sp.get(nm) or {}
            # 常驻 / 奇遇 以官方异色记录本为准（有记录就用记录的赛季，没有才退回官方表的 season_id）
            book_sid = str((self._book_by_name().get(nm) or {}).get("season") or "")
            if book_sid:
                sid_txt = "S0" if book_sid in ("none", "S0") else book_sid
            else:
                try:
                    sid = int(info.get("season") or 0)
                except (TypeError, ValueError):
                    sid = 0
                sid_txt = f"S{sid}" if sid >= 1 else "S0"
            out.append({
                "season": sid_txt,
                "name": nm,
                "group": group,
                "groups": [g for g in (info.get("groups") or []) if g] or [group],
                "marks": (["异色", "异色蛋"] if info.get("has_shiny")
                          else []),
                "leader": int(info.get("stage") or 0) >= 4,
                "ride": info.get("ride"),
                "no": str(info.get("no") or dex or ""),
                "stage": int(info.get("stage") or 0),
                "egg": str(rec.get("蛋名") or f"{nm}的蛋"),
            })
        return out

    async def _group_reply(self, event: Any, group: str):
        """查蛋组：**一张网格图装下这个组的全部精灵**，出不了图就发文字清单。"""
        text = self._group_reply_text(group)
        if not group or not (self._grid_enabled() and self._image_reply_on()
                             and _grid() is not None):
            return await self._say(event, text)
        cards = self._group_cards(group)
        if not cards:
            return await self._say(event, text)
        pngs, _pages, _want, total = await self._paginate_pngs(
            header=f"蛋组 · {group} · {len(cards)} 只",
            cards_res=cards, cards_enc=[], per=len(cards), all_pages=True)
        if not pngs:
            return await self._say(event, text)
        r = self._result_images(event, pngs)          # 不发「蛋组 · xx｜共 N 只」这行字
        if r is not None:
            return r
        return event.plain_result(text)

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
        _t0 = time.perf_counter()
        q = str(arg or "").strip()
        if not q:
            return await self._say(
                event,
                "格式：查询 火花 / 查蛋 火花\n"
                "· 加空格：带不带斜线都行（查询 火花、/查询 火花）\n"
                "· 不加空格：前面最好带斜线（/查询火花）\n"
                "· 查成交：查询 火花 最近（历史 也行）\n"
                "· 查蛋组：查询 拟人组 / 查询 拟人 / 查询 海洋")
        grp = self._match_group(q)
        if grp is not None:
            return await self._group_reply(event, grp)
        name, mode = self._sp_split_mode(q)
        hw = self._parse_hw(q) if mode == "info" else None
        rec = None if hw else self._find_species(name if mode == "recent" else q)
        if risky and mode == "info" and not hw and not rec:
            return None                     # 风险写法又认不出来 → 不吭声
        if mode == "recent":
            return await self._recent_reply(event, name)
        if hw:
            return await self._identify_reply(event, hw)
        if not rec:
            return await self._say(
                event,
                f"「{q}」暂未收录。\n"
                f"（传说精灵、活动限定这些官方图鉴没公开的品种查不到；"
                f"知道身高体重就直接发，比如 34/3.617 或 0.34 3.617）")
        _with_market = await self._perm_allowed(event, "market")
        text, png = await self._species_payload(rec, q,
                                                with_market=_with_market)
        return await self._reply(event, text, png, t0=_t0)

    async def _recent_reply(self, event: Any, name: str):
        """最近成交：走控制台 /api/market/recent（底层是 getPrivateMarketTrend）。"""
        if not await self._perm_gate(event, "market"):
            return
        q = str(name or "").strip()
        if not q:
            return await self._say(event, "格式：查询 火花 最近")
        if self._market_paused():
            return await self._say(event, self.PAUSED_HINT)
        try:
            data = await self._api("/api/market/recent?q=" + urllib.parse.quote(q))
        except Exception as e:
            return await self._say(event,
                                   f"查成交记录失败：{type(e).__name__}: {e}")
        if not isinstance(data, dict) or not data.get("ok"):
            err = str((data or {}).get("error") or "") if isinstance(data, dict) else ""
            return await self._say(
                event,
                f"没查到「{q}」的成交记录。"
                + (f"\n{err[:90]}" if err else "\n换个名字试试：查询 治愈兔 最近"))
        lines2 = [str(x) for x in (data.get("lines") or []) if str(x).strip()]
        icons = None
        c = _card()
        if c is not None and c.available():
            p = c.icon_path("egg", q)
            icons = [p] if p else None
        return await self._say(event, "\n".join(lines2) or f"{q}：暂无成交记录", icons)

    # ---------------------------------------------------------------- 自诊断
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
        # 关键是这条：真跑一遍「查ID」那条卡片链路
        try:
            _d = await self._api("/api/merchant?q=Z10010025")
            if isinstance(_d, dict) and _d.get("ok"):
                try:
                    _p = await asyncio.to_thread(self._merchant_card, _d, None, None)
                    lines.append(f"商家卡渲染：{'OK %d 字节' % len(_p) if _p else '返回 None（渲染器不可用）'}")
                except Exception as e:
                    lines.append(f"商家卡渲染：抛异常 {type(e).__name__}: {str(e)[:80]}")
            else:
                _e = str((_d or {}).get("error") or "")[:60] if isinstance(_d, dict) else "无返回"
                lines.append(f"取商家样本：失败（{_e}）")
        except Exception as e:
            lines.append(f"取商家样本：抛异常 {type(e).__name__}: {str(e)[:60]}")
        # 诊断结果**故意发文字**：出了问题才好复制，图片链路坏了也能看到
        yield event.plain_result("\n".join(lines))

    # ---------------------------------------------------------------- UID 查角色
    #: UID 命令词：UID / 查UID / 游戏ID，后面跟纯数字
    _UID_CMD_RE = re.compile(r"^\s*(?:查\s*)?(?:[Uu][Ii][Dd]|游戏[Ii][Dd])\s*(\d{5,15})\s*$")

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

    # ---------------------------------------------------------------- 改绑 / 改名记录
    @filter.command("改绑记录")
    async def cmd_rebind(self, event: AstrMessageEvent):
        """改绑记录 [N] —— 谁把绑定的游戏角色换到别的助手了（仅控制台管理员）"""
        yield await self._roles_changes(event, "换绑")

    @filter.command("改名记录")
    async def cmd_rename_hist(self, event: AstrMessageEvent):
        """改名记录 [N] —— 绑定过的游戏角色改过名的（仅控制台管理员）"""
        yield await self._roles_changes(event, "改名")

    async def _roles_changes(self, event: Any, reason: str):
        if self._market_paused():
            return event.plain_result(self.PAUSED_HINT)
        # 只给市场控制台管理员看（号码在配置 / 控制台里配）
        if not await self._is_console_admin(event):
            return event.plain_result("暂无控制台管理员权限")
        arg = self._cmd_args(event)
        limit = 15
        if str(arg).isdigit():
            limit = max(1, min(50, int(arg)))
        data = await self._api(
            f"/api/roles/changes?limit={limit}"
            f"&reason={urllib.parse.quote(reason)}")
        if not isinstance(data, dict) or not data.get("ok"):
            err = str((data or {}).get("error") or "") if isinstance(data, dict) else ""
            return event.plain_result(f"取{reason}记录失败：{err or '控制台没返回'}")
        items = [x for x in (data.get("items") or []) if isinstance(x, dict)]
        if not items:
            return event.plain_result(
                f"还没有{reason}记录。\n"
                f"（控制台每扫一次 ID 库就会比对绑定关系，发现变动才记一条）")
        rows: List[tuple] = []
        for x in items[:limit]:
            gid = str(x.get("game") or "")
            at = self._fmt_ts_hm(x.get("at"))
            if reason == "换绑":
                rows.append((gid, f"{x.get('old_seller') or '—'} → "
                                   f"{x.get('now_seller') or '—'}　{at}"))
            else:
                # 改名：老角色名 → 新角色名（顺手带上卖家和平台昵称，方便认人）
                old_r = str(x.get("old_role") or "—")
                now_r = str(x.get("now_role") or "—")
                seller = str(x.get("old_seller") or x.get("now_seller") or "")
                nick = str(x.get("now_nick") or x.get("old_nick") or "")
                extra = "　" + " ".join(y for y in (seller, nick) if y) if (seller or nick) else ""
                rows.append((gid, f"{old_r} → {now_r}{extra}　{at}"))
        title = f"{reason}记录（共 {data.get('count') or len(items)} 条）"
        png = None
        if self._image_reply_on():
            try:
                c = _card()
                if c is not None and c.available():
                    png = await asyncio.to_thread(
                        c.render_card, title=title, rows=rows,
                        **self._card_kwargs())
            except Exception as e:
                logger.warning(f"[出图] {reason}记录失败: {type(e).__name__}: {e}")
        text = "\n".join(f"{k}：{v}" for k, v in rows)
        return await self._reply(event, f"{title}\n{text}", png)

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
    @filter.command("蛋推送绑定")
    async def cmd_bind(self, event: AstrMessageEvent):
        if not await self._perm_gate(event, "push"):
            return
        """在哪个会话发这条指令，就把推送目标绑定到哪个会话（推荐）。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            yield await self._say(event, "拿不到当前会话标识，请在私聊/群里重新发送一次")
            return
        umos = self.state.get("umos") or []
        if umo not in umos:
            umos.append(umo)
        self.state["umos"] = umos[-10:]
        _save_state(self.state)
        yield await self._say(event, 
            f"✅ 已绑定推送目标\n{umo}\n"
            f"（共 {len(self.state['umos'])} 个目标）\n"
            f"⚠️ 购买成功通知默认只私聊管理员，不跟着这个绑定走；\n"
            f"想让购买成功也发到这里，把配置 buy_notify_target 填 bind"
        )

    @filter.command("蛋推送解绑")
    async def cmd_unbind(self, event: AstrMessageEvent):
        if not await self._perm_gate(event, "push"):
            return
        umo = getattr(event, "unified_msg_origin", "") or ""
        umos = [u for u in (self.state.get("umos") or []) if u != umo]
        self.state["umos"] = umos
        _save_state(self.state)
        yield await self._say(event, f"已解绑当前会话，剩余 {len(umos)} 个目标")

    @filter.command("蛋推送测试")
    async def cmd_test(self, event: AstrMessageEvent):
        if not await self._perm_gate(event, "push"):
            return
        """发一条样例消息，确认推送链路。"""
        if self._market_paused():
            yield await self._say(event, self.PAUSED_HINT)
            return
        sample_guess = {"top": [{"name": "电咩咩", "pct": 100.0, "tag": ""}], "special": []}
        sample = {"id": "test", "name": "神奇蛋", "points": 12, "seller": "Z10031963",
                  "courier": "快递员D1", "code": "EGG-XXXX-XXXX",
                  "claimed_at": int(time.time() * 1000),
                  "height_cm": 34, "weight_g": 3617, "state": "available"}
        n = await self._send(self._render(sample, sample_guess))
        yield await self._say(event, f"已向 {n} 个目标发送测试消息" if n
                                 else "发送失败，看 AstrBot 日志里的 warn 行")

    @filter.command("蛋推送状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if not await self._perm_gate(event, "push"):
            return
        if self._market_paused():
            yield await self._say(event, self.PAUSED_HINT)
            return
        data = await self._api("/api/purchases")
        api_ok = bool(data and data.get("ok"))
        lines = [
            f"市集推送状态 · 版本 {VERSION}",     # ← 先确认装的到底是哪一版
            self._push_switch_text(),
            f"接口：{self.api_base}（{'正常' if api_ok else '读取失败'}）",
            f"平台：{self._platform()}",
            f"目标：{len(self._candidate_umos())} 个",
        ]
        for u in self._candidate_umos()[:5]:
            lines.append(f"  · {u}")
        lines += [
            f"间隔：{self.interval:g}s · 轮询 {self.stats['polls']} 次",
            f"已推送 {self.stats['sent']} · 忽略 {self.stats['skipped']} · 失败 {self.stats['errors']}",
            f"已记录 id：{len(self.state.get('seen') or [])} 个 · "
            f"已推挂单 {len(self.state.get('pushed_ids') or {})} 个",
            "",
            self._push_log_text(),
        ]
        if data and data.get("stale"):
            lines.append("⚠ 控制台用的是缓存数据（远端接口暂时不可用）")
        yield await self._say(event, "\n".join(lines))

    @filter.command("蛋推送重置")
    async def cmd_reset(self, event: AstrMessageEvent):
        if not await self._perm_gate(event, "push"):
            return
        # 只清「推送记录」，绑定关系和监控名单留着 ——
        # 以前这里整个 state 换掉，顺手把监控名单/黑名单也清了
        keep = {k: self.state.get(k) for k in
                ("umos", "watch", "watch_ops", "watch_deleted", "marks")}
        self.state = {"seen": [], "started": 0, "pushed_ids": {}, "pushed_sig": {},
                      "listings": {}, "flower_seen": []}
        self.state.update(keep)
        _hard_save_state(self.state)          # 这次要真清，不能跟磁盘合并
        yield await self._say(event, "已清空推送记录（绑定和监控名单保留），"
                                     "下一轮会把现有记录当新的处理")


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