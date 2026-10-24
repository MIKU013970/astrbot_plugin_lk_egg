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

_PLUGIN_DIR = Path(__file__).resolve().parent
_PLUGIN_NAME = "astrbot_plugin_lk_egg"


def _data_dir() -> Path:
    """按平台规范：持久化数据放 data/plugin_data/<plugin_name>/"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        return Path(get_astrbot_data_path()) / "plugin_data" / _PLUGIN_NAME
    except Exception:
        # 兼容旧版 AstrBot / 独立测试
        return Path("data") / "plugin_data" / _PLUGIN_NAME


_STATE_PATH = _data_dir() / "notified.json"
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
    "1.8.0",
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
        if not bool(self._cfg("enabled", True)):
            logger.info("[市集推送] 配置 enabled=false，插件不启动")
            return
        self._task = asyncio.create_task(self._loop())
        self._code_task = asyncio.create_task(self._daily_code_loop())

    async def terminate(self) -> None:
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
    def _load_ranges(self) -> Dict[str, Any]:
        """读同目录 ranges.json：蛋的体重区间 / 过线值 / 蛋组 / 进化链。"""
        try:
            p = _PLUGIN_DIR / "ranges.json"
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
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

    def _render_species(self, r: Dict[str, Any]) -> str:
        variants = r.get("variants") or []
        if not variants:                      # 兼容旧结构
            variants = [r]
        n = len(variants)
        head = f"{r['精灵']}（{variants[0].get('蛋名') or r.get('蛋名') or ''}）图鉴 {variants[0].get('图鉴编号') or r.get('图鉴编号') or ''}"
        if n > 1:
            head += f" · {n} 种形态"
        else:
            bins = variants[0].get("区间数")
            if bins not in (None, "", 0):
                head += f" · 区间 {bins}"
        lines = [head, f"蛋组：{r['蛋组']}",
                 f"公母比例：{r.get('公母') or '未知'}",
                 f"进化链：{r['进化链']}"]

        if n == 1:
            v = variants[0]
            lines += [
                f"身高区间：{v['身高区间']}",
                f"体重区间：{v['体重区间']}",
                f"小块头：过线 {v['小块头过线值']} · 极限 {v['小块头极限值']}",
                f"大块头：过线 {v['大块头过线值']} · 极限 {v['大块头极限值']}",
            ]
        else:
            for i, v in enumerate(variants, 1):
                label = v.get("形态") or v.get("蛋名") or ""
                lines.append(f"{i}. {label} · 区间 {v['区间数']}"
                             f" · {v['身高区间']} · {v['体重区间']}")
                lines.append(f"   小块头 {v['小块头过线值']} · 大块头 {v['大块头过线值']}"
                             f" · {v.get('公母') or '公母未知'}")
        return "\n".join(lines)

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
        data = await self._api(f"/api/merchants/search?q={sid}&limit=1")
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
        """'34/3.617' → (34.0, 3617.0)  单位：cm / 克"""
        import re as _re
        m = _re.match(r"^\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*$", str(text or ""))
        if not m:
            return None
        h = float(m.group(1))
        w_raw = float(m.group(2))
        # 带小数点 或 <100 当成 kg，否则当克
        if "." in m.group(2) or w_raw < 100:
            w = w_raw * 1000
        else:
            w = w_raw
        if not (1 <= h <= 400) or not (1 <= w <= 500000):
            return None
        return h, w

    def _match_species(self, h: float, w: float, limit: int = 5):
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
            lines.append(f"{i}. {x['精灵']} · 区间 {x['区间数']} · {size}")
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
        "—————— 查蛋（主功能）——————\n"
        "查询 火花               区间数/蛋组/进化链/身高体重/过线值\n"
        "查询 火神               进化链上任意名字都行（火神/焰火/火花）\n"
        "查询 鸭吉吉             多形态的精灵会把每种规格都列出来\n"
        "/34/3.617               身高/体重 查蛋（34cm 3.617kg）\n"
        "/34/3617                整数按克算（3617g）\n"
        "—————— 发码 ——————\n"
        "发码 EGG-XXXX-XXXX       随机发到设置里指定的群\n"
        "发码记录                 看最近发了哪些码\n"
        "（码不够时可在配置里开 auto_buy_enabled 自动补货）\n"
        "输入兑换码 EGG-XXXX      同上（两个名字都行）\n"
        "—————— 查助手（商家）——————\n"
        "查ID Z10014514          昵称/上架/售出/好评差评/货架明细\n"
        "查询ID Z10014514        同上（两个名字都能用）\n"
        "查ID 咕咕            也能用平台昵称查\n"
        "查ID Z10014514          游戏UID（已建索引的才有）\n"
        "—————— 名单 ——————\n"
        "标 Z10014514 黑 备注     加黑名单（备注可省略）\n"
        "标 Z10014514 白 备注     加白名单\n"
        "取消标 Z10014514         移出名单\n"
        "黑名单 / 白名单          列全部\n"
        "—————— 新上架提醒 ——————\n"
        "新上架 添加 Z10014514    开始监控这个商家\n"
        "新上架 删除 Z10014514 / 新上架 列表\n"
        "—————— 推送 ——————\n"
        "蛋推送绑定 / 解绑 / 测试 / 状态 / 重置\n"
        "（提示：直接发精灵名字的功能已关闭，请用「查询 名字」）"
    )

    FORMAT_HINTS = {
        "查询": "格式：查询 火花\n进化链上任意名字都行：查询 火神 / 查询 焰火",
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
    COMMAND_WORDS = ("查询ID", "查询", "查ID", "标", "取消标", "黑名单", "白名单",
                     "新上架", "蛋监控", "帮助", "菜单", "蛋推送")

    def _looks_like_command(self, text: str) -> bool:
        t = str(text or "").lstrip()
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
                yield event.plain_result("还没有监控任何商家。\n用：蛋监控 添加 Z10014514（换成真实助手ID）")
                return
            lines = [f"监控中的商家（{len(watch)} 个）"]
            for sid in watch[:20]:
                info = await self._seller_info(sid)
                nick = (info or {}).get("nickname") or "—"
                lines.append(f"· {nick}（{sid}）在售 {(info or {}).get('listed', '—')} 件")
            yield event.plain_result("\n".join(lines))
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
            yield event.plain_result(
                ("已添加监控：" + "、".join(added) + f"\n共 {len(self.state['watch'])} 个")
                if added else "没识别到要添加的助手ID（Z 开头）")
            return

        if sub in ("删除", "del", "remove"):
            removed = []
            for x in rest:
                x = x.strip().upper()
                if x in self.state["watch"]:
                    self.state["watch"].remove(x)
                    self.state["listings"].pop(x, None)
                    removed.append(x)
            _save_state(self.state)
            yield event.plain_result(
                ("已删除监控：" + "、".join(removed)) if removed else "列表里没有这些 ID")
            return

        yield event.plain_result("用法：\n蛋监控 添加 Z10014514\n蛋监控 删除 Z10014514\n蛋监控 列表")

    # ---------------------------------------------------------------- 指令：黑白名单
    @filter.command("标")
    async def cmd_mark(self, event: AstrMessageEvent):
        """标 Z10014514 黑 欠钱不还"""
        args = self._cmd_parts(event)
        if len(args) < 3:
            yield event.plain_result("用法：标 Z10014514 黑 备注写这里\n（黑=黑名单，白=白名单，备注可省略）")
            return
        sid = args[1].upper()
        kind = args[2]
        if kind not in ("黑", "白"):
            yield event.plain_result("第二个参数只能是「黑」或「白」")
            return
        note = " ".join(args[3:])
        by = getattr(event, "unified_msg_origin", "") or ""
        rec = self._set_mark(sid, kind, note, by)
        yield event.plain_result(await self._describe_mark(sid, rec))

    @filter.command("取消标")
    async def cmd_unmark(self, event: AstrMessageEvent):
        args = self._cmd_parts(event)
        if len(args) < 2:
            yield event.plain_result("用法：取消标 Z10014514")
            return
        sid = args[1].upper()
        yield event.plain_result(f"已取消 {sid} 的标记" if self._del_mark(sid)
                                 else f"{sid} 不在名单里")

    @filter.command("黑名单")
    async def cmd_blacklist(self, event: AstrMessageEvent):
        rows = self._marks_of("黑")
        if not rows:
            yield event.plain_result("黑名单是空的")
            return
        lines = [f"黑名单（{len(rows)} 个）"]
        for sid, rec in rows[:30]:
            info = await self._seller_info(sid)
            nick = (info or {}).get("nickname") or "—"
            note = f" · {rec['note']}" if rec.get("note") else ""
            lines.append(f"· {nick}（{sid}）{note}")
        if len(rows) > 30:
            lines.append(f"…… 还有 {len(rows) - 30} 个")
        yield event.plain_result("\n".join(lines))

    @filter.command("白名单")
    async def cmd_whitelist(self, event: AstrMessageEvent):
        rows = self._marks_of("白")
        if not rows:
            yield event.plain_result("白名单是空的")
            return
        lines = [f"白名单（{len(rows)} 个）"]
        for sid, rec in rows[:30]:
            info = await self._seller_info(sid)
            nick = (info or {}).get("nickname") or "—"
            note = f" · {rec['note']}" if rec.get("note") else ""
            lines.append(f"· {nick}（{sid}）{note}")
        if len(rows) > 30:
            lines.append(f"…… 还有 {len(rows) - 30} 个")
        yield event.plain_result("\n".join(lines))

    @filter.command("帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(self.MENU)

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
                yield event.plain_result(self.FORMAT_HINTS.get("新上架", "格式：\n新上架 添加 Z10014514\n新上架 删除 Z10014514\n新上架 列表"))
                return
            lines = [f"监控中的商家（{len(watch)} 个）"]
            for sid in watch[:20]:
                info = await self._seller_info(sid)
                lines.append(f"· {(info or {}).get('nickname') or '—'}（{sid}）"
                             f"在售 {(info or {}).get('listed', '—')} 件")
            yield event.plain_result("\n".join(lines))
            return

        if sub in ("添加", "add"):
            added = []
            for x in rest:
                x = x.strip().upper()
                if x.startswith("Z") and x not in self.state["watch"]:
                    self.state["watch"].append(x)
                    added.append(x)
            _save_state(self.state)
            yield event.plain_result(
                ("已添加监控：" + "、".join(added) + f"\n共 {len(self.state['watch'])} 个")
                if added else self.FORMAT_HINTS.get("新上架", "格式：\n新上架 添加 Z10014514\n新上架 删除 Z10014514\n新上架 列表"))
            return

        if sub in ("删除", "del", "remove"):
            removed = []
            for x in rest:
                x = x.strip().upper()
                if x in self.state["watch"]:
                    self.state["watch"].remove(x)
                    self.state["listings"].pop(x, None)
                    removed.append(x)
            _save_state(self.state)
            yield event.plain_result(
                ("已删除监控：" + "、".join(removed)) if removed
                else self.FORMAT_HINTS.get("新上架", "格式：\n新上架 添加 Z10014514\n新上架 删除 Z10014514\n新上架 列表"))
            return

        yield event.plain_result(self.FORMAT_HINTS.get("新上架", "格式：\n新上架 添加 Z10014514\n新上架 删除 Z10014514\n新上架 列表"))

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
        yield event.plain_result(self._render_species(rec))

    @filter.regex(r"^\s*/?\s*\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?\s*$")
    async def on_bare_hw(self, event: AstrMessageEvent):
        """34/3.617 或 /34/3.617 —— 身高/体重 查蛋"""
        raw = (getattr(event, "message_str", "") or "").strip()
        prefix = str(self._cfg("wake_prefix", "/") or "/")
        body = raw[len(prefix):].strip() if raw.startswith(prefix) else raw
        hw = self._parse_hw(body)
        if not hw:
            return
        yield event.plain_result(self._render_identify(*hw))

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
            yield event.plain_result(await self._describe_mark(sid, rec))
            return
        # 单纯 Z 号 → 查商家
        data = await self._api("/api/merchant?q=" + urllib.parse.quote(sid))
        if data and data.get("ok"):
            mark = self._marks().get(sid)
            yield event.plain_result(self._merchant_block(data, mark))
            return
        if data and data.get("blocked_by"):
            yield event.plain_result(
                f"查不到 {sid}：这个商家屏蔽了你的账号（{'、'.join(data['blocked_by'])}）。")
            return
        mark = self._marks().get(sid)
        if mark:
            yield event.plain_result(await self._describe_mark(sid, mark))
            return
        yield event.plain_result(
            f"没查到商家 {sid}。\n如果是精灵名，用：查询 火花")

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

    async def _auto_buy_shortfall(self, need: int) -> int:
        """券不够时去市集补货：从起始价开始，每次 +1 分，直到上限。

        例：10 分没买到 → 11 分 → 12 分 → 13 分（默认上限 13，可配）
        返回总共买到的数量。
        """
        if not bool(self._cfg("auto_buy_enabled", False)):
            return 0
        if need <= 0:
            return 0
        try:
            per_round = max(1, min(20, int(self._cfg("auto_buy_count", 10) or 10)))
            start_price = float(self._cfg("auto_buy_max_points", 10) or 10)
            ceiling = float(self._cfg("auto_buy_ceiling_points", 13) or 13)
            kw = str(self._cfg("auto_buy_keyword", "神奇蛋") or "神奇蛋")
        except Exception:
            return 0
        if ceiling < start_price:
            ceiling = start_price

        total = 0
        price = start_price
        while need - total > 0 and price <= ceiling:
            cnt = min(per_round, need - total)
            body = json.dumps({"keyword": kw, "max_points": price,
                               "count": cnt, "confirm": True})
            try:
                raw = await self._api_post("/api/buy/codes", body)
            except Exception as e:
                logger.warning(f"[补货] {price:g} 分档调用失败: {type(e).__name__}: {e}")
                raw = None
            bought = int((raw or {}).get("bought") or 0)
            if bought:
                total += bought
                logger.info(f"[补货] {price:g} 分档买进 {bought} 个「{kw}」")
            else:
                logger.info(f"[补货] {price:g} 分档没买到，加价到 {price + 1:g}")
            price += 1
            await asyncio.sleep(0.5)

        if total:
            logger.info(f"[补货] 共买进 {total} 个（价格档 {start_price:g}~{ceiling:g}）")
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

    async def _send_to_group(self, group_id: str, text: str) -> bool:
        """把一条消息发到指定群"""
        plat = self._platform()
        umo = f"{plat}:GroupMessage:{group_id}"
        for chain in self._chains(text):
            try:
                await self.context.send_message(umo, chain)
                return True
            except Exception as e:
                logger.warning(f"[发码] 发到 {umo} 失败: {type(e).__name__}: {e}")
        return False

    @filter.command("发码记录")
    async def cmd_code_log(self, event: AstrMessageEvent):
        """发码记录 —— 看最近发了哪些码"""
        log = self._code_log()
        if not log:
            yield event.plain_result("还没有发码记录")
            return
        today = time.strftime("%Y-%m-%d")
        today_n = sum(1 for x in log
                      if time.strftime("%Y-%m-%d", time.localtime(x.get("at", 0))) == today)
        lines = [f"发码记录（共 {len(log)} 条 · 今天 {today_n} 个）"]
        for x in log[-15:][::-1]:
            when = time.strftime("%m-%d %H:%M", time.localtime(x.get("at", 0)))
            egg = f" {x['egg']}" if x.get("egg") else ""
            lines.append(f"· {when} · {x['code']} → 群 {x['group']}（{x.get('way', '')}）{egg}")
        if len(log) > 15:
            lines.append(f"…… 只显示最近 15 条")
        yield event.plain_result("\n".join(lines))

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

    async def _give_code(self, event: AstrMessageEvent):
        import random as _random

        groups = self._code_groups()
        arg = self._cmd_args(event).strip()
        if not groups:
            yield event.plain_result(
                "还没配置群号。\n"
                "去 AstrBot WebUI → 插件 → 本插件 → 配置 → `code_groups` 填群号（多个用逗号隔开）")
            return

        # 取码：参数优先，否则从备选池随机
        codes = [x for x in arg.split() if x]
        if not codes:
            pool = self._code_pool()
            if not pool:
                yield event.plain_result(
                    "没给兑换码，备选池也是空的。\n"
                    "用法：发码 EGG-XXXX-XXXX\n"
                    "或者在配置里填 `code_pool`（一行一个）")
                return
            codes = [_random.choice(pool)]

        group_id = _random.choice(groups)
        text = codes[0] if len(codes) == 1 else "\n".join(codes)
        ok = await self._send_to_group(group_id, text)
        if ok:
            for _c in codes:
                self._log_code(_c, "", group_id, "手动")
            show = codes[0] if len(codes) == 1 else f"{len(codes)} 个码"
            yield event.plain_result(
                f"已发放：{show}\n"
                f"发到群：{group_id}（从 {len(groups)} 个群里随机选）")
        else:
            yield event.plain_result(
                f"发到群 {group_id} 失败，检查群号对不对、机器人还在不在群里")

    @filter.command("查ID")
    async def cmd_lookup_id(self, event: AstrMessageEvent):
        """查ID Z10014514 / 查ID 咕咕"""
        q = self._cmd_args(event)
        if not q:
            yield event.plain_result(
                "格式：查ID Z10014514\n也可以用昵称：查ID 咕咕")
            return
        data = await self._api("/api/merchant?q=" + urllib.parse.quote(str(q)))
        mark = None
        if data and data.get("ok"):
            mark = self._marks().get((data.get("seller") or "").upper())
            yield event.plain_result(self._merchant_block(data, mark))
            return
        if data and data.get("blocked_by"):
            yield event.plain_result(
                f"查不到「{q}」：这个商家屏蔽了你的账号（{'、'.join(data['blocked_by'])}）。\n"
                f"换个没被屏蔽的账号再查；挂单本身在市集还能看到。")
            return
        if data and data.get("error"):
            yield event.plain_result(f"查不到「{q}」：{str(data['error'])[:60]}")
            return
        mark = self._marks().get(q.upper())
        if mark:
            yield event.plain_result(await self._describe_mark(q.upper(), mark))
            return
        yield event.plain_result(
            f"没查到商家「{q}」。\n如果他是精灵名，用：查询 {q}")

    @filter.command("查询ID")
    async def cmd_lookup_id_alias(self, event: AstrMessageEvent):
        """查ID Z10014514 / 查ID 咕咕"""
        q = self._cmd_args(event)
        if not q:
            yield event.plain_result(
                "格式：查ID Z10014514\n也可以用昵称：查ID 咕咕")
            return
        data = await self._api("/api/merchant?q=" + urllib.parse.quote(str(q)))
        mark = None
        if data and data.get("ok"):
            mark = self._marks().get((data.get("seller") or "").upper())
            yield event.plain_result(self._merchant_block(data, mark))
            return
        if data and data.get("blocked_by"):
            yield event.plain_result(
                f"查不到「{q}」：这个商家屏蔽了你的账号（{'、'.join(data['blocked_by'])}）。\n"
                f"换个没被屏蔽的账号再查；挂单本身在市集还能看到。")
            return
        if data and data.get("error"):
            yield event.plain_result(f"查不到「{q}」：{str(data['error'])[:60]}")
            return
        mark = self._marks().get(q.upper())
        if mark:
            yield event.plain_result(await self._describe_mark(q.upper(), mark))
            return
        yield event.plain_result(
            f"没查到商家「{q}」。\n如果他是精灵名，用：查询 {q}")

    @filter.command("查询")
    async def cmd_species(self, event: AstrMessageEvent):
        """查询 火花（进化链上的名字都行）"""
        q = self._cmd_args(event)
        if not q:
            yield event.plain_result(
                "格式：查询 火花\n也可以直接发精灵名字：火神")
            return
        hw = self._parse_hw(q)
        if hw:
            yield event.plain_result(self._render_identify(*hw))
            return
        rec = self._find_species(q)
        yield event.plain_result(self._render_species(rec) if rec
                                 else f"图鉴里没有「{q}」。\n也可以发「身高/体重」，比如 34/3.617")

    # 直接发精灵名字也能查（只认图鉴里存在的名字，不认识的直接不理）
    @filter.command("蛋推送绑定")
    async def cmd_bind(self, event: AstrMessageEvent):
        """在哪个会话发这条指令，就把推送目标绑定到哪个会话（推荐）。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            yield event.plain_result("拿不到当前会话标识，请在私聊/群里重新发送一次")
            return
        umos = self.state.get("umos") or []
        if umo not in umos:
            umos.append(umo)
        self.state["umos"] = umos[-10:]
        _save_state(self.state)
        yield event.plain_result(
            f"✅ 已绑定推送目标\n{umo}\n之后买到蛋会推送到这里（共 {len(self.state['umos'])} 个目标）"
        )

    @filter.command("蛋推送解绑")
    async def cmd_unbind(self, event: AstrMessageEvent):
        umo = getattr(event, "unified_msg_origin", "") or ""
        umos = [u for u in (self.state.get("umos") or []) if u != umo]
        self.state["umos"] = umos
        _save_state(self.state)
        yield event.plain_result(f"已解绑当前会话，剩余 {len(umos)} 个目标")

    @filter.command("蛋推送测试")
    async def cmd_test(self, event: AstrMessageEvent):
        """发一条样例消息，确认推送链路。"""
        sample_guess = {"top": [{"name": "电咩咩", "pct": 100.0, "tag": ""}], "special": []}
        sample = {"id": "test", "name": "神奇蛋", "points": 12, "seller": "Z10031963",
                  "courier": "快递员D1", "code": "EGG-XXXX-XXXX",
                  "claimed_at": int(time.time() * 1000),
                  "height_cm": 34, "weight_g": 3617, "state": "available"}
        n = await self._send(self._render(sample, sample_guess))
        yield event.plain_result(f"已向 {n} 个目标发送测试消息" if n
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
        yield event.plain_result("\n".join(lines))

    @filter.command("蛋推送重置")
    async def cmd_reset(self, event: AstrMessageEvent):
        keep_umos = self.state.get("umos") or []
        self.state = {"seen": [], "started": 0, "umos": keep_umos}
        _save_state(self.state)
        yield event.plain_result("已清空推送记录（绑定关系保留），下一轮会把现有记录当新的处理")




