# -*- coding: utf-8 -*-
"""运行时素材仓库：把「图标 PNG」从安装包挪到本地缓存。

为什么：
    1541 张图标占安装包 7.09 MB（93%），插件市场按包体审核，7.61 MB 过不了。
    包体只留 `icons/index.json`（名字→slug 映射，17 KB），
    PNG 在**首次运行时**一次性拉一个 zip 解到 data/icons/，之后永久命中本地缓存。

取素材的两条路（按顺序，全失败就降级成没有图标，不影响文字/版面）：
    1. 蛋神助手控制台 `{api_base}/api/plugin-assets/zip` —— 本机，最快；
       控制台可以预先把 wiki / 官方资源全部下好（“先把模型全部下载”就在这一步）。
    2. 配置里的 `asset_url`（GitHub Release 的 icons.zip 之类），给没装控制台的用户兜底。

设计要点：
    * 只发**一个** zip 请求，不做 1500 次小请求；
    * 先解到临时目录再原子替换，断电不会留半个文件；
    * 有 `.synced` 标记 + 包体大小比对，已经同步过就不再请求；
    * 任何异常都吞掉并返回统计，绝不让素材问题把插件搞挂。
"""
from __future__ import annotations

import asyncio
import json
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

#: 包内只保留这个文件（名字 → slug 的映射）
INDEX_NAME = "index.json"

#: 图标三大类
KINDS = ("pet", "egg", "shiny")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class SyncStats:
    ok: bool = False
    source: str = ""
    files: int = 0
    bytes: int = 0
    skipped: bool = False
    message: str = ""

    def line(self) -> str:
        if self.skipped:
            return f"素材已是最新（{self.files} 张，跳过下载）"
        if self.ok:
            return (f"素材同步完成：{self.files} 张 / "
                    f"{self.bytes / 1024 / 1024:.1f} MB（来源：{self.source}）")
        return f"素材同步失败：{self.message or '没有可用来源'}"


class AssetStore:
    """图标缓存仓库。只管「文件在不在」「要不要拉一次」。"""

    def __init__(self, data_root: Path, plugin_dir: Path,
                 console_base: str = "http://127.0.0.1:8777",
                 asset_url: str = "", timeout: float = 60.0,
                 logger: Any = None) -> None:
        self.data_root = Path(data_root)          # 一般是插件 data/ 目录
        self.plugin_dir = Path(plugin_dir)        # 插件安装目录（包内 icons/index.json 在这）
        self.console_base = (console_base or "").rstrip("/")
        self.asset_url = (asset_url or "").strip()
        self.timeout = float(timeout)
        self.logger = logger
        self._lock = asyncio.Lock()
        self._done = False

    # ---------------- 路径 ----------------

    @property
    def root(self) -> Path:
        return self.data_root / "icons"

    @property
    def index_file(self) -> Path:
        """包内 index.json（始终随包发布）。"""
        return self.plugin_dir / "icons" / INDEX_NAME

    @property
    def cache_index_file(self) -> Path:
        return self.root / INDEX_NAME

    def local_path(self, kind: str, slug: str) -> Optional[Path]:
        p = self.root / kind / f"{slug}.png"
        return p if p.is_file() else None

    def count(self) -> int:
        n = 0
        for k in KINDS:
            d = self.root / k
            if d.is_dir():
                n += sum(1 for _ in d.glob("*.png"))
        return n

    def marker(self) -> Optional[Dict[str, Any]]:
        try:
            return json.loads((self.root / ".synced").read_text(encoding="utf-8"))
        except Exception:
            return None

    # ---------------- 同步 ----------------

    async def ensure(self, force: bool = False) -> SyncStats:
        """确保本地有图标。已经同步过就直接返回 skipped。"""
        async with self._lock:
            if self._done and not force:
                return SyncStats(ok=True, skipped=True, files=self.count(),
                                 source="cache", message="本次进程已同步")
            m = self.marker()
            if not force and m and self.count() >= int(m.get("files") or 0) > 0:
                self._done = True
                return SyncStats(ok=True, skipped=True, files=self.count(),
                                 source=str(m.get("source") or "cache"))

            errors: List[str] = []
            for name, getter in (("控制台", self._from_console),
                                 ("asset_url", self._from_url)):
                try:
                    data = await getter()
                except Exception as e:
                    errors.append(f"{name}: {type(e).__name__}: {e}")
                    continue
                if not data:
                    continue
                st = self._install(data, name)
                if st.ok:
                    self._done = True
                    return st
                errors.append(f"{name}: {st.message}")

            msg = "；".join(errors) if errors else "控制台与 asset_url 都不可用"
            self._warn(f"[素材] 同步失败：{msg}")
            return SyncStats(ok=False, message=msg)

    #: 解包后至少要这么多张才算成功（防止下到一个 404 页面/chenk zip）
    MIN_FILES = 200

    def _install(self, raw: bytes, source: str) -> SyncStats:
        tmp = self.root.with_name("icons.tmp")
        try:
            if len(raw) < 50_000:
                return SyncStats(ok=False, message=f"包太小（{len(raw)} B），不像素材包")
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True, exist_ok=True)
            import io
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                names = [n for n in z.namelist() if n.lower().endswith(".png")]
                if len(names) < self.MIN_FILES:
                    return SyncStats(ok=False,
                                     message=f"包内只有 {len(names)} 张图，不像完整素材包")
                z.extractall(tmp)

            # 兼容「zip 里多一层目录」的情况：找到含 pet/egg/shiny 的那一层
            base = tmp
            if not (base / "pet").is_dir():
                for cand in tmp.iterdir():
                    if cand.is_dir() and (cand / "pet").is_dir():
                        base = cand
                        break

            # 原子替换
            old = self.root.with_name("icons.old")
            import shutil
            shutil.rmtree(old, ignore_errors=True)
            if self.root.exists():
                self.root.rename(old)
            shutil.move(str(base), str(self.root))
            shutil.rmtree(old, ignore_errors=True)

            files = self.count()
            (self.root / ".synced").write_text(json.dumps({
                "at": _now(), "source": source, "files": files,
                "bytes": len(raw),
            }, ensure_ascii=False), encoding="utf-8")
            self._info(f"[素材] 已安装 {files} 张图标（来源 {source}，{len(raw)/1024/1024:.1f} MB）")
            return SyncStats(ok=True, source=source, files=files, bytes=len(raw))
        except Exception as e:
            return SyncStats(ok=False, message=f"{type(e).__name__}: {e}")
        finally:
            try:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass

    async def _from_console(self) -> Optional[bytes]:
        if not self.console_base:
            return None
        url = f"{self.console_base}/api/plugin-assets/zip"
        return await self._http_get(url)

    async def _from_url(self) -> Optional[bytes]:
        if not self.asset_url:
            return None
        return await self._http_get(self.asset_url)

    async def _http_get(self, url: str) -> Optional[bytes]:
        """先试 aiohttp；环境里没有/用不了就退到 urllib（放线程里跑）。"""
        headers = {"User-Agent": "roco-lk-egg-assets/1.0"}
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=self.timeout),
                                 headers=headers) as r:
                    if r.status != 200:
                        self._warn(f"[素材] {url[:60]} → HTTP {r.status}")
                        return None
                    return await r.read()
        except Exception as e:
            self._warn(f"[素材] aiohttp 不可用（{type(e).__name__}），改用 urllib 重试")
        try:
            return await asyncio.to_thread(self._urllib_get, url, headers)
        except Exception as e:
            self._warn(f"[素材] {url[:60]} 取失败: {type(e).__name__}: {e}")
            return None

    def _urllib_get(self, url: str, headers: Dict[str, str]) -> Optional[bytes]:
        import urllib.request
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                if r.status != 200:
                    self._warn(f"[素材] {url[:60]} → HTTP {r.status}")
                    return None
                return r.read()
        except Exception as e:
            self._warn(f"[素材] {url[:60]} 取失败: {type(e).__name__}: {e}")
            return None

    # ---------------- 日志 ----------------

    def _warn(self, msg: str) -> None:
        if self.logger:
            try:
                self.logger.warning(msg)
            except Exception:
                pass

    def _info(self, msg: str) -> None:
        if self.logger:
            try:
                self.logger.info(msg)
            except Exception:
                pass
