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

#: 头像直径
AVATAR = 100

#: 卡片圆角 / 容器圆角
CARD_RADIUS = 20
BOX_RADIUS = 18

#: 环绕徽标直径
SAT = 36

# ---------------------------------------------------------------- 配色

#: 默认配色。前 8 个和 card.py 同名同义（card_theme 配置直接生效），
#: 后 5 个是网格图专用（grid_theme 配置覆盖）。
#: 底色取自参考图的暖米色（#F4F0E7 一带），卡片纯白 + 细边 + 软阴影分层。
THEME_DEFAULT: Dict[str, str] = {
    "背景": "#F4F0E7",      # 草稿纸米色
    "卡片": "#FFFFFF",
    "边框": "#E7E0D3",
    "标题": "#2C2C2C",
    "正文": "#4A4642",
    "次要": "#8C867E",
    "强调": "#C25828",
    "标签底": "#F4F4F4",
    "网格线": "#D2CBBA",    # 虚线容器 / 虚线分割线
    "容器底": "#FFFEFB",
    "头像底": "#F1EDE3",
    "阴影": "#3C3228",
    "空白": "#B8B2A8",
}

#: 标签类型 → (文字色, 底色)。多色标签就是靠这张表。
TAG_STYLE: Dict[str, Tuple[str, str]] = {
    "shiny":   ("#6D28D9", "#F3E8FF"),   # 异色 —— 紫
    "shinyegg": ("#00796B", "#DFF3F0"),  # 异色蛋 —— 青
    "leader":  ("#B4430F", "#FFEEDF"),   # 首领化 —— 橙
    "group":   ("#4B5563", "#F1F2F4"),   # 蛋组 —— 灰
    "season":  ("#1D4ED8", "#E4ECFF"),   # 赛季 —— 蓝
    "dex":     ("#6B7280", "#F4F4F5"),   # 图鉴号 —— 灰
    "special": ("#0E7490", "#E0F2FE"),   # 特殊途径 —— 天蓝
    "ride":    ("#166534", "#DCFCE7"),   # 可同乘 —— 绿
    "noride":  ("#8A8178", "#F3F1EC"),   # 不可同乘 —— 浅灰
}

#: 蛋组 → 外圈颜色。同组恒定（同一个蛋组在所有页都是同一个颜色）
RING_PALETTE: Tuple[str, ...] = (
    "#5C7AEA", "#2EA18F", "#E28C3C", "#A86CD0",
    "#5A965A", "#D05C78", "#488CBE", "#C4A848",
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
         bins: Optional[int] = None) -> Dict[str, Any]:
    """造一张卡片。tags 可以传 dict（tag() 造的）、(文字, 类型) 元组或纯文字。

    elem / elem_color —— 系别文字（如 `龙/翼`）和系别主色（如 `#e64c62`）。
    给了系别色，卡片底色和头像外圈就跟着它走；bg / border 可以再显式指定。
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
    """按比例把图标缩进 box×box（不变形），返回 RGBA 图或 None。"""
    Image, _, _, _ = _pil()  # type: ignore[misc]
    try:
        im = Image.open(str(path)).convert("RGBA")
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
    """卡片高度（两条引擎共用这个公式，HTML 的 .pet 也按它定高，版式才不会飘）。"""
    base = max(10, int(base or 18))
    fs_name = base + 2
    fs_sub = max(10, base - 5)
    fs_tag = max(9, base - 6)
    pill_h = fs_tag + 12
    tag_rows = max(1, min(2, int(tag_rows or 1)))
    return int(16 + avatar_d + 12 + fs_name + 6 + fs_sub + 10
               + tag_rows * pill_h + (tag_rows - 1) * 4 + 16)


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
    """画一张网格图，返回 PNG 字节。

    header   —— 左上角大字（例如 `异色精灵 · S5 · 圣龙宝藏`）
    subtitle —— 右上角小字（例如 `第 1 / 3 页 · 共 39 只`）
    footer   —— 底部一行提示（翻页 / 图例 / 数据来源）
    panels   —— 两栏（左常驻 / 右奇遇）：`[{"title","color","cards"}, …]`
                给两个 panel 时中间画一条竖虚线分开，每栏各占一半、两列卡片
    """
    pil = _pil()
    if not pil:
        raise RuntimeError("没装 Pillow")
    Image, ImageDraw, _, _ = pil  # type: ignore[misc]

    th = parse_theme(theme)
    bg = th["背景"]
    W = max(560, int(width))
    cols = max(1, min(6, int(cols or COLS)))
    plist = _norm_panels(cards, panels)
    two = len(plist) == 2
    per_cols = max(1, cols // 2) if two else cols
    all_cards = [c for p in plist for c in p["cards"]]

    base = max(10, int(font_size or 18))
    fs_head = base + 9          # 标题
    fs_name = base + 2          # 卡片名
    fs_sub = max(10, base - 5)  # 卡片小字
    fs_tag = max(9, base - 6)   # 标签
    fs_foot = max(10, base - 4)  # 页脚
    fs_panel = max(11, base)     # 栏目标题（常驻 / 奇遇）

    f_head = _font(font_path, fs_head, bold=True)
    f_sub = _font(font_path, max(10, base - 4))
    f_name = _font(font_path, fs_name, bold=True)
    f_small = _font(font_path, fs_sub)
    f_tag = _font(font_path, fs_tag)
    f_foot = _font(font_path, fs_foot)
    f_sat = _font(font_path, max(9, fs_tag))
    f_panel = _font(font_path, fs_panel, bold=True)

    PAD = 26
    GAP = 18
    INNER = 18
    DIVW = 34 if two else 0        # 中间分隔带宽度

    # 卡片宽度：按「每栏几列」算
    IW = W - PAD * 2 - INNER * 2
    panel_w = int((IW - DIVW) / 2) if two else IW
    avatar_d = AVATAR if per_cols <= 4 else 86
    card_w = int((panel_w - GAP * (per_cols - 1)) / float(per_cols))

    # 标签要占几行（最多两行）→ 卡片高度
    pill_h = fs_tag + 12
    _scratch_img = Image.new("RGB", (8, 8), (255, 255, 255))
    _sdraw = ImageDraw.Draw(_scratch_img)
    tag_rows_max = 1
    for c in all_cards:
        n_rows = len(_tag_rows(_sdraw, c.get("tags") or [], f_tag, card_w - 16))
        tag_rows_max = max(tag_rows_max, min(2, n_rows))
    card_h = card_height(base, avatar_d, tag_rows_max)

    prows = [max(1, int(math.ceil(max(1, len(p["cards"])) / float(per_cols))))
             for p in plist]
    rows = max(prows) if prows else 1
    panel_top = (fs_panel + 12) if any(p["title"] for p in plist) else 0

    head_h = int(fs_head + 14 + 12)
    foot_lines = 1 if footer else 0
    foot_h = int(fs_foot + 8) * foot_lines
    box_h = INNER * 2 + panel_top + rows * card_h + (rows - 1) * GAP
    H = int(PAD + head_h + box_h + (foot_h + 10 if foot_h else 0) + PAD)

    img = Image.new("RGBA", (int(W), int(H)), (bg[0], bg[1], bg[2], 255))
    draw = ImageDraw.Draw(img)

    # ---------------- 表头 ----------------
    hy = PAD
    left_limit = W - PAD * 2 - (140 if subtitle else 0)
    draw.text((PAD, hy), _ellipsis(draw, header, f_head, left_limit),
              font=f_head, fill=th["标题"])
    if subtitle:
        sw = _tw(draw, subtitle, f_sub)
        draw.text((W - PAD - sw, hy + (fs_head - fs_sub) / 2.0 + 2),
                  subtitle, font=f_sub, fill=th["次要"])
    dvy = hy + fs_head + 14
    _draw_dashed(draw, [(PAD, dvy), (W - PAD, dvy)], th["网格线"], 2, 9, 6)

    # ---------------- 容器 ----------------
    bx0, by0 = PAD, dvy + 12
    bx1, by1 = W - PAD, by0 + box_h
    draw.rounded_rectangle([bx0, by0, bx1, by1], radius=BOX_RADIUS,
                           fill=(th["容器底"][0], th["容器底"][1], th["容器底"][2], 255))

    if not all_cards:
        cy = by0 + box_h / 2.0
        ew = _tw(draw, empty_text, f_name)
        draw.text(((W - ew) / 2.0, cy - fs_name / 2.0), empty_text,
                  font=f_name, fill=th["空白"])

    # ---------------- 每栏 ----------------
    for pi, panel in enumerate(plist):
        px0 = bx0 + INNER + pi * (panel_w + DIVW)
        if panel["title"]:
            pcol = _to_rgb(panel.get("color") or "") or th["强调"]
            ty0 = by0 + INNER + (panel_top - fs_panel) / 2.0 - 2
            dot_r = max(4, fs_panel // 4)
            draw.ellipse([px0 + 2, ty0 + fs_panel / 2.0 - dot_r,
                          px0 + 2 + dot_r * 2, ty0 + fs_panel / 2.0 + dot_r], fill=pcol)
            draw.text((px0 + 8 + dot_r * 2, ty0), panel["title"],
                      font=f_panel, fill=pcol)
            cnt = f"{len(panel['cards'])} 只"
            cw_ = _tw(draw, cnt, f_small)
            draw.text((px0 + panel_w - cw_, ty0 + 3), cnt, font=f_small, fill=th["次要"])

        for i, c in enumerate(panel["cards"]):
            r, cc = divmod(i, per_cols)
            x0 = px0 + cc * (card_w + GAP)
            y0 = by0 + INNER + panel_top + r * (card_h + GAP)
            x1, y1 = x0 + card_w, y0 + card_h
            c = c if isinstance(c, dict) else {}

            _shadow(img, (x0, y0, x1, y1), CARD_RADIUS, th["阴影"], blur=6, alpha=22, dy=2)
            # 系别色：卡片底色 / 描边 / 头像外圈都跟着系别走（调用方给了 bg/border 就用它）
            accent = _to_rgb(c.get("elem_color") or "")
            card_fill = (_to_rgb(c.get("bg") or "")
                         or (_mix(accent, (255, 255, 255), 0.80) if accent else th["卡片"]))
            card_line = (_to_rgb(c.get("border") or "")
                         or (_mix(accent, (255, 255, 255), 0.50) if accent else th["边框"]))
            draw.rounded_rectangle([x0, y0, x1, y1], radius=CARD_RADIUS,
                                   fill=(card_fill[0], card_fill[1], card_fill[2], 255),
                                   outline=card_line, width=1)

            cx = (x0 + x1) / 2.0
            ring = accent or (_to_rgb(c.get("ring") or "") or th["强调"])

            # 头像圈：外圈主题色 + 内圈浅底 + 图标按比例缩放
            ay = y0 + 16
            ax = cx - avatar_d / 2.0
            draw.ellipse([ax - 3, ay - 3, ax + avatar_d + 3, ay + avatar_d + 3], fill=ring)
            draw.ellipse([ax, ay, ax + avatar_d, ay + avatar_d], fill=th["头像底"])
            icon_px = int(avatar_d * 0.82)
            ic = _fit_icon(str(c.get("icon") or ""), icon_px) if c.get("icon") else None
            if ic is not None:
                _paste_center(img, ic, int(cx), int(ay + avatar_d / 2.0))
            else:
                qw = _tw(draw, "?", f_head)
                draw.text((cx - qw / 2.0, ay + avatar_d / 2.0 - fs_head * 0.62),
                          "?", font=f_head, fill=th["空白"])

            # 环绕徽标：45°/135°/225°/315° 四个方位（最多 4 个）
            sats = list(c.get("sats") or [])[:4]
            if sats:
                sx, sy = cx, ay + avatar_d / 2.0
                # 徽标贴在精灵图**下半部**（和右下角区间数同一片区域），不再浮在图的上沿
                offs = [(-0.24, 0.06), (0.24, 0.06), (-0.24, 0.30), (0.24, 0.30)]
                for k, s in enumerate(sats):
                    ux, uy = offs[min(k, 3)]
                    px = sx + ux * avatar_d
                    py = sy + uy * avatar_d
                    rr = SAT / 2.0
                    sat_ring = _to_rgb((s or {}).get("ring") or "") or ring
                    draw.ellipse([px - rr, py - rr, px + rr, py + rr],
                                 fill=(255, 255, 255, 255), outline=sat_ring, width=2)
                    s_icon = str((s or {}).get("icon") or "")
                    s_text = str((s or {}).get("text") or "")
                    s_color = _to_rgb((s or {}).get("color") or "") or th["正文"]
                    sim = _fit_icon(s_icon, int(SAT * 0.72)) if s_icon else None
                    if sim is not None:
                        _paste_center(img, sim, int(px), int(py))
                    elif s_text:
                        tw_ = _tw(draw, s_text, f_sat)
                        draw.text((px - tw_ / 2.0, py - fs_tag * 0.68), s_text,
                                  font=f_sat, fill=s_color)

            # 系别标签：贴在精灵图**左下角**（骑在圆圈边上，像给图角盖的章）
            elem_txt = str(c.get("elem") or "").strip()
            if elem_txt:
                f_el = _font(font_path, max(9, fs_tag), bold=True)
                bh = max(16, fs_tag + 8)
                bw = int(_tw(draw, elem_txt, f_el) + 12)
                # 章用系别色的**深一档**（主色留给外圈），加粗白边 —— 这样章和外圈
                # 分得开，不会糊成一坨让人以为头像不圆
                ecol = accent or th["强调"]
                ecx = cx - avatar_d * 0.38
                ecy = ay + avatar_d * 0.92
                ex0, ey0 = ecx - bw / 2.0, ecy - bh / 2.0
                draw.rounded_rectangle([ex0, ey0, ex0 + bw, ey0 + bh], radius=bh / 2.0,
                                       fill=ecol,
                                       outline=(card_fill[0], card_fill[1], card_fill[2], 255),
                                       width=3)
                etw = _tw(draw, elem_txt, f_el)
                draw.text((ex0 + (bw - etw) / 2.0, ey0 + bh / 2.0 - fs_tag * 0.66),
                          elem_txt, font=f_el, fill=_fg_on(ecol))

            # 区间数：精灵图**右上角**，和系别章同一套胶囊样式
            bins_n = c.get("bins")
            if bins_n not in (None, ""):
                btxt = f"区间 {bins_n}"
                f_b = _font(font_path, max(9, fs_tag), bold=True)
                bh2 = max(16, fs_tag + 8)
                bw2 = int(_tw(draw, btxt, f_b) + 12)
                bcol = accent or th["强调"]                          # 和系别章同色
                bcx = cx + avatar_d * 0.38
                bcy = ay + avatar_d * 0.92
                pbx = bcx - bw2 / 2.0
                pby = bcy - bh2 / 2.0
                draw.rounded_rectangle([pbx, pby, pbx + bw2, pby + bh2],
                                       radius=bh2 / 2.0, fill=bcol,
                                       outline=(card_fill[0], card_fill[1], card_fill[2], 255),
                                       width=3)
                btw = _tw(draw, btxt, f_b)
                draw.text((pbx + (bw2 - btw) / 2.0, pby + bh2 / 2.0 - fs_tag * 0.66),
                          btxt, font=f_b, fill=_fg_on(bcol))

            # 名字 / 小字
            ny = ay + avatar_d + 12
            name = _ellipsis(draw, str(c.get("name") or ""), f_name, card_w - 24)
            nw = _tw(draw, name, f_name)
            draw.text((cx - nw / 2.0, ny), name, font=f_name, fill=th["标题"])
            sy2 = ny + fs_name + 6
            sub = _ellipsis(draw, str(c.get("sub") or ""), f_small, card_w - 24)
            if sub:
                sw = _tw(draw, sub, f_small)
                draw.text((cx - sw / 2.0, sy2), sub, font=f_small,
                          fill=(_to_rgb(c.get("sub_fg") or "") or th["次要"]))

            # 标签（多色小胶囊，居中，最多两行 —— 高度已经在上面量好了）
            ty = sy2 + fs_sub + 10
            maxw = card_w - 16
            rows_tags = _tag_rows(draw, c.get("tags") or [], f_tag, maxw)
            for ri, row in enumerate(rows_tags):
                total_w = sum(x["w"] for x in row) + 6 * (len(row) - 1)
                tx = cx - total_w / 2.0
                yy = ty + ri * (pill_h + 4)
                if yy + pill_h > y1 - 4:
                    break
                for x in row:
                    fg = _to_rgb(x["fg"]) or th["正文"]
                    bgc = _to_rgb(x["bg"]) or th["标签底"]
                    draw.rounded_rectangle([tx, yy, tx + x["w"], yy + pill_h],
                                           radius=pill_h / 2.0, fill=bgc,
                                           outline=_mix(fg, bgc, 0.55), width=1)
                    tw_ = _tw(draw, x["text"], f_tag)
                    draw.text((tx + (x["w"] - tw_) / 2.0, yy + 5), x["text"],
                              font=f_tag, fill=fg)
                    tx += x["w"] + 6

    # 中间竖虚线（常驻 ｜ 奇遇）
    if two:
        dx = bx0 + INNER + panel_w + DIVW / 2.0
        _draw_dashed(draw, [(dx, by0 + INNER + 2), (dx, by1 - INNER - 2)],
                     th["网格线"], 2, 8, 7)

    # 容器的虚线边框画在卡片上面，才不会被卡片盖住一小截
    _dashed_rrect(draw, (bx0, by0, bx1, by1), BOX_RADIUS, th["网格线"], 2, 9, 6)

    # ---------------- 页脚 ----------------
    if footer:
        fy = by1 + 12
        draw.text((PAD, fy), _ellipsis(draw, footer, f_foot, W - PAD * 2 - 90),
                  font=f_foot, fill=th["次要"])
        pg = f"{page} / {total_pages}"
        pw = _tw(draw, pg, f_foot)
        draw.text((W - PAD - pw, fy), pg, font=f_foot, fill=th["空白"])

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
    two = len(plist) == 2
    per_cols = max(1, cols // 2) if two else cols
    all_cards = [c for p in plist for c in p["cards"]]
    avatar_d = AVATAR if per_cols <= 4 else 86

    # 标签要占几行：能测就真测（和 Pillow 版同一套 _tag_rows），测不了按 1 行算
    fs_tag = max(9, base - 6)
    tag_rows_max = 1
    pil = _pil()
    if pil is not None:
        try:
            Image, ImageDraw, _, _ = pil  # type: ignore[misc]
            scratch = Image.new("RGB", (8, 8), (255, 255, 255))
            sd = ImageDraw.Draw(scratch)
            f_tag = _font(font_path, fs_tag)
            W = max(560, int(width))
            IW = W - 26 * 2 - 18 * 2
            DIVW = 34 if two else 0
            panel_w = int((IW - DIVW) / 2) if two else IW
            card_w = int((panel_w - 18 * (per_cols - 1)) / float(per_cols))
            for c in all_cards:
                c = c if isinstance(c, dict) else {}
                n_rows = len(_tag_rows(sd, c.get("tags") or [], f_tag, card_w - 16))
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
                "bins": c.get("bins"),
                "bins_bg": ("#%02X%02X%02X" % accent) if accent else "",
                "img": data_uri(str(c.get("icon") or "")),
                "tags": tags,
                "sats": sats,
            })
        return out_cards

    out_panels = []
    for p in plist:
        out_panels.append({
            "title": p["title"],
            "color": p["color"] or "#C25828",
            "count": len(p["cards"]),
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
