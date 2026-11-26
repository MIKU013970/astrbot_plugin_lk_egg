# -*- coding: utf-8 -*-
"""高度定制化的**网格图**（4 列 · 大圆角卡片 · 环绕徽标 · 多色标签）。

这套渲染器是 `templates/grid.html`（HTML + Jinja2 + CSS Grid）的 **Pillow 复刻**：

    AstrBot 有 Playwright 渲染服务  → 走 templates/grid.html（CSS Grid，效果最接近设计稿）
    没有 / 渲染失败 / 超时          → 走 grid.py（这套），像素级同款，不依赖任何外网与浏览器

两条路吃的是**同一份数据结构**（见 `card()`），所以换引擎不会换排版。

数据结构
--------
```python
{
  "name": "满月砣",                       # 大标题（必填）
  "sub":  "图鉴 238 · 海洋组",            # 小字，一行，超宽自动省略
  "icon": "C:/.../pet/pet3582.png",       # 主图（一阶段，缺省画灰底问号）
  "ring": "#5C7AEA",                      # 头像外圈颜色（按蛋组取，见 ring_color）
  "tags": [{"text": "异色", "kind": "shiny"}],       # 多色标签，最多画两行
  "sats": [{"icon": "/…/egg/蛋107197.png"}]          # 环绕头像的小徽标，最多 4 个
}
```
"""
from __future__ import annotations

import base64
import hashlib
import io
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 版式常量

#: 网格列数（参考图就是 4 列）
COLS = 4

#: 默认画布宽度。固定宽度 → 每次渲染排版一致，不会被内容撑变形
WIDTH = 1080

#: 头像直径（稍微加大，更突出）
AVATAR = 96

#: 卡片圆角 / 容器圆角（羊皮纸 + 圆角徽章，洛克魔法图鉴感）
CARD_RADIUS = 18
BOX_RADIUS = 20

#: 环绕徽标直径
SAT = 24

# ---------------------------------------------------------------- 配色

#: 默认配色。前 8 个和 card.py 同名同义（card_theme 配置直接生效），
#: 其余是这套「暖纸图鉴」版式专用（grid_theme 配置覆盖）。
# 参考 rocom / gamewiki：暖纸底 + 白磨砂卡 + 单一金强调，卡片不再整张上色。
THEME_DEFAULT: Dict[str, str] = {
    "背景": "#F3EDE0",   # 暖纸底
    "卡片": "#FFFDF8",   # 白磨砂卡
    "边框": "#E9DFCC",   # 1px 暖灰边框
    "标题": "#2C2418",   # 深褐标题
    "正文": "#4A4032",   # 褐正文
    "次要": "#8E8170",   # 暖灰次要
    "强调": "#E8A72C",   # 单一金强调
    "标签底": "#F7F0E0", # 浅奶油标签/底色
    "网格线": "#DED2BC", # 分隔线
    "容器底": "#F3EDE0", # 页面纸底
    "头像底": "#FFF9EC", # 头像奶油底
    "阴影": "#6B5634",   # 暖褐阴影
    "空白": "#B8AB94",   # 空状态灰
    "天空顶": "#F6F0E3", # 纸面渐变顶（保留键，兼容旧配置）
    "天空底": "#EFE5D3", # 纸面渐变底
    "金": "#E8A72C",     # 金
    "金深": "#B9771D",   # 深金
    "纸": "#F3EDE0",     # 页面纸底
    "纸边": "#E9DFCC",   # 纸边框
    "云": "#FFFDF8",     # 保留键
    "星": "#FFD978",     # 保留键
}

#: 标签类型 → (文字色, 底色)。只做低饱和功能色，不抢主视觉。
TAG_STYLE: Dict[str, Tuple[str, str]] = {
    "shiny":    ("#6D4FB8", "#F1EBFB"),  # 异色
    "shinyegg": ("#2C7A66", "#E5F4EF"),  # 异色蛋
    "leader":   ("#A65A12", "#FBEFDF"),  # 首领化
    "group":    ("#6B5E4C", "#F5EFE3"),  # 蛋组
    "season":   ("#2F5FA8", "#E8EFFA"),  # 赛季
    "dex":      ("#6D4FB8", "#F1EBFB"),  # 图鉴号
    "special":  ("#2C6E8F", "#E5F2F7"),  # 特殊途径
    "ride":     ("#2F7D57", "#E8F4EC"),  # 可同乘
    "noride":   ("#8E8170", "#F1EADF"),  # 不可同乘
}

#: 蛋组 → 外圈颜色（低饱和，和暖纸底协调）。
RING_PALETTE: Tuple[str, ...] = (
    "#5B84C4", "#4E9C86", "#C98E3E", "#8E6CB8",
    "#5F9A5F", "#C46A78", "#4F91B5", "#B39A45",
)


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont
        return Image, ImageDraw, ImageFilter, ImageFont
    except Exception:
        return None


def available() -> bool:
    """Pillow 装了没。"""
    return _pil() is not None


# ---------------------------------------------------------------- 颜色工具


def _to_rgb(val: Any) -> Optional[Tuple[int, int, int]]:
    if isinstance(val, (tuple, list)) and len(val) == 3:
        try:
            return (int(val[0]), int(val[1]), int(val[2]))
        except (TypeError, ValueError):
            return None
    s = str(val or "").strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{3}", s):
        s = "".join(ch * 2 for ch in s)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", s):
        return None
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def parse_theme(text: Any, base: Optional[Dict[str, str]] = None) -> Dict[str, Tuple[int, int, int]]:
    """`键=#RRGGBB` 逐行 → {键: (r, g, b)}。认不出的键忽略，不影响其它项。"""
    out: Dict[str, Tuple[int, int, int]] = {}
    for key, val in (base if base is not None else THEME_DEFAULT).items():
        rgb = _to_rgb(val)
        if rgb:
            out[key] = rgb
    items: List[Tuple[str, str]] = []
    if isinstance(text, dict):
        items = [(str(k), str(v)) for k, v in text.items()]
    else:
        for line in str(text or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#") and "=" not in line:
                continue
            # 半角/全角两种写法都认，顺手把行尾注释切掉
            line = re.split(r"\s+#", line)[0].strip()
            for sep in ("：", ":", "="):
                if sep in line:
                    k, _, v = line.partition(sep)
                    items.append((k.strip(), v.strip()))
                    break
    for k, v in items:
        if k in out:
            rgb = _to_rgb(v)
            if rgb:
                out[k] = rgb
    return out


def _fg_on(bg: Tuple[int, int, int]) -> Tuple[int, int, int]:
    """在给定底色上选白字还是黑字（保证读得清）。"""
    lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
    return (255, 255, 255) if lum < 150 else (28, 24, 20)


def _mix(a: Tuple[int, int, int], b: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    t = max(0.0, min(1.0, float(t)))
    return (int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))


def ring_color(group: str) -> str:
    """蛋组名 → 稳定的外圈颜色（同组恒同色）。"""
    g = str(group or "").strip()
    if not g:
        return RING_PALETTE[0]
    h = hashlib.md5(g.encode("utf-8")).digest()
    return RING_PALETTE[h[0] % len(RING_PALETTE)]


def tag(text: str, kind: str = "group") -> Dict[str, str]:
    """造一个标签（带类型配色）。"""
    fg, bg = TAG_STYLE.get(str(kind or "group"), TAG_STYLE["group"])
    return {"text": str(text or ""), "kind": str(kind or "group"), "fg": fg, "bg": bg}


def card(name: str, sub: str = "", icon: Optional[str] = None,
         ring: str = "#5C7AEA", tags: Optional[Sequence[Any]] = None,
         sats: Optional[Sequence[Dict[str, Any]]] = None,
         elem: str = "", elem_color: str = "",
         bg: str = "", border: str = "", sub_fg: str = "",
         bins: Optional[int] = None,
         star: Optional[int] = None, review: Optional[int] = None,
         star_icon: str = "", review_icon: str = "") -> Dict[str, Any]:
    """造一张卡片。tags 可以传 dict（tag() 造的）、(文字, 类型) 元组或纯文字。

    elem / elem_color —— 系别文字（如 `龙/翼`）和系别主色（如 `#e64c62`）。
    给了系别色，卡片底色和头像外圈就跟着它走；bg / border 可以再显式指定。

    star / review —— 星光值和回顾值，画在精灵图**左边**和**右边**；
    star_icon / review_icon 是对应的 wiki 图标路径（会转成 data URI 内联）。
    """
    out_tags: List[Dict[str, str]] = []
    for t in (tags or []):
        if isinstance(t, dict):
            out_tags.append(t if t.get("fg") and t.get("bg") else tag(
                str(t.get("text") or ""), str(t.get("kind") or "group")))
        elif isinstance(t, (tuple, list)) and len(t) >= 2:
            out_tags.append(tag(str(t[0]), str(t[1])))
        elif t:
            out_tags.append(tag(str(t), "group"))
    return {
        "name": str(name or ""),
        "sub": str(sub or ""),
        "icon": str(icon) if icon else "",
        "ring": str(ring or RING_PALETTE[0]),
        "tags": out_tags,
        "sats": [s for s in (sats or []) if s],
        "elem": str(elem or ""),
        "elem_color": str(elem_color or ""),
        "bg": str(bg or ""),
        "border": str(border or ""),
        "sub_fg": str(sub_fg or ""),
        "bins": int(bins) if bins not in (None, "") else None,
        "star": int(star) if star not in (None, "") else 0,
        "review": int(review) if review not in (None, "") else 0,
        "star_img": data_uri(str(star_icon or "")),
        "review_img": data_uri(str(review_icon or "")),
    }


# ---------------------------------------------------------------- 小工具


# 普通 / 粗体候选字体名。和 card.py 保持同一套，粗体优先真粗体文件
FONT_REGULAR: Tuple[str, ...] = (
    "msyh.ttc", "msyh.ttf", "simhei.ttf", "Deng.ttf", "simsun.ttc",
    "NotoSansSC-Regular.otf", "SourceHanSansSC-Regular.otf", "font.ttf", "font.otf",
    "wqy-microhei.ttc", "wqy-zenhei.ttc", "NotoSansCJK-Regular.ttc",
    "NotoSansCJKsc-Regular.otf", "PingFang.ttc", "STHeiti Light.ttc",
    "Arial Unicode.ttf", "DejaVuSans.ttf",
)
FONT_BOLD: Tuple[str, ...] = (
    "msyhbd.ttc", "msyhbd.ttf", "simhei.ttf",
    "NotoSansSC-Bold.otf", "SourceHanSansSC-Bold.otf",
    "NotoSansCJK-Bold.ttc", "NotoSansCJKsc-Bold.otf", "PingFang.ttc", "simsun.ttc",
)

#: 探测目录：插件目录（打包字体）→ Windows → Linux → macOS
FONT_DIRS: Tuple[str, ...] = (
    str(Path(__file__).resolve().parent),
    str(Path(os.environ.get("WINDIR") or "C:/Windows") / "Fonts"),
    "/usr/share/fonts", "/usr/local/share/fonts", "~/.fonts",
    "/System/Library/Fonts", "/Library/Fonts", "~/Library/Fonts",
)

_FONT_RESOLVED: Dict[str, Optional[str]] = {}


def find_font(explicit: str = "", bold: bool = False) -> Optional[str]:
    """找一个能画中文的字体文件；找不到返回 None（调用方退回位图字体）。"""
    key = ("bold" if bold else "reg") + "|" + str(explicit or "")
    if key in _FONT_RESOLVED:
        return _FONT_RESOLVED[key]
    names = FONT_BOLD if bold else FONT_REGULAR
    cands: List[str] = []
    if explicit:
        p = Path(str(explicit))
        if p.is_file():
            cands.append(str(p))
        for n in names:                      # 指定了目录 / 同目录里的字体
            try:
                q = p.with_name(n)
                if q.is_file():
                    cands.append(str(q))
            except Exception:
                pass
    for d in FONT_DIRS:
        try:
            root = Path(str(d)).expanduser()
        except Exception:
            continue
        if not root.is_dir():
            continue
        for n in names:
            q = root / n
            if q.is_file():
                cands.append(str(q))
        if len(cands) >= 2:
            break
    hit = cands[0] if cands else None
    _FONT_RESOLVED[key] = hit
    return hit


def _font(font_path: Optional[str], size: int, bold: bool = False):
    """加载字体；找不到就用 Pillow 内置位图字体兜底（中文会变方块，但不崩）。"""
    _, _, _, ImageFont = _pil()  # type: ignore[misc]
    size = max(8, int(size))
    fp = find_font(str(font_path or ""), bold=bold)
    if not fp:
        fp = find_font("", bold=False)
    if fp:
        try:
            return ImageFont.truetype(fp, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _tw(draw, text: str, font) -> float:
    try:
        return float(draw.textlength(str(text), font=font))
    except Exception:
        try:
            return float(draw.textsize(str(text), font=font)[0])
        except Exception:
            return float(len(str(text)) * 10)


def _ellipsis(draw, text: str, font, limit: float) -> str:
    """超宽截断加省略号 —— 卡片是固定格子的，绝不让字跑出格子。"""
    s = "" if text is None else str(text)
    if not s or limit <= 0:
        return s
    if _tw(draw, s, font) <= limit:
        return s
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _tw(draw, s[:mid] + "…", font) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return (s[:lo] + "…") if lo else "…"


def _fit_icon(path: str, box: int):
    """按比例把图标缩进 box×box（不变形），返回 RGBA 图或 None。

    支持本地路径和 `data:image/...;base64,...`（card() 会把图标转成 data URI，
    以前这里只认路径，导致网格图的星光/回顾图标一直画不出来）。
    """
    Image, _, _, _ = _pil()  # type: ignore[misc]
    s = str(path or "")
    try:
        if s.startswith("data:"):
            raw = base64.b64decode(s.split(",", 1)[1])
            im = Image.open(io.BytesIO(raw)).convert("RGBA")
        else:
            im = Image.open(s).convert("RGBA")
    except Exception:
        return None
    w, h = im.size or (0, 0)
    if not w or not h:
        return None
    s = min(box / float(w), box / float(h))
    nw, nh = max(1, int(w * s)), max(1, int(h * s))
    try:
        return im.resize((nw, nh), Image.LANCZOS)
    except Exception:
        return None


def _paste_center(base, im, cx: int, cy: int) -> None:
    if im is None:
        return
    base.alpha_composite(im, (int(cx - im.width / 2), int(cy - im.height / 2)))


def card_height(base: int, avatar_d: int = AVATAR, tag_rows: int = 1) -> int:
    """卡片高度（两条引擎共用这个公式，HTML 的 .pet 也按它定高，版式才不会飘）。

    新结构（自上而下）：顶部系别色带 6 → 头像 108 → 名字 → 小字
    → 星光/回顾胶囊 22 → 标签 1~2 行。HTML 的 padding/margin 和这里一一对应。
    """
    base = max(10, int(base or 18))
    fs_name = base + 1
    fs_sub = max(10, base - 5)
    fs_tag = max(9, base - 7)
    pill_h = max(18, fs_tag + 9)
    tag_rows = max(1, min(2, int(tag_rows or 1)))
    name_line = int(fs_name * 1.30) + 2
    sub_line = int(fs_sub * 1.35) + 2
    return int(12 + 20 + 6 + avatar_d + 8 + name_line + 2 + sub_line + 4 + 18
               + 4 + tag_rows * pill_h + (tag_rows - 1) * 4 + 12)


def _tag_rows(draw, tags: Sequence[Any], font, maxw: float) -> List[List[Dict[str, Any]]]:
    """把标签排成最多两行（量宽度和真正画图共用这一份，保证量得准）。"""
    rows: List[List[Dict[str, Any]]] = [[]]
    row_w = 0.0
    for t in (tags or []):
        t = t if isinstance(t, dict) else {}
        txt = str(t.get("text") or "").strip()
        if not txt:
            continue
        w = _tw(draw, txt, font) + 16
        if row_w and row_w + 6 + w > maxw:
            if len(rows) >= 2:
                break
            rows.append([])
            row_w = 0.0
        rows[-1].append({"text": txt, "w": w,
                         "fg": t.get("fg") or "#4B5563",
                         "bg": t.get("bg") or "#F1F2F4"})
        row_w += (6 if row_w else 0) + w
    return [r for r in rows if r]


def _rr_points(x0: float, y0: float, x1: float, y1: float, r: float,
               step: float = 3.0) -> List[Tuple[float, float]]:
    """圆角矩形轮廓点（顺时针），给虚线描边用。"""
    r = max(1.0, min(float(r), (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    pts: List[Tuple[float, float]] = []

    def arc(cx: float, cy: float, a0: float, a1: float) -> None:
        n = max(3, int(abs(a1 - a0) * r / step))
        for i in range(n + 1):
            a = math.radians(a0 + (a1 - a0) * i / float(n))
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))

    arc(x1 - r, y0 + r, -90.0, 0.0)
    arc(x1 - r, y1 - r, 0.0, 90.0)
    arc(x0 + r, y1 - r, 90.0, 180.0)
    arc(x0 + r, y0 + r, 180.0, 270.0)
    pts.append(pts[0])
    return pts


def _draw_dashed(draw, pts: Sequence[Tuple[float, float]], color, width: int = 2,
                 dash: float = 9.0, gap: float = 6.0) -> None:
    """沿着一串点画虚线。"""
    if len(pts) < 2:
        return
    on = True
    acc = 0.0
    prev = pts[0]
    seg_start = prev
    for p in pts[1:]:
        acc += math.hypot(p[0] - prev[0], p[1] - prev[1])
        if on and acc >= dash:
            draw.line([seg_start, p], fill=color, width=width)
            on, acc, seg_start = False, 0.0, p
        elif (not on) and acc >= gap:
            on, acc, seg_start = True, 0.0, p
        prev = p
    if on:
        draw.line([seg_start, pts[-1]], fill=color, width=width)


def _dashed_rrect(draw, box, radius: float, color, width: int = 2,
                  dash: float = 9.0, gap: float = 6.0) -> None:
    _draw_dashed(draw, _rr_points(box[0], box[1], box[2], box[3], radius),
                 color, width, dash, gap)


def _shadow(base, box, radius: float, color: Tuple[int, int, int],
            blur: int = 7, alpha: int = 20, dy: int = 3) -> None:
    """卡片软阴影（Pillow 没有 box-shadow，就自己糊一层高斯）。"""
    Image, _, ImageFilter, _ = _pil()  # type: ignore[misc]
    pad = max(3, blur * 2)
    w = (box[2] - box[0]) + pad * 2
    h = (box[3] - box[1]) + pad * 2
    layer = Image.new("RGBA", (max(1, int(w)), max(1, int(h))), (0, 0, 0, 0))
    d = ImageDraw_proxy(layer)
    d.rounded_rectangle(
        [pad, pad, pad + (box[2] - box[0]), pad + (box[3] - box[1])],
        radius=radius, fill=(color[0], color[1], color[2], max(0, min(255, alpha))))
    try:
        layer = layer.filter(ImageFilter.GaussianBlur(max(1, blur)))
    except Exception:
        pass
    base.alpha_composite(layer, (int(box[0] - pad), int(box[1] - pad + dy)))


def ImageDraw_proxy(img):
    _, ImageDraw, _, _ = _pil()  # type: ignore[misc]
    return ImageDraw.Draw(img)


def _png(img) -> bytes:
    """编码 PNG。网格图颜色比文字卡多，先走普通压缩，超 400 KB 再试调色板。"""
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False, compress_level=6)
    data = buf.getvalue()
    if len(data) > 400_000:
        try:
            Image, _, _, _ = _pil()  # type: ignore[misc]
            buf2 = io.BytesIO()
            img.convert("P", palette=Image.ADAPTIVE, colors=256).save(
                buf2, "PNG", optimize=True, compress_level=9)
            if buf2.tell() < len(data):
                data = buf2.getvalue()
        except Exception:
            pass
    return data


# ---------------------------------------------------------------- 主渲染


def _norm_panels(cards: Optional[Sequence[Dict[str, Any]]],
                 panels: Optional[Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """把「单栏 cards」和「双栏 panels」统一成 panels 列表。"""
    if panels:
        out = []
        for p in panels:
            p = p if isinstance(p, dict) else {}
            out.append({
                "title": str(p.get("title") or ""),
                "color": str(p.get("color") or ""),
                "cards": [c for c in (p.get("cards") or []) if isinstance(c, dict)],
            })
        return out or [{"title": "", "color": "", "cards": []}]
    return [{"title": "", "color": "",
             "cards": [c for c in (cards or []) if isinstance(c, dict)]}]


def _lerp_rgb(a, b, t: float):
    t = max(0.0, min(1.0, float(t)))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _vgrad(size, top, bottom):
    """竖向渐变图（宽 1 拉伸，够快）。"""
    Image, _, _, _ = _pil()  # type: ignore[misc]
    w, h = max(1, int(size[0])), max(1, int(size[1]))
    strip = Image.new("RGB", (1, h))
    px = strip.load()
    for y in range(h):
        px[0, y] = _lerp_rgb(top, bottom, y / float(max(1, h - 1)))
    return strip.resize((w, h))


def _hgrad(size, left, right):
    Image, _, _, _ = _pil()  # type: ignore[misc]
    w, h = max(1, int(size[0])), max(1, int(size[1]))
    strip = Image.new("RGB", (w, 1))
    px = strip.load()
    for x in range(w):
        px[x, 0] = _lerp_rgb(left, right, x / float(max(1, w - 1)))
    return strip.resize((w, h))


def _paper_noise(size: int = 96, seed: int = 7):
    """暖纸纹理（稀疏小点，alpha 很低），Pillow 和 HTML 共用同一种颗粒。"""
    Image, ImageDraw, _, _ = _pil()  # type: ignore[misc]
    import random
    rnd = random.Random(seed)
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = im.load()
    for _ in range(size * size // 8):
        x, y = rnd.randrange(size), rnd.randrange(size)
        a = rnd.randint(2, 6)
        tone = rnd.choice(((96, 78, 48), (140, 120, 86), (70, 58, 36)))
        px[x, y] = (tone[0], tone[1], tone[2], a)
    return im


_NOISE_URI_CACHE = ""


def _noise_uri() -> str:
    """纸纹理 → data URI（HTML 里铺在背景上）。"""
    global _NOISE_URI_CACHE
    if _NOISE_URI_CACHE:
        return _NOISE_URI_CACHE
    try:
        im = _paper_noise(72, 11)
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        _NOISE_URI_CACHE = "data:image/png;base64," + base64.b64encode(
            buf.getvalue()).decode("ascii")
    except Exception:
        _NOISE_URI_CACHE = ""
    return _NOISE_URI_CACHE


def _grad_round(img, box, radius: float, c1, c2, *, outline=None,
                width: int = 2, horizontal: bool = False) -> None:
    """圆角矩形内填渐变色（用蒙版裁切）。"""
    Image, ImageDraw, _, _ = _pil()  # type: ignore[misc]
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    grad = (_hgrad((w, h), c1, c2) if horizontal else _vgrad((w, h), c1, c2))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1],
                                           radius=max(0, int(radius)), fill=255)
    img.paste(grad, (x0, y0), mask)
    if outline:
        ImageDraw.Draw(img).rounded_rectangle(
            [x0, y0, x1 - 1, y1 - 1], radius=max(0, int(radius)),
            outline=outline, width=width)


def _star(draw, cx: float, cy: float, r: float, fill,
          outline=None, width: int = 1) -> None:
    pts = []
    for i in range(10):
        ang = math.radians(-90 + i * 36)
        rr = r if i % 2 == 0 else r * 0.46
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    draw.polygon(pts, fill=fill, outline=outline, width=width)


def _diamond(draw, cx: float, cy: float, r: float, fill, outline=None) -> None:
    pts = [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]
    draw.polygon(pts, fill=fill, outline=outline)


def _cloud(img, cx: float, cy: float, s: float, color, alpha: int = 120) -> None:
    """一团软云：几个半透明椭圆叠在一起。"""
    Image, ImageDraw, _, _ = _pil()  # type: ignore[misc]
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    parts = ((-0.72, 0.10, 0.46), (-0.20, -0.12, 0.56),
             (0.34, -0.02, 0.50), (0.78, 0.14, 0.40), (0.02, 0.20, 0.55))
    for dx, dy, rr in parts:
        x = cx + dx * s
        y = cy + dy * s
        r = rr * s
        d.ellipse([x - r, y - r * 0.62, x + r, y + r * 0.62],
                  fill=(color[0], color[1], color[2], max(0, min(255, alpha))))
    img.alpha_composite(layer)


def _bracket(draw, x: float, y: float, sx: int, sy: int,
             size: float, color, width: int = 3) -> None:
    """四角金色包角（sx/sy 是方向，取 -1 或 1）。"""
    draw.line([(x, y), (x + sx * size, y)], fill=color, width=width)
    draw.line([(x, y), (x, y + sy * size)], fill=color, width=width)


def _layout_panels(plist: Sequence[Dict[str, Any]], cols: int,
                   inner_w: int, gap: int, pgap: int) -> List[Dict[str, Any]]:
    """按两栏卡片数量分配列宽：常驻 1 只时左栏只占 1 列，右栏拿 3 列。

    返回 [{panel, cols, width, card_w}]；单栏时 cols 就是总列数。
    """
    active = [p for p in plist if p["cards"]] or [{"title": "", "color": "", "cards": []}]
    if len(active) == 1:
        p = active[0]
        cw = int((inner_w - gap * (cols - 1)) / float(cols))
        return [{"panel": p, "cols": cols, "width": inner_w, "card_w": cw}]

    p0, p1 = active[0], active[1]
    n0, n1 = len(p0["cards"]), len(p1["cards"])
    total = max(1, n0 + n1)
    c0 = int(round(cols * n0 / float(total)))
    c0 = max(1, min(cols - 1, c0))
    c1 = cols - c0
    cw = int((inner_w - gap * (cols - 2) - pgap) / float(cols))
    w0 = c0 * cw + (c0 - 1) * gap
    w1 = inner_w - w0 - pgap          # 末栏补齐，避免 1~2px 缝
    return [
        {"panel": p0, "cols": c0, "width": w0, "card_w": cw},
        {"panel": p1, "cols": c1, "width": w1, "card_w": cw},
    ]


def render_grid(
    *,
    header: str,
    cards: Optional[Sequence[Dict[str, Any]]] = None,
    panels: Optional[Sequence[Dict[str, Any]]] = None,
    subtitle: str = "",
    footer: str = "",
    page: int = 1,
    total_pages: int = 1,
    total: int = 0,
    width: int = WIDTH,
    cols: int = COLS,
    font_size: int = 18,
    font_path: str = "",
    theme: Any = "",
    empty_text: str = "这里还没有登记到精灵",
) -> bytes:
    """画一张「暖纸图鉴」网格图，返回 PNG 字节。

    参考 rocom / gamewiki 的做法：暖纸底 + 白磨砂卡 + 单一金色强调，
    不再给每张卡整张上系别色，也不再堆金色缎带/包角/多层描边。
    和 templates/grid.html 共用同一份数据与尺寸公式。
    """
    pil = _pil()
    if not pil:
        raise RuntimeError("没装 Pillow")
    Image, ImageDraw, _, _ = pil  # type: ignore[misc]

    th = parse_theme(theme)
    W = max(560, int(width))
    cols = max(1, min(6, int(cols or COLS)))
    plist = _norm_panels(cards, panels)
    all_cards = [c for p in plist for c in p["cards"]]

    base = max(10, int(font_size or 18))
    fs_head = base + 16
    fs_sub_head = max(11, base - 5)
    fs_name = base + 1
    fs_sub = max(10, base - 5)
    fs_tag = max(9, base - 7)
    fs_foot = max(10, base - 6)
    fs_panel = base + 2

    f_head = _font(font_path, fs_head, bold=True)
    f_sub_head = _font(font_path, fs_sub_head)
    f_name = _font(font_path, fs_name, bold=True)
    f_small = _font(font_path, fs_sub)
    f_tag = _font(font_path, fs_tag)
    f_foot = _font(font_path, fs_foot)
    f_panel = _font(font_path, fs_panel, bold=True)

    # ---------------- 版式尺寸（HTML 模板按同一组数字） ----------------
    M = 26
    TOP_H = int(max(88, fs_head + 62))
    GAP = 14
    PGAP = 26

    inner_w = W - M * 2
    lays = _layout_panels(plist, cols, inner_w, GAP, PGAP)
    two = len(lays) == 2
    card_w = lays[0]["card_w"]
    per_cols = max(l["cols"] for l in lays)
    avatar_d = AVATAR if per_cols <= 4 else 84

    pill_h = max(18, fs_tag + 9)
    _scratch = Image.new("RGB", (8, 8), (255, 255, 255))
    _sd = ImageDraw.Draw(_scratch)
    tag_rows_max = 1
    for c in all_cards:
        n = len(_tag_rows(_sd, c.get("tags") or [], f_tag, card_w - 20))
        tag_rows_max = max(tag_rows_max, min(2, n))
    card_h = card_height(base, avatar_d, tag_rows_max)

    prows = [max(1, int(math.ceil(max(1, len(l["panel"]["cards"])) / float(l["cols"]))))
             for l in lays]
    rows = max(prows) if prows else 1
    panel_top = 30 if any(l["panel"]["title"] for l in lays) else 0

    board_top = TOP_H + 22
    grid_top = board_top + panel_top
    board_h = rows * card_h + (rows - 1) * GAP
    if not all_cards:
        board_h = 160
    foot_h = 34 if (footer or total_pages > 1) else 0
    H = int(grid_top + board_h + (14 + foot_h if foot_h else 0) + M)

    # ---------------- 暖纸底 ----------------
    img = _vgrad((W, H), th["天空顶"], th["天空底"]).convert("RGBA")
    # 纸纹（低 alpha 平铺）
    tex = _paper_noise(96, 7)
    for ty in range(0, H, tex.height):
        for tx in range(0, W, tex.width):
            img.alpha_composite(tex, (tx, ty))
    draw = ImageDraw.Draw(img)

    # ---------------- 顶部金色横幅（满幅） ----------------
    _grad_round(img, (-2, -2, W + 2, TOP_H), 0, th["星"], th["强调"],
                horizontal=True)
    # 幽灵字：在横幅内裁切，不压正文
    _ghost = "ROCO KINGDOM"
    _gs = max(24, min(int(TOP_H * 0.82), 86))
    _gf = _font(font_path, _gs, bold=True)
    _gw = _tw(draw, _ghost, _gf)
    if _gw > W * 0.72:
        _gs = max(18, int(_gs * W * 0.72 / max(1.0, _gw)))
        _gf = _font(font_path, _gs, bold=True)
        _gw = _tw(draw, _ghost, _gf)
    _gl = Image.new("RGBA", (W, TOP_H), (0, 0, 0, 0))
    ImageDraw.Draw(_gl).text((W - 24 - _gw, TOP_H - _gs - 2), _ghost,
                             font=_gf, fill=(255, 255, 255, 40))
    img.alpha_composite(_gl)

    # 标题 / 副标题
    draw.text((M, 22), _ellipsis(draw, str(header or ""), f_head, inner_w - 170),
              font=f_head, fill=th["标题"])
    if str(subtitle or "").strip():
        draw.text((M + 2, 22 + fs_head + 4),
                  _ellipsis(draw, str(subtitle), f_sub_head, inner_w - 170),
                  font=f_sub_head, fill=(74, 64, 50))

    # 右侧「共 N 只」胶囊
    _cnt = int(total or len(all_cards))
    _pt = f"共 {_cnt} 只"
    _ptw = _tw(draw, _pt, f_sub_head) + 26
    _ph = fs_sub_head + 16
    _px1 = W - M
    _px0 = _px1 - _ptw
    _py0 = 26
    draw.rounded_rectangle([_px0, _py0, _px1, _py0 + _ph],
                           radius=_ph / 2.0, fill=(255, 255, 255, 120),
                           outline=(255, 255, 255, 170), width=1)
    draw.text((_px0 + 13, _py0 + (_ph - fs_sub_head) / 2.0 - 2),
              _pt, font=f_sub_head, fill=(92, 66, 20))

    if not all_cards:
        _ey = grid_top + 74
        _q_r = 30
        _qx = W / 2.0
        draw.ellipse([_qx - _q_r, _ey - _q_r - 26, _qx + _q_r, _ey + _q_r - 26],
                     fill=th["卡片"], outline=th["边框"], width=1)
        _qw = _tw(draw, "?", f_head)
        draw.text((_qx - _qw / 2.0, _ey - fs_head - 24), "?",
                  font=f_head, fill=th["空白"])
        _ew = _tw(draw, empty_text, f_name)
        draw.text(((W - _ew) / 2.0, _ey + 16), empty_text,
                  font=f_name, fill=th["空白"])
        draw.text(((W - _tw(draw, "去查一只精灵，把第一枚蛋登记进来吧", f_small)) / 2.0,
                   _ey + 44), "去查一只精灵，把第一枚蛋登记进来吧",
                  font=f_small, fill=th["空白"])

    # ---------------- 每栏 ----------------
    _px_cursor = M
    for _li, lay in enumerate(lays):
        panel = lay["panel"]
        px0 = _px_cursor
        _pcols = lay["cols"]
        panel_w = lay["width"]
        _px_cursor += panel_w + PGAP
        if panel["title"]:
            pcol = _to_rgb(panel.get("color") or "") or th["强调"]
            _dot = 8
            _dy = board_top + 11
            draw.ellipse([px0, _dy, px0 + _dot, _dy + _dot], fill=pcol)
            draw.text((px0 + _dot + 8, board_top + 2), str(panel["title"]),
                      font=f_panel, fill=th["标题"])
            _ct = f"{len(panel['cards'])} 只"
            _ctw = _tw(draw, _ct, f_tag)
            draw.text((px0 + panel_w - _ctw, board_top + 8), _ct,
                      font=f_tag, fill=th["次要"])

        for i, c in enumerate(panel["cards"]):
            r, cc = divmod(i, _pcols)
            x0 = px0 + cc * (card_w + GAP)
            y0 = grid_top + r * (card_h + GAP)
            x1, y1 = x0 + card_w, y0 + card_h
            c = c if isinstance(c, dict) else {}

            accent = _to_rgb(c.get("elem_color") or "") or th["强调"]
            soft = _mix(accent, th["卡片"], 0.84)
            ink = _mix(accent, (44, 36, 24), 0.34)

            _shadow(img, (x0, y0, x1, y1), CARD_RADIUS, th["阴影"],
                    blur=6, alpha=14, dy=2)
            draw.rounded_rectangle([x0, y0, x1, y1], radius=CARD_RADIUS,
                                   fill=th["卡片"], outline=th["边框"], width=1)

            # 顶部小行：系别小胶囊 + 区间
            _ch_h = 19
            _chip_x = x0 + 12
            if str(c.get("elem") or "").strip():
                _et = str(c.get("elem"))
                _ef = _font(font_path, fs_tag, bold=True)
                _ew = int(_tw(draw, _et, _ef) + 16)
                draw.rounded_rectangle([_chip_x, y0 + 11, _chip_x + _ew,
                                        y0 + 11 + _ch_h],
                                       radius=6, fill=soft)
                draw.text((_chip_x + 8, y0 + 11 + 3), _et, font=_ef, fill=ink)
            if c.get("bins") not in (None, ""):
                _bt = f"区间 {c.get('bins')}"
                _bw = _tw(draw, _bt, f_tag)
                draw.text((x1 - 12 - _bw, y0 + 14), _bt, font=f_tag,
                          fill=th["次要"])

            # 头像：细系别圈 + 奶油底
            cx = (x0 + x1) / 2.0
            ay = y0 + 38
            _acy = ay + avatar_d / 2.0
            draw.ellipse([cx - avatar_d / 2.0 - 3, _acy - avatar_d / 2.0 - 3,
                          cx + avatar_d / 2.0 + 3, _acy + avatar_d / 2.0 + 3],
                         fill=accent)
            draw.ellipse([cx - avatar_d / 2.0, _acy - avatar_d / 2.0,
                          cx + avatar_d / 2.0, _acy + avatar_d / 2.0],
                         fill=th["头像底"])
            ic = _fit_icon(str(c.get("icon") or ""), int(avatar_d * 0.84)) \
                if c.get("icon") else None
            if ic is not None:
                _paste_center(img, ic, int(cx), int(_acy))
            else:
                _qw = _tw(draw, "?", f_head)
                draw.text((cx - _qw / 2.0, _acy - fs_head * 0.62), "?",
                          font=f_head, fill=th["空白"])

            # 蛋徽标：头像右下角最多两枚
            for k, s in enumerate(list(c.get("sats") or [])[:2]):
                rr = SAT / 2.0
                sx2 = cx + avatar_d / 2.0 - 8 - k * 24
                sy2 = ay + avatar_d - 8
                draw.ellipse([sx2 - rr, sy2 - rr, sx2 + rr, sy2 + rr],
                             fill=th["卡片"], outline=th["边框"], width=1)
                sim = _fit_icon(str((s or {}).get("icon") or ""), int(SAT * 0.74))
                if sim is not None:
                    _paste_center(img, sim, int(sx2), int(sy2))

            # 名字 / 小字
            ny = ay + avatar_d + 8
            name = _ellipsis(draw, str(c.get("name") or ""), f_name, card_w - 22)
            nw = _tw(draw, name, f_name)
            draw.text((cx - nw / 2.0, ny), name, font=f_name, fill=th["标题"])
            sy2 = ny + int(fs_name * 1.30) + 2
            sub = _ellipsis(draw, str(c.get("sub") or ""), f_small, card_w - 22)
            if sub:
                sw = _tw(draw, sub, f_small)
                draw.text((cx - sw / 2.0, sy2), sub, font=f_small, fill=th["次要"])

            # 星光 / 回顾：一行小字 + 图标，不做胶囊
            _facts = []
            if c.get("star") not in (None, "", 0):
                _facts.append((str(c.get("star")), str(c.get("star_img") or "")))
            if c.get("review") not in (None, "", 0):
                _facts.append((str(c.get("review")), str(c.get("review_img") or "")))
            fy = sy2 + int(fs_sub * 1.35) + 4
            if _facts:
                _parts = []
                for _t, _ip in _facts:
                    _iw = 14 if _ip else 0
                    _parts.append((_t, _ip, int(_tw(draw, _t, f_tag) + _iw + 2)))
                _tot = sum(p[2] for p in _parts) + 10 * (len(_parts) - 1)
                _fx = cx - _tot / 2.0
                for _t, _ip, _wd in _parts:
                    if _ip:
                        _fic = _fit_icon(_ip, 13)
                        if _fic is not None:
                            _paste_center(img, _fic, int(_fx + 6), int(fy + 8))
                    draw.text((_fx + (15 if _ip else 0), fy), _t, font=f_tag,
                              fill=th["正文"])
                    _fx += _wd + 10

            # 标签：浅奶油小胶囊，最多两行
            ty = fy + 18 + 4
            rows_tags = _tag_rows(draw, c.get("tags") or [], f_tag, card_w - 20)
            for ri, row in enumerate(rows_tags[:2]):
                total_w = sum(x["w"] for x in row) + 6 * (len(row) - 1)
                tx = cx - total_w / 2.0
                yy = ty + ri * (pill_h + 4)
                if yy + pill_h > y1 - 6:
                    break
                for x in row:
                    fg = _to_rgb(x["fg"]) or th["次要"]
                    bgc = _to_rgb(x["bg"]) or th["标签底"]
                    draw.rounded_rectangle([tx, yy, tx + x["w"], yy + pill_h],
                                           radius=pill_h / 2.0, fill=bgc)
                    tw_ = _tw(draw, x["text"], f_tag)
                    draw.text((tx + (x["w"] - tw_) / 2.0, yy + 3), x["text"],
                              font=f_tag, fill=fg)
                    tx += x["w"] + 6

    # 双栏分隔：一根很淡的竖线
    if two:
        dx = M + lays[0]["width"] + PGAP / 2.0
        draw.line([(dx, board_top + 2), (dx, grid_top + board_h)],
                  fill=th["网格线"], width=1)

    # ---------------- 页脚 ----------------
    if foot_h:
        _fy = grid_top + board_h + 14
        draw.line([(M, _fy), (W - M, _fy)], fill=th["网格线"], width=1)
        _brand = str(footer or "").strip() or "洛克王国 · 精灵图鉴"
        draw.text((M, _fy + 10), _ellipsis(draw, _brand, f_foot, inner_w - 160),
                  font=f_foot, fill=th["次要"])
        _pg = f"{page} / {total_pages}"
        _pgw = _tw(draw, _pg, f_foot)
        draw.text((W - M - _pgw, _fy + 10), _pg, font=f_foot, fill=th["空白"])

    return _png(img)


# ---------------------------------------------------------------- HTML 引擎用


def html_data(
    *,
    header: str,
    cards: Optional[Sequence[Dict[str, Any]]] = None,
    panels: Optional[Sequence[Dict[str, Any]]] = None,
    subtitle: str = "",
    footer: str = "",
    page: int = 1,
    total_pages: int = 1,
    total: int = 0,
    width: int = WIDTH,
    cols: int = COLS,
    font_size: int = 18,
    font_path: str = "",
    theme: Any = "",
) -> Dict[str, Any]:
    """把同一份数据整理成 `templates/grid.html` 的 Jinja2 上下文。

    图标一律转成 `data:image/png;base64,...` 内联 —— Playwright 截图的页面
    读不到本地文件路径（沙箱/跨目录都会 403），内联是最稳的做法。
    """
    th = parse_theme(theme)
    cols = max(1, min(6, int(cols or COLS)))
    base = max(10, int(font_size or 18))
    plist = _norm_panels(cards, panels)
    all_cards = [c for p in plist for c in p["cards"]]

    # 和 render_grid 同一组版式常量（HTML 直接吃这些数字，两条引擎才不会飘）
    M = 26
    GAP = 14
    PGAP = 26
    W = max(560, int(width))
    inner_w = W - M * 2
    lays = _layout_panels(plist, cols, inner_w, GAP, PGAP)
    two = len(lays) == 2
    per_cols = max(l["cols"] for l in lays)
    card_w = lays[0]["card_w"]
    avatar_d = AVATAR if per_cols <= 4 else 84
    fs_head = base + 16
    top_h = int(max(88, fs_head + 62))
    panel_top = 30 if any(l["panel"]["title"] for l in lays) else 0
    fs_tag = max(9, base - 7)
    pill_h = max(18, fs_tag + 9)

    # 标签要占几行：能测就真测（和 Pillow 版同一套 _tag_rows），测不了按 1 行算
    tag_rows_max = 1
    pil = _pil()
    if pil is not None:
        try:
            Image, ImageDraw, _, _ = pil  # type: ignore[misc]
            scratch = Image.new("RGB", (8, 8), (255, 255, 255))
            sd = ImageDraw.Draw(scratch)
            f_tag = _font(font_path, fs_tag)
            for c in all_cards:
                c = c if isinstance(c, dict) else {}
                n_rows = len(_tag_rows(sd, c.get("tags") or [], f_tag, card_w - 20))
                tag_rows_max = max(tag_rows_max, min(2, n_rows))
        except Exception:
            tag_rows_max = 1

    def pack(cards_in: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out_cards: List[Dict[str, Any]] = []
        for c in cards_in:
            c = c if isinstance(c, dict) else {}
            sats: List[Dict[str, str]] = []
            for s in list(c.get("sats") or [])[:4]:
                sats.append({
                    "img": data_uri(str((s or {}).get("icon") or "")),
                    "text": str((s or {}).get("text") or ""),
                    "color": str((s or {}).get("color") or "#4A4642"),
                    "ring": str((s or {}).get("ring") or ""),
                })
            tags = []
            for t in (c.get("tags") or []):
                t = t if isinstance(t, dict) else {}
                if str(t.get("text") or "").strip():
                    tags.append({"text": str(t["text"]),
                                 "fg": str(t.get("fg") or "#4B5563"),
                                 "bg": str(t.get("bg") or "#F1F2F4")})
            accent = _to_rgb(c.get("elem_color") or "")
            if _to_rgb(c.get("bg") or ""):
                bg_hex = str(c["bg"])
            elif accent:
                bg_hex = "#%02X%02X%02X" % _mix(accent, (255, 255, 255), 0.80)
            else:
                bg_hex = "#%02X%02X%02X" % th["卡片"]
            if _to_rgb(c.get("border") or ""):
                line_hex = str(c["border"])
            elif accent:
                line_hex = "#%02X%02X%02X" % _mix(accent, (255, 255, 255), 0.50)
            else:
                line_hex = "#%02X%02X%02X" % th["边框"]
            ring_hex = ("#%02X%02X%02X" % accent) if accent else str(
                c.get("ring") or RING_PALETTE[0])
            elem_bg = ("#%02X%02X%02X" % accent) if accent else ""
            elem_soft = ("#%02X%02X%02X" % _mix(accent, th["卡片"], 0.84)) if accent else ""
            elem_ink = ("#%02X%02X%02X" % _mix(accent, (44, 36, 24), 0.34)) if accent else ""
            out_cards.append({
                "name": str(c.get("name") or ""),
                "sub": str(c.get("sub") or ""),
                "elem_bg": elem_bg,
                "sub_fg": str(c.get("sub_fg") or ""),
                "ring": ring_hex,
                "bg": bg_hex,
                "border": line_hex,
                "elem": str(c.get("elem") or ""),
                "elem_color": "#%02X%02X%02X" % accent if accent else "",
                "elem_fg": ("#%02X%02X%02X" % _fg_on(accent)) if accent else "",
                "elem_soft": elem_soft,
                "elem_ink": elem_ink,
                "bins": c.get("bins"),
                "bins_bg": ("#%02X%02X%02X" % accent) if accent else "",
                "img": data_uri(str(c.get("icon") or "")),
                "tags": tags,
                "sats": sats,
                # 星光值 / 回顾值：名字下方胶囊（图标转 data URI 内联，
                # 以前这里直接塞本地路径，Playwright 沙箱读不到，HTML 版就丢图标）
                "star": int(c.get("star") or 0),
                "review": int(c.get("review") or 0),
                "star_img": data_uri(str(c.get("star_img") or "")),
                "review_img": data_uri(str(c.get("review_img") or "")),
            })
        return out_cards

    out_panels = []
    for lay in lays:
        p = lay["panel"]
        out_panels.append({
            "title": p["title"],
            "color": p["color"] or "#E8A72C",
            "count": len(p["cards"]),
            "cols": lay["cols"],
            "width": lay["width"],
            "cards": pack(p["cards"]),
        })

    return {
        "header": header,
        "subtitle": subtitle,
        "footer": footer,
        "page": page,
        "total_pages": total_pages,
        "cards": [c for p in out_panels for c in p["cards"]],
        "panels": out_panels,
        "two_panels": two,
        "width": int(width),
        "cols": cols,
        "per_cols": per_cols,
        "base": base,
        "card_h": card_height(base, avatar_d, tag_rows_max),
        "empty_text": "这里还没有登记到精灵",
        "m": M,
        "gap": GAP,
        "pgap": PGAP,
        "top_h": top_h,
        "panel_top": panel_top,
        "pill_h": pill_h,
        "avatar_d": avatar_d,
        "card_radius": CARD_RADIUS,
        "total_count": int(total or len(all_cards)),
        "noise": _noise_uri(),
        "color": {k: "#%02X%02X%02X" % v for k, v in th.items()},
    }



def data_uri(path: str) -> str:
    """本地图片 → data URI（读不到就返回空串，模板那边按占位处理）。"""
    p = str(path or "")
    if not p:
        return ""
    if p.startswith("data:"):
        return p
    try:
        f = Path(p)
        if not f.is_file():
            return ""
        raw = f.read_bytes()
        if len(raw) > 400_000:      # 太大的图不放内联，免得 HTML 撑爆
            return ""
        mime = "image/png"
        if raw[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            mime = "image/webp"
        elif raw[:6] in (b"GIF87a", b"GIF89a"):
            mime = "image/gif"
        return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        return ""
