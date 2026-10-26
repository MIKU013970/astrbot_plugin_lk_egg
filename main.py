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
import hashlib
import os
import re
import json
import shutil
import urllib.parse
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


_PLUGIN_DIR = Path(__file__).resolve().parent
_PLUGIN_NAME = "astrbot_plugin_lk_egg"
VERSION = "1.26.0"


def _data_dir() -> Path:
    """按平台规范：持久化数据放 data/plugin_data/<plugin_name>/"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        return Path(get_astrbot_data_path()) / "plugin_data" / _PLUGIN_NAME
    except Exception:
        # 兼容旧版 AstrBot / 独立测试
        return Path("data") / "plugin_data" / _PLUGIN_NAME


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


def _load_state() -> Dict[str, Any]:
    _migrate_old_state()
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"seen": [], "started": 0, "umos": [], "watch": [],
                "listings": {}, "marks": {}}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    except Exception as e:
        logger.warning(f"[市集推送] 保存状态失败: {e}")


@register(
    "astrbot_plugin_lk_egg",
    "MIKU013970 & DeepSeek",
    "市集购买成功推送 + 新上架监控 + 黑白名单查询",
    "1.26.0",
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
        guess = item.get("guess")
        if guess and guess.get("top"):
            return guess
        h, w = item.get("height_cm"), item.get("weight_g")
        if not h or not w:
            return None
        data = await self._api(f"/api/egg/identify?h={h}&w={w}")
        return (data or {}).get("guess")

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

    async def _send(self, text: str) -> int:
        umos = self._candidate_umos()
        if not umos:
            logger.warning("[市集推送] 没有推送目标：先在目标会话发一次 /蛋推送绑定，"
                           "或在配置里填 target_qq")
            return 0
        ok = 0
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
                logger.info(f"[市集推送] 已推送到 {umo}")
            else:
                logger.warning(f"[市集推送] 推送到 {umo} 失败: "
                               f"{type(last).__name__}: {last}")
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

    async def _loop(self) -> None:
        logger.info(f"[市集推送] 轮询 {self.api_base} 每 {self.interval:g}s"
                    f" · 平台 {self._platform()} · 目标 {len(self._candidate_umos())} 个")
        while True:
            self.stats["polls"] += 1
            self.stats["last_at"] = int(time.time())
            try:
                await self._poll_once(True)
                await self._poll_listings()
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
        if not bool(self._cfg("enabled", True)):
            logger.info("[市集推送] 配置 enabled=false，插件不启动")
            return
        self._task = asyncio.create_task(self._loop())
        self._code_task = asyncio.create_task(self._daily_code_loop())

    async def terminate(self) -> None:
        _wt = getattr(self, "_warm_task", None)
        if _wt:
            _wt.cancel()
            try:
                await _wt
            except (asyncio.CancelledError, BaseException):
                pass
            self._warm_task = None
        _ct = getattr(self, "_code_task", None)
        if _ct:
            _ct.cancel()
            try:
                await _ct
            except (asyncio.CancelledError, BaseException):
                pass
            self._code_task = None
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

    def _find_species(self, q: str) -> Optional[Dict[str, Any]]:
        """精灵名 / 蛋名 / 进化链上任意名字 / 裸名字 → 记录（含多形态）"""
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
        # 模糊
        for name, rec in sp.items():
            if q in name or name in q:
                return rec
        for nm, target in alias.items():
            if (q in nm or nm in q) and target in sp:
                return sp[target]
        return None

    # ---------------------------------------------------------------- 查蛋行情
    async def _market_quote_raw(self, name: str) -> Dict[str, Any]:
        """查蛋时附带的实时行情（大婉 / 小婉 / 大粗 / 小粗）。

        走控制台的 GET /api/market/quote，控制台那边负责多账号轮询 + 按蛋种缓存。
        拿不到（超时 / 控制台没起 / 接口报错）就返回空字典，绝不抛出去卡住查蛋。
        """
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

    async def _species_payload(self, rec: Dict[str, Any], query: str = ""):
        """图鉴正文 + 行情；返回 (文字版, 图片版或 None)。"""
        text = self._render_species(rec)
        lines: List[str] = []
        items: List[Dict[str, Any]] = []
        eggs = self._distinct_eggs(rec)
        try:
            for egg_name, _forms in (eggs or [(rec.get("精灵") or "", [])]):
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
                png = await asyncio.to_thread(self._species_card, rec, items, query)
            except Exception as e:
                logger.warning(f"[出图] 图鉴卡失败，退回文字: {type(e).__name__}: {e}")
        return text, png

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
                out.append(f"{prefix}{v.get('形态') or ''} · 区间 {v.get('区间数')}")
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
                out.append(f"小块头：过线 {v['小块头过线值']} · 极限 {v['小块头极限值']}")
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
        if len(variants) <= 1:
            boxes = [
                ("小块头",
                 f"过线 {v.get('小块头过线值') or '—'}  极限 {v.get('小块头极限值') or '—'}",
                 (78, 138, 186)),
                ("大块头",
                 f"过线 {v.get('大块头过线值') or '—'}  极限 {v.get('大块头极限值') or '—'}",
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
                           str(it.get("low", 0)),
                           str(it.get("listed", 0))) for it in items[:6]],
            }
        dex = v.get("图鉴编号") or rec.get("图鉴编号") or ""
        bins = v.get("区间数") if len(variants) == 1 else None
        tiles = None
        if len(eggs) > 1:
            # 多种蛋：每种蛋一格，写清蛋名和它对应的形态
            tiles = []
            for egg_name, eforms in eggs:
                cap = [egg_name, f"{len(eforms)} 种形态" if eforms else ""]
                tiles.append((c.icon_path("egg", egg_name, eforms[0] if eforms else ""),
                              [x for x in cap if x]))
        return c.render_card(
            title=str(rec.get("精灵") or ""),
            subtitle=(f"图鉴 {dex}" if dex else ""),
            tag=(f"区间 {bins}" if bins not in (None, "", 0) else ""),
            rows=rows, boxes=boxes, table=table, tiles=tiles,
            head_icons=self._spe_icons(rec, v), **self._card_kwargs())

    def _identify_card(self, h: float, w: float,
                       hits: Optional[List[Dict[str, Any]]] = None) -> Optional[bytes]:
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
            head_v = ((f"区间 {bins} · " if bins not in (None, "", 0) else "")
                      + str(self._size_of(h, w, x)))
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
            hs.append((c.icon_path("pet", nm, form0), f"{i}. {nm}", head_v, subs))
        return c.render_card(
            title="身高体重查蛋",
            subtitle=f"身高 {h:g} cm · 体重 {w / 1000:.3f} kg",
            rows=([("结果", "没有匹配的精灵蛋"),
                   ("", "可能是输入格式不对，或者这颗蛋不是图鉴里的品种")]
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
        """落到临时文件（协议端要按路径读），顺手清掉 1 小时前的旧图。"""
        import tempfile
        d = Path(tempfile.gettempdir()) / "lk_egg_cards"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            d = Path(tempfile.gettempdir())
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

    def _result_image(self, event: Any, png: bytes):
        """把 PNG 包成 AstrBot 的结果对象，几种写法挨个试。"""
        path = self._temp_png(png)
        fn = getattr(event, "image_result", None)
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

    async def _reply(self, event: Any, text: str, png: Optional[bytes]):
        """统一出口：能出图就出图，出不了就发文字。"""
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
        v = v if isinstance(v, dict) else {}
        pet = str(v.get("精灵") or rec.get("精灵") or "")
        egg = str(v.get("蛋名") or rec.get("蛋名") or pet)
        form0 = str(v.get("形态简称") or v.get("形态") or "")
        out = [c.icon_path("pet", pet, form0), c.icon_path("egg", egg)]
        # 异色
        shiny_pet = c.icon_path("shiny", pet, form0)
        if shiny_pet:
            out.append(shiny_pet)
            shiny_egg = None
            for en, _forms in self._distinct_eggs(rec):
                if "异色" in en:
                    shiny_egg = c.icon_path("egg", en, form0)
                    break
            if shiny_egg:
                out.append(shiny_egg)
        out = [x for x in out if x]
        return out or None

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
                       mark: Optional[Dict[str, Any]] = None) -> List[tuple]:
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

        # 上次采集 / 上次在助手上操作（数据变动倒推）
        _seen = data.get("last_seen")
        if _seen:
            rows.append(("上次采集", self._fmt_ts_hm(_seen)))
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
        return rows

    def _merchant_card(self, data: Dict[str, Any],
                       mark: Optional[Dict[str, Any]] = None,
                       avatar: Optional[str] = None) -> Optional[bytes]:
        """商家卡：头像 + 基本信息 + 货架按精灵分类的蛋图网格。"""
        c = _card()
        if c is None or not c.available():
            return None
        m = data.get("merchant") or {}
        seller = str(data.get("seller") or m.get("seller") or "—")
        nick = m.get("nickname") or m.get("displayName") or seller
        tiles = self._shelf_tiles(data.get("listings") or [])
        # 没设平台头像的商家（占大多数，都是"玩家xxxx""访客"这种默认号）
        # 用「神奇的蛋」的图兜底，卡片左边就不会空一块
        head = avatar or c.icon_path("egg", "神奇蛋")
        return c.render_card(title=str(nick), rows=self._merchant_rows(data, mark),
                             tiles=(tiles or None), tiles_per_row=4,
                             head_icons=([head] if head else None),
                             **self._card_kwargs())

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
        """身高体重查蛋的统一出口（识别卡也放到线程里画）。"""
        png = None
        if self._image_reply_on():
            try:
                png = await asyncio.to_thread(self._identify_card, hw[0], hw[1])
            except Exception as e:
                logger.warning(f"[出图] 识别卡失败: {type(e).__name__}: {e}")
        return await self._reply(event, self._render_identify(*hw), png)

    # ---------------------------------------------------------------- 新上架提醒
    def _watch(self) -> List[str]:
        ids = [str(x).strip().upper() for x in (self.state.get("watch") or []) if x]
        cfg_ids = [x.strip().upper() for x in str(self._cfg("watch_sellers", "") or "")
                   .replace("，", ",").split(",") if x.strip()]
        out = []
        for x in ids + cfg_ids:
            if x and x not in out:
                out.append(x)
        return out

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

    async def _poll_listings(self) -> int:
        """逐个监控商家拉实时货架，发现新挂单就推送。返回推送条数。"""
        sent = 0
        watch = self._watch()
        if not watch:
            return 0
        store = self.state.setdefault("listings", {})
        for sid in watch:
            data = await self._api(f"/api/merchants/listings?seller={sid}")
            if not data or not data.get("ok"):
                continue
            items = data.get("items") or []
            ids = [str(it.get("id")) for it in items if it.get("id")]
            prev = store.get(sid)
            if prev is None:
                # 第一次看见这个商家：只记下来，不推送（避免一开插件刷屏）
                store[sid] = ids
                continue
            fresh = [it for it in items
                     if str(it.get("id")) not in set(prev)]
            if fresh:
                text = self._render_listing(sid, data.get("nickname") or "",
                                            fresh, data.get("listed") or len(items))
                if await self._send(text):
                    sent += 1
                    self.stats["sent"] += 1
                    logger.info(f"[市集推送] {sid} 新上架 {len(fresh)} 件")
            store[sid] = ids
        _save_state(self.state)
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
        """'34/3.617'、'34 3.617'、'0.34 3.617' → (34.0, 3617.0)  单位：cm / 克

        分隔符用 / 空格 逗号 x 都行。
        身高小于 4 的一律按「米」算（游戏里显示的就是 0.32 这种），×100 转成厘米；
        图鉴里最矮的蛋是 4 cm，所以这个阈值不会误伤。
        体重带小数点或 <100 按 kg，否则按克。
        """
        import re as _re
        m = _re.match(r"^\s*(\d+(?:\.\d+)?)\s*[/\s,，、x×*]\s*(\d+(?:\.\d+)?)\s*$",
                      str(text or ""), _re.I)
        if not m:
            return None
        h = float(m.group(1))
        if h < 4:                      # 游戏里给的是米
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

    def _render_identify(self, h: float, w: float) -> str:
        hits = self._match_species(h, w)
        head = f"身高 {h:g} cm · 体重 {w / 1000:.3f} kg"
        if not hits:
            return (f"{head}\n没有匹配的精灵蛋。\n"
                    f"（可能是输入格式不对，或者这颗蛋不是图鉴里的品种）")
        lines = [head, f"可能孵出（{len(hits)} 种）："]
        for i, x in enumerate(hits, 1):
            size = self._size_of(h, w, x)
            bins = x.get("区间数")
            lines.append(f"{i}. {x['精灵']}"
                         + (f" · 区间 {bins}" if bins not in (None, "", 0) else "")
                         + f"{' · ' if bins not in (None, '', 0) else ' · '}{size}")
            lines.append(f"   蛋名 {x['蛋名']} · 蛋组 {x['蛋组']} · {x.get('公母') or '未知'}")
            lines.append(f"   身高 {x['身高区间']} · 体重 {x['体重区间']}")
        return "\n".join(lines)

    def _merchant_block(self, d: Dict[str, Any],
                        mark: Optional[Dict[str, Any]] = None) -> str:
        """名字 + 助手ID + 统计 + 货架（不显示角色/游戏UID）"""
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


    MENU = (
        "🥚 蛋神助手 · 可用指令\n"
        "—————— 查蛋 ——————\n"
        "查询 火花               区间数/蛋组/进化链/身高体重/过线值\n"
        "查蛋 火花               跟「查询」完全一样\n"
        "/查询火花               不加空格也行（前面带斜线）\n"
        "查询 火神               进化链上任意名字都行\n"
        "查询 鸭吉吉             多形态会列出每种规格\n"
        "查询 治愈兔 最近        最近成交：4 种牌子各 10 笔（历史 也行）\n"
        "0.32 2.12               身高体重查蛋（空格分隔，身高按米算）\n"
        "34/3.617                身高厘米 + 体重千克（斜杠也行）\n"
        "34 3617                 整数体重按克算\n"
        "（查蛋附带行情：大婉/小婉/大粗/小粗 的今日成交 · 中位价 · 最低积分 · 在售）\n"
        "—————— 查助手（商家）——————\n"
        "查ID Z10014514          昵称/上架/售出/好评差评/货架明细\n"
        "查ID z10014514          大小写都行\n"
        "查 id z10014514         命令词拆开、小写也行\n"
        "查IDZ10014514           命令词和 ID 粘一起也行\n"
        "查ID 10014514           纯数字会自动补 Z 再查一次\n"
        "查ID 134961358          游戏ID（已建索引的才有）\n"
        "查ID 咕咕               用平台昵称查\n"
        "查询ID xxx              和 查ID 完全一样\n"
        "—————— 名单 ——————\n"
        "标 Z10014514 黑 备注     加黑名单（备注可省略）\n"
        "标 Z10014514 白 备注     加白名单\n"
        "取消标 Z10014514         移出名单\n"
        "黑名单 / 白名单          列全部\n"
        "—————— 新上架提醒 ——————\n"
        "新上架 添加 Z10014514    开始监控这个商家\n"
        "新上架 删除 Z10014514    取消监控\n"
        "新上架 列表              看监控列表\n"
        "—————— 发码 ——————\n"
        "发码                     从最早的 10 个码里随机发 1 个\n"
        "发码 EGG-XXXX-XXXX       直接发这个码\n"
        "发码记录                 看最近发了哪些码（仅管理员）\n"
        "—————— 推送 ——————\n"
        "蛋推送绑定 / 解绑 / 测试 / 状态 / 重置\n"
        "—————— 其它 ——————\n"
        "UID 503671642           用游戏 UID 查等级/离线/家园/收集度\n"
        "改绑记录 / 改名记录      看绑定角色有没有被换绑 / 改名\n"
        "帮助 / 菜单              看这张卡片\n"
        "诊断                     管理员用：查图片链路哪里挂了\n"
        "回复默认都是图片         配置里关掉 reply_as_image 就发文字\n"
        "（提示：直接发精灵名字的功能已关闭，请用「查询 名字」）\n"
    )

    FORMAT_HINTS = {
        "查询": "格式：查询 火花（查蛋 也一样）\n进化链上任意名字都行：查询 火神 / 查询 焰火\n查最近成交：查询 火花 最近",
        "查蛋": "格式：查蛋 火花\n和「查询」完全一样，斜杠可加可不加",
        "发码": "格式：发码 EGG-XXXX-XXXX（先配置 code_groups 群号）",
        "输入兑换码": "格式：输入兑换码 EGG-XXXX-XXXX",
        "查ID": "格式：查ID Z10014514\n也可以用平台昵称：查ID 咕咕",
        "查询ID": "格式：查询ID Z10014514（和 查ID 一样）",
        "新上架": "格式：\n新上架 添加 Z10014514\n新上架 删除 Z10014514\n新上架 列表",
        "蛋监控": "格式：\n蛋监控 添加 Z10014514\n蛋监控 删除 Z10014514\n蛋监控 列表",
        "标": "格式：标 Z10014514 黑 备注写这里\n（第二项只能是「黑」或「白」）",
        "黑名单": "直接发「黑名单」就能列出全部\n加人：标 Z10014514 黑 备注",
        "白名单": "直接发「白名单」就能列出全部\n加人：标 Z10014514 白 备注",
    }

    # ---------------------------------------------------------------- 指令

    # ---------------------------------------------------------------- 指令：新上架监控
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
                     "新上架", "蛋监控", "帮助", "菜单", "蛋推送")

    def _looks_like_command(self, text: str) -> bool:
        t = str(text or "").lstrip()
        if self._ID_CMD_RE.match(t):
            return True
        return any(t.startswith(w) for w in self.COMMAND_WORDS)

    @filter.command("蛋监控")
    async def cmd_watch(self, event: AstrMessageEvent):
        """蛋监控 添加 Z10014514 / 删除 / 列表"""
        args = self._cmd_parts(event)
        q = self._cmd_args(event)
        sub = args[1] if len(args) > 1 else ""
        rest = args[2:]

        if sub in ("列表", "list", ""):
            watch = self._watch()
            if not watch:
                yield await self._say(event, "还没有监控任何商家。\n用：蛋监控 添加 Z10014514（换成真实助手ID）")
                return
            lines = [f"监控中的商家（{len(watch)} 个）"]
            for sid in watch[:20]:
                info = await self._seller_info(sid)
                nick = (info or {}).get("nickname") or "—"
                lines.append(f"· {nick}（{sid}）在售 {(info or {}).get('listed', '—')} 件")
            yield await self._say(event, "\n".join(lines))
            return

        if sub in ("添加", "add"):
            added = []
            for x in rest:
                x = x.strip().upper()
                if not x.startswith("Z"):
                    continue
                if x not in self.state["watch"]:
                    self.state["watch"].append(x)
                    added.append(x)
            _save_state(self.state)
            yield await self._say(event, 
                ("已添加监控：" + "、".join(added) + f"\n共 {len(self.state['watch'])} 个")
                if added else "没识别到要添加的助手ID（Z 开头）")
            return

        if sub in ("删除", "del", "remove"):
            removed, missing = [], []
            for x in rest:
                x = x.strip().upper()
                if x in self.state["watch"]:
                    self.state["watch"].remove(x)
                    self.state["listings"].pop(x, None)
                    removed.append(x)
                else:
                    missing.append(x)
            _save_state(self.state)
            yield await self._say(event, 
                ("已删除监控：" + "、".join(removed)) if removed else "列表里没有这些 ID")
            return

        yield await self._say(event, "用法：\n蛋监控 添加 Z10014514\n蛋监控 删除 Z10014514\n蛋监控 列表")

    # ---------------------------------------------------------------- 指令：黑白名单
    @filter.command("标")
    async def cmd_mark(self, event: AstrMessageEvent):
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

    @filter.command("取消标")
    async def cmd_unmark(self, event: AstrMessageEvent):
        args = self._cmd_parts(event)
        if len(args) < 2:
            yield await self._say(event, "用法：取消标 Z10014514")
            return
        sid = args[1].upper()
        yield await self._say(event, f"已取消 {sid} 的标记" if self._del_mark(sid)
                                 else f"{sid} 不在名单里")

    @filter.command("黑名单")
    async def cmd_blacklist(self, event: AstrMessageEvent):
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
        yield await self._say(event, self.MENU)

    @filter.command("新上架")
    async def cmd_watch_alias(self, event: AstrMessageEvent):
        """新上架 添加 Z10014514 / 删除 / 列表"""
        args = self._cmd_parts(event)
        sub = args[1] if len(args) > 1 else ""
        rest = args[2:]
        # 兼容「添加Z10014514」这种不带空格的写法
        for kw in ("添加", "删除", "列表"):
            if sub.startswith(kw) and sub != kw:
                tail = sub[len(kw):].strip()
                if tail:
                    rest = [tail] + rest
                sub = kw
                break

        if sub in ("列表", "list", ""):
            watch = self._watch()
            if not watch:
                yield await self._say(event, self.FORMAT_HINTS.get("新上架", "格式：\n新上架 添加 Z10014514\n新上架 删除 Z10014514\n新上架 列表"))
                return
            lines = [f"监控中的商家（{len(watch)} 个）"]
            for sid in watch[:20]:
                info = await self._seller_info(sid)
                lines.append(f"· {(info or {}).get('nickname') or '—'}（{sid}）"
                             f"在售 {(info or {}).get('listed', '—')} 件")
            yield await self._say(event, "\n".join(lines))
            return

        if sub in ("添加", "add"):
            added, dup, bad = [], [], []
            for x in rest:
                x = x.strip().upper()
                if not x.startswith("Z"):
                    bad.append(x)
                elif x in self._watch():
                    dup.append(x)
                else:
                    self.state["watch"].append(x)
                    added.append(x)
            _save_state(self.state)
            total = len(self.state["watch"])
            parts = []
            if added:
                parts.append("已添加监控：" + "、".join(added))
            if dup:
                parts.append("已经在监控里了：" + "、".join(dup))
            if bad:
                parts.append("不是助手ID（要 Z 开头）：" + "、".join(bad))
            if not rest:
                parts = ["没写要监控谁。\n格式：新上架 添加 Z10014514"]
            parts.append(f"当前共 {total} 个")
            yield await self._say(event, "\n".join(parts))
            return

        if sub in ("删除", "del", "remove"):
            removed, missing = [], []
            for x in rest:
                x = x.strip().upper()
                if x in self.state["watch"]:
                    self.state["watch"].remove(x)
                    self.state["listings"].pop(x, None)
                    removed.append(x)
                else:
                    missing.append(x)
            _save_state(self.state)
            parts = []
            if removed:
                parts.append("已删除监控：" + "、".join(removed))
            if missing:
                parts.append("本来就不在监控里：" + "、".join(missing))
            if not rest:
                parts = ["没写要删除谁。\n格式：新上架 删除 Z10014514"]
            parts.append(f"当前共 {len(self.state['watch'])} 个")
            yield await self._say(event, "\n".join(parts))
            return

        yield await self._say(event, self.FORMAT_HINTS.get("新上架", "格式：\n新上架 添加 Z10014514\n新上架 删除 Z10014514\n新上架 列表"))

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
            return                      # 不是精灵名就闭嘴
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

    @filter.regex(r"^\s*[Zz]\d{5,12}(\s+(黑|白)(\s+.*)?)?\s*$")
    async def on_bare_zid(self, event: AstrMessageEvent):
        """直接发 Z10014514 查商家；发「Z10014514 黑 备注」加名单。

        AstrBot 派发前会把 / 剥掉，所以 /Z10014514 也会走到这里。
        """
        raw = (getattr(event, "message_str", "") or "").strip()
        parts = raw.split()
        if not parts:
            return
        sid = parts[0].upper()
        if not sid.startswith("Z"):
            return
        # Z号 + 黑/白 [+备注] → 打标记
        if len(parts) >= 2 and parts[1] in ("黑", "白"):
            note = " ".join(parts[2:])
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

    # ---------------------------------------------------------------- 发码记录 / 每日自动发
    def _code_log(self) -> list:
        return self.state.setdefault("code_log", [])

    def _log_code(self, code: str, egg: str, group: str, way: str) -> None:
        log = self._code_log()
        log.append({"code": code, "egg": egg, "group": group,
                    "way": way, "at": int(time.time())})
        self.state["code_log"] = log[-500:]
        _save_state(self.state)

    def _sent_today(self) -> set:
        today = time.strftime("%Y-%m-%d")
        return {x["code"] for x in self._code_log()
                if time.strftime("%Y-%m-%d", time.localtime(x.get("at", 0))) == today}

    async def _fetch_available_codes(self) -> list:
        """从控制台拉可领的兑换码"""
        data = await self._api("/api/purchases?account=*&refresh=0")
        items = (data or {}).get("items") or []
        out = []
        for it in items:
            if it.get("state") == "available" and it.get("code"):
                out.append({"code": it["code"], "name": it.get("name") or "",
                            "at": it.get("claimed_at") or 0})
        out.sort(key=lambda x: x["at"])          # 最早的在前
        return out

    def _in_window(self) -> bool:
        now = time.strftime("%H:%M")
        start = str(self._cfg("daily_code_start", "08:30") or "08:30")
        end = str(self._cfg("daily_code_end", "10:00") or "10:00")
        return start <= now <= end

    async def _daily_code_once(self) -> int:
        """执行一次每日发码，返回发出条数"""
        if not bool(self._cfg("daily_code_enabled", True)):
            return 0
        if not self._in_window():
            return 0
        groups = self._code_groups()
        if not groups:
            return 0
        keyword = str(self._cfg("daily_code_keyword", "神奇") or "神奇")
        count = max(1, int(self._cfg("daily_code_count", 10) or 10))
        tpl = str(self._cfg("daily_code_template", "神奇礼物蛋神兑换码———{code}")
                  or "神奇礼物蛋神兑换码———{code}")

        # 当天额度：每天最多发 count 个
        today = time.strftime("%Y-%m-%d")
        quota_sent = sum(
            1 for x in self._code_log()
            if x.get("way") == "每日自动"
            and time.strftime("%Y-%m-%d", time.localtime(x.get("at", 0))) == today)
        remain = count - quota_sent
        if remain <= 0:
            return 0

        sent_today = self._sent_today()
        codes = [c for c in await self._fetch_available_codes()
                 if keyword in c["name"] and c["code"] not in sent_today]

        # 不够就先去市集补（默认关闭，要手动开 auto_buy_enabled）
        if len(codes) < remain:
            short = remain - len(codes)
            bought = await self._auto_buy_shortfall(short)
            if bought:
                try:
                    data = await self._api("/api/purchases?account=*&refresh=1")
                    items = (data or {}).get("items") or []
                    codes = sorted(
                        [{"code": it["code"], "name": it.get("name") or "",
                          "at": it.get("claimed_at") or 0}
                         for it in items
                         if it.get("state") == "available" and it.get("code")
                         and keyword in str(it.get("name") or "")
                         and it["code"] not in sent_today],
                        key=lambda x: x["at"])
                except Exception:
                    pass

        if not codes:
            return 0
        picked = codes[:remain]                   # 时间最早的 N 个（受当天额度限制）
        import random as _random
        ok = 0
        for c in picked:
            gid = _random.choice(groups)
            text = tpl.format(code=c["code"], egg=c["name"])
            if await self._send_to_group(gid, text):
                self._log_code(c["code"], c["name"], gid, "每日自动")
                ok += 1
            await asyncio.sleep(1.0)
        if ok:
            logger.info(f"[发码] 每日自动发放 {ok} 个（关键字「{keyword}」）")
        return ok

    async def _daily_code_loop(self) -> None:
        """每分钟检查一次，在时间窗内把当天额度发完"""
        while True:
            try:
                n = await self._daily_code_once()
                if n:
                    logger.info(f"[发码] 本轮发出 {n} 个")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[发码] 自动发放异常: {type(e).__name__}: {e}")
            await asyncio.sleep(60)

    async def _sync_market(self) -> bool:
        """让控制台重新拉一遍市集快照"""
        try:
            raw = await self._api_post("/api/market/sync", "{}")
            return bool(raw and raw.get("ok"))
        except Exception as e:
            logger.warning(f"[补货] 刷新市场失败: {type(e).__name__}: {e}")
            return False

    async def _auto_buy_shortfall(self, need: int) -> int:
        """券不够时补货：买不到就先刷新市场重试，仍不够再加价。

        每一档的流程：
           买 → 不够 → 刷新市场 → 再买一次 → 还不够 → 加 auto_buy_step 分
        价格从 auto_buy_max_points 开始，加到 auto_buy_ceiling_points 为止。
        """
        if not bool(self._cfg("auto_buy_enabled", False)):
            return 0
        if need <= 0:
            return 0
        try:
            per_round = max(1, min(20, int(self._cfg("auto_buy_count", 10) or 10)))
            price = float(self._cfg("auto_buy_max_points", 10) or 10)
            ceiling = float(self._cfg("auto_buy_ceiling_points", 13) or 13)
            step = float(self._cfg("auto_buy_step", 1) or 1)
            kw = str(self._cfg("auto_buy_keyword", "神奇蛋") or "神奇蛋")
        except Exception:
            return 0
        if step <= 0:
            step = 1.0
        if ceiling < price:
            ceiling = price

        total = 0
        rounds = 0
        while need - total > 0 and price <= ceiling + 1e-9 and rounds < 80:
            rounds += 1
            cnt = min(per_round, need - total)
            bought = 0
            # ① 先按当前价买
            try:
                raw = await self._api_post("/api/buy/codes", json.dumps(
                    {"keyword": kw, "max_points": price, "count": cnt, "confirm": True}))
                bought = int((raw or {}).get("bought") or 0)
            except Exception as e:
                logger.warning(f"[补货] {price:g} 分档失败: {type(e).__name__}")
            if bought:
                total += bought
                logger.info(f"[补货] {price:g} 分档买进 {bought} 个「{kw}」")
                if need - total <= 0:
                    break

            # ② 不够 → 刷新市场再试一次
            if need - total > 0:
                logger.info(f"[补货] {price:g} 分档没买够，刷新市场后重试")
                if await self._sync_market():
                    await asyncio.sleep(1.0)
                    try:
                        raw2 = await self._api_post("/api/buy/codes", json.dumps(
                            {"keyword": kw, "max_points": price,
                             "count": min(per_round, need - total), "confirm": True}))
                        b2 = int((raw2 or {}).get("bought") or 0)
                    except Exception:
                        b2 = 0
                    if b2:
                        total += b2
                        logger.info(f"[补货] 刷新后 {price:g} 分档又买进 {b2} 个")
                        if need - total <= 0:
                            break

            # ③ 还不够 → 加价
            price = round(price + step, 2)
            await asyncio.sleep(0.4)

        if total:
            logger.info(f"[补货] 共买进 {total} 个（轮次 {rounds}）")
        else:
            logger.info(f"[补货] 到上限 {ceiling:g} 分仍没买到")
        return total

    # ---------------------------------------------------------------- 发码到群
    def _code_groups(self) -> list:
        raw = str(self._cfg("code_groups", "") or "")
        for ch in ("，", ",", "\n", "\t", " "):
            raw = raw.replace(ch, ",")
        return [x.strip() for x in raw.split(",") if x.strip()]

    def _code_pool(self) -> list:
        raw = str(self._cfg("code_pool", "") or "")
        return [x.strip() for x in raw.splitlines() if x.strip()]

    def _known_umos(self) -> List[str]:
        return [str(u) for u in (self.state.get("umos") or []) if u]

    def _group_umos(self, group_id: str) -> List[str]:
        """给一个群号，给出所有可能可用的 umo（优先用真实见过的）"""
        gid = str(group_id).strip()
        out: List[str] = []
        # ① 真实见过这个群 → 直接用原样的 umo
        for u in self._known_umos():
            if u.endswith(f":GroupMessage:{gid}") or u.endswith(f":GroupMessage_{gid}"):
                if u not in out:
                    out.append(u)
        if out:
            return out
        # ② 没见过的群 → 用平台前缀拼，多试几个
        prefixes: List[str] = []
        for p in (self._platform(), "aiocqhttp", "qq_official", "gewechat"):
            if p and p not in prefixes:
                prefixes.append(p)
        for u in self._known_umos():
            p = u.split(":", 1)[0]
            if p and p not in prefixes:
                prefixes.append(p)
        return [f"{p}:GroupMessage:{gid}" for p in prefixes]

    async def _send_to_group(self, group_id: str, text: str) -> bool:
        """把一条消息发到指定群；确实发出去才算成功"""
        chains = self._chains(text)
        for umo in self._group_umos(group_id):
            last_err = ""
            for chain in chains:
                try:
                    ret = await self.context.send_message(umo, chain)
                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"
                    continue
                if ret is False:
                    last_err = "返回 False（会话不存在/平台不匹配）"
                    continue
                logger.info(f"[发码] 已发到 {umo}")
                return True
            logger.warning(f"[发码] {umo} 发送失败：{last_err}")
        logger.warning(f"[发码] 群 {group_id} 全部失败，试过: {self._group_umos(group_id)}")
        return False

    @filter.command("发码记录")
    async def cmd_code_log(self, event: AstrMessageEvent):
        """发码记录 —— 只有管理员能看"""
        if not self._is_admin(event):
            yield await self._say(event, "发码记录只有管理员能看。\n（在配置里把 QQ 号填进 admin_ids）")
            return
        log = self._code_log()
        if not log:
            yield await self._say(event, "还没有发码记录")
            return
        today = time.strftime("%Y-%m-%d")
        today_n = sum(1 for x in log
                      if time.strftime("%Y-%m-%d", time.localtime(x.get("at", 0))) == today)
        if self._cmd_args(event).strip() in ("清空", "clear"):
            self.state["code_log"] = []
            _save_state(self.state)
            yield await self._say(event, "已清空发码记录")
            return
        lines = [f"发码记录（共 {len(log)} 条 · 今天 {today_n} 个）"]
        for x in log[-15:][::-1]:
            when = time.strftime("%m-%d %H:%M", time.localtime(x.get("at", 0)))
            egg = f" {x['egg']}" if x.get("egg") else ""
            lines.append(f"· {when} · {x['code']} → 群 {x['group']}（{x.get('way', '')}）{egg}")
        if len(log) > 15:
            lines.append(f"…… 只显示最近 15 条")
        yield await self._say(event, "\n".join(lines))

    @filter.command("发码")
    async def cmd_give_code(self, event: AstrMessageEvent):
        """发码 兑换码 —— 随机发到配置里指定的群"""
        async for r in self._give_code(event):
            yield r

    @filter.command("输入兑换码")
    async def cmd_give_code_alias(self, event: AstrMessageEvent):
        """输入兑换码 XXX —— 和「发码」等价"""
        async for r in self._give_code(event):
            yield r

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

    async def _give_code(self, event: AstrMessageEvent):
        import random as _random

        groups = self._code_groups()
        if not groups:
            yield await self._say(event, 
                "还没配置群号。\n"
                "去 AstrBot WebUI → 插件 → 本插件 → 配置 → `code_groups` 填群号（多个用逗号隔开）")
            return

        arg = self._cmd_args(event).strip()
        egg_name = ""
        if arg:
            # ② 带了码 → 直接发这个
            codes = [x for x in arg.split() if x][:1]
        else:
            # ① 没带码 → 从最早的 10 个可领码里随机取 1 个
            pool = []
            try:
                available = await self._fetch_available_codes()
                kw = str(self._cfg("daily_code_keyword", "") or "")
                if kw:
                    available = [c for c in available if kw in c["name"]]
                pool = available[:10]
            except Exception:
                pool = []
            if not pool:
                # 控制台拿不到就退回配置里的备选池
                cfg_pool = self._code_pool()[:10]
                pool = [{"code": x, "name": ""} for x in cfg_pool]
            if not pool:
                yield await self._say(event, 
                    "没有可发的兑换码。\n"
                    "控制台没连上、或者当前没有可领的码。\n"
                    "也可以直接指定：发码 EGG-XXXX-XXXX")
                return
            pick = _random.choice(pool)
            codes = [pick["code"]]
            egg_name = pick.get("name") or ""

        group_id = _random.choice(groups)
        tpl = str(self._cfg("daily_code_template", "神奇礼物蛋神兑换码———{code}")
                  or "神奇礼物蛋神兑换码———{code}")
        text = tpl.format(code=codes[0], egg=egg_name) if not arg else codes[0]
        ok = await self._send_to_group(group_id, text)
        if ok:
            for _c in codes:
                self._log_code(_c, egg_name, group_id, "手动")
            yield await self._say(event, 
                f"已发放：{text}\n"
                f"发到群：{group_id}（从 {len(groups)} 个群里随机选）")
        else:
            tried = "、".join(self._group_umos(group_id)[:3])
            yield await self._say(event, 
                f"发到群 {group_id} 失败。\n"
                f"试过：{tried}\n"
                f"检查：① 群号对不对 ② 机器人在不在那个群 ③ 配置里的 platform 是否和适配器一致")

    # ---------------------------------------------------------------- 查ID
    #: 查ID / 查询ID：命令词大小写随便，中间空格随便，后面有没有空格都行
    _ID_CMD_RE = re.compile(r"^\s*[/#]?\s*(?:查\s*[Ii][Dd]|查询\s*[Ii][Dd])\s*(.*)$")

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
        if not q:
            return await self._say(event, 
                "格式：查ID Z10014514\n也可以用昵称：查ID 咕咕\n（大小写、空格都随意）")

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
        # 纯数字查不到就补个 Z 再试：玩家常把助手ID 前面的 Z 忘了写
        ok = isinstance(data, dict) and data.get("ok")
        if not ok and re.fullmatch(r"\d{5,12}", qid):
            alt = await self._api("/api/merchant?q=" + urllib.parse.quote("Z" + qid))
            if isinstance(alt, dict) and alt.get("ok"):
                data = alt
                note = f"（你发的是纯数字，按助手ID Z{qid} 查到了）"

        if isinstance(data, dict) and data.get("ok"):
            mark = self._marks().get((data.get("seller") or "").upper())
            out = self._merchant_block(data, mark)
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
                text += "\n———— 绑定变更 ——​——"
                for ch in _chs:
                    if ch.get("reason") == "换绑":
                        text += (f"\n· 换绑 {ch.get('game')} "
                                 f"{ch.get('old_seller')} → {ch.get('now_seller') or '—'}")
                    else:
                        text += f"\n· 改名 {ch.get('game')} 「{ch.get('old_role') or ''}」"
            png = None
            if self._image_reply_on():
                try:
                    avatar = await self._avatar_path(
                        str(data.get("seller") or ""),
                        (data.get("merchant") or {}).get("avatar") or "")
                    png = await asyncio.to_thread(self._merchant_card, data, mark, avatar)
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
        rows: List[tuple] = [("蛋组", rec.get("蛋组") or ""),
                             ("公母", rec.get("公母") or "未知"),
                             ("进化链", rec.get("进化链") or "")]
        if len(variants) <= 1:
            v = variants[0] if variants else {}
            rows += [("蛋名", v.get("蛋名") or rec.get("蛋名") or ""),
                     ("身高区间", v.get("身高区间") or ""),
                     ("体重区间", v.get("体重区间") or "")]
            return rows
        rows.append(("形态", f"{len(variants)} 种"))
        default_egg = str(variants[0].get("蛋名") or "")
        for i, cand in enumerate(variants[:8], 1):
            nm = str(cand.get("形态简称") or cand.get("形态") or "")
            bins = cand.get("区间数")
            rows.append((f"{i}. {nm}",
                         (f"区间 {bins} · " if bins not in (None, "", 0) else "")
                         + str(cand.get("体重区间") or "")))
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
                "· 查成交：查询 火花 最近（历史 也行）")
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
                f"图鉴里没有「{q}」。\n也可以发「身高/体重」，"
                f"比如 34/3.617 或 0.34 3.617")
        text, png = await self._species_payload(rec, q)
        return await self._reply(event, text, png)

    async def _recent_reply(self, event: Any, name: str):
        """最近成交：走控制台 /api/market/recent（底层是 getPrivateMarketTrend）。"""
        q = str(name or "").strip()
        if not q:
            return await self._say(event, "格式：查询 火花 最近")
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
        """改绑记录 [N] —— 谁把绑定的游戏角色换到别的助手了"""
        yield await self._roles_changes(event, "换绑")

    @filter.command("改名记录")
    async def cmd_rename_hist(self, event: AstrMessageEvent):
        """改名记录 [N] —— 绑定过的游戏角色改过名的"""
        yield await self._roles_changes(event, "改名")

    async def _roles_changes(self, event: Any, reason: str):
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
                rows.append((gid, f"「{x.get('old_role') or ''}」改名　{at}"))
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
            f"✅ 已绑定推送目标\n{umo}\n之后买到蛋会推送到这里（共 {len(self.state['umos'])} 个目标）"
        )

    @filter.command("蛋推送解绑")
    async def cmd_unbind(self, event: AstrMessageEvent):
        umo = getattr(event, "unified_msg_origin", "") or ""
        umos = [u for u in (self.state.get("umos") or []) if u != umo]
        self.state["umos"] = umos
        _save_state(self.state)
        yield await self._say(event, f"已解绑当前会话，剩余 {len(umos)} 个目标")

    @filter.command("蛋推送测试")
    async def cmd_test(self, event: AstrMessageEvent):
        """发一条样例消息，确认推送链路。"""
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
        data = await self._api("/api/purchases")
        api_ok = bool(data and data.get("ok"))
        lines = [
            "市集推送状态",
            f"接口：{self.api_base}（{'正常' if api_ok else '读取失败'}）",
            f"平台：{self._platform()}",
            f"目标：{len(self._candidate_umos())} 个",
        ]
        for u in self._candidate_umos()[:5]:
            lines.append(f"  · {u}")
        lines += [
            f"间隔：{self.interval:g}s · 轮询 {self.stats['polls']} 次",
            f"已推送 {self.stats['sent']} · 忽略 {self.stats['skipped']} · 失败 {self.stats['errors']}",
            f"已记录 id：{len(self.state.get('seen') or [])} 个",
        ]
        if data and data.get("stale"):
            lines.append("⚠ 控制台用的是缓存数据（远端接口暂时不可用）")
        yield await self._say(event, "\n".join(lines))

    @filter.command("蛋推送重置")
    async def cmd_reset(self, event: AstrMessageEvent):
        keep_umos = self.state.get("umos") or []
        self.state = {"seen": [], "started": 0, "umos": keep_umos}
        _save_state(self.state)
        yield await self._say(event, "已清空推送记录（绑定关系保留），下一轮会把现有记录当新的处理")




