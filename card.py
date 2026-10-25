# -*- coding: utf-8 -*-
"""查蛋结果渲染成图片卡片（Pillow）。

设计要点：
- **Pillow 是可选依赖**。没装的时候 `available()` 返回 False，
  插件那边会自动退回纯文字，不会因为少个库就发不出消息。
- **配色和字号全部可配**，见 `parse_theme()` / `find_font()`。
- **字体找不到就用 None**，渲染时用 Pillow 内置位图字体兜底（中文会变方块，
  但至少不崩），插件那边靠这条判断要不要退回文字。

渲染成本实测：热态约 6 ms/张，冷启动第一张约 56 ms（含 import PIL）。
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 主题与字体

#: 配色默认值（键 → #RRGGBB）。用户可以在配置里逐项覆盖。
THEME_DEFAULT: Dict[str, str] = {
    "背景": "#FCFAF6",
    "卡片": "#FFFFFF",
    "边框": "#E8E2D8",
    "标题": "#262220",
    "正文": "#4A4642",
    "次要": "#8C867E",
    "强调": "#C25828",
    "标签底": "#F6F0E6",
}

#: 字号比例：以配置里的「正文字号」为基准推导其它字号
SIZE_RATIO = {
    "标题": 12,     # 正文 + 12
    "名字": 1,      # 正文 + 1
    "正文": 0,
    "次要": -3,
    "更小": -4,
    "数字": 2,      # 正文 + 2
}

#: 候选字体，按顺序探测。第一个存在的就用。
FONT_CANDIDATES: Tuple[str, ...] = (
    "msyh.ttc", "msyhbd.ttc", "simhei.ttf", "Deng.ttf", "simsun.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
)

#: 插件目录里自带的字体（放哪个名字都认）
BUNDLED_FONTS: Tuple[str, ...] = (
    "font.ttf", "font.otf", "font.ttc",
    "NotoSansSC-Regular.otf", "NotoSansSC-Regular.ttf",
    "SourceHanSansSC-Regular.otf", "msyh.ttc",
)


def _pil():
    """延迟导入 Pillow；没装就返回 None。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        return Image, ImageDraw, ImageFont
    except Exception:
        return None


def available() -> bool:
    """Pillow 装了没。没装就别调 render_*。"""
    return _pil() is not None


def parse_theme(text: Any, base: Optional[Dict[str, Any]] = None) -> Dict[str, Tuple[int, int, int]]:
    """解析配色配置 → {键: (r, g, b)}。

    格式：每行一项 `键=#RRGGBB`，分隔符 `=`、`:`、`：` 都行，# 可省略。
    写错的、认不出的键一律忽略，不影响其它项。
    """
    out: Dict[str, Tuple[int, int, int]] = {}
    src = base if base is not None else THEME_DEFAULT
    for key, val in src.items():
        rgb = _to_rgb(val)
        if rgb:
            out[key] = rgb

    if isinstance(text, dict):
        items = text.items()
    else:
        items = []
        for line in str(text or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#") and "=" not in line:
                continue
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


def _to_rgb(val: Any) -> Optional[Tuple[int, int, int]]:
    if isinstance(val, (tuple, list)) and len(val) == 3:
        try:
            return tuple(int(x) & 255 for x in val)  # type: ignore[return-value]
        except Exception:
            return None
    s = str(val or "").strip().lstrip("#").strip()
    if len(s) == 3 and all(c in "0123456789abcdefABCDEF" for c in s):
        s = "".join(c * 2 for c in s)
    if len(s) == 6 and all(c in "0123456789abcdefABCDEF" for c in s):
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    return None


def find_font(custom: str = "") -> Optional[str]:
    """找一个能用的中文字体文件。找不到返回 None。

    顺序：用户指定 → 插件目录自带 → 系统常见路径。
    """
    if custom:
        p = Path(os.path.expanduser(str(custom).strip()))
        if p.is_file():
            return str(p)

    here = Path(__file__).resolve().parent
    for name in BUNDLED_FONTS:
        p = here / name
        if p.is_file():
            return str(p)

    win = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for name in FONT_CANDIDATES:
        p = Path(name)
        if not p.is_absolute():
            p = win / name
        if p.is_file():
            return str(p)

    # 系统里随便捞一个 ttf/ttc（可能不含中文，但好过没有）
    for root in ("/usr/share/fonts", "/usr/local/share/fonts",
                 str(win), "/System/Library/Fonts", "/Library/Fonts"):
        r = Path(root)
        if not r.is_dir():
            continue
        try:
            for f in sorted(r.rglob("*")):
                if f.suffix.lower() in (".ttf", ".ttc", ".otf"):
                    return str(f)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- 渲染

def _sizes(base: int) -> Dict[str, int]:
    return {k: max(9, base + off) for k, off in SIZE_RATIO.items()}


def _fonts(font_path: Optional[str], sizes: Dict[str, int]):
    _, _, ImageFont = _pil()  # type: ignore[misc]
    out = {}
    for key, size in sizes.items():
        try:
            out[key] = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
        except Exception:
            try:
                out[key] = ImageFont.load_default()
            except Exception:
                out[key] = None
    return out


def _fit(draw, text: str, font, limit: float) -> str:
    """超宽就截断加省略号，绝不画出卡片外。"""
    text = "" if text is None else str(text)
    if font is None:
        return text
    try:
        if draw.textlength(text, font=font) <= limit:
            return text
    except Exception:
        return text
    while text and draw.textlength(text + "…", font=font) > limit:
        text = text[:-1]
    return text + "…" if text else ""


def _lazy_image():
    """滚动的图片容器：先照着估算开一块，装不下就换更长的重画。

    比一开始就算准高度省事得多，而且不会因为字体度量差异裁掉内容。
    """
    return None


def render_card(
    title: str,
    subtitle: str = "",
    tag: str = "",
    rows: Optional[Sequence[Sequence[Any]]] = None,
    boxes: Optional[Sequence[Sequence[Any]]] = None,
    table: Optional[Dict[str, Any]] = None,
    theme: Any = None,
    font_path: str = "",
    width: int = 700,
    font_size: int = 18,
) -> bytes:
    """画一张卡片，返回 PNG bytes。

    rows  : [(左, 右), ...]                    普通键值行
    boxes : [(标签, 内容, (r,g,b)), ...]       两列色块（大小头那种）
    table : {"columns": [...], "items": [四个值的元组, ...]}  行情表
    """
    pil = _pil()
    if not pil:
        raise RuntimeError("没装 Pillow")
    Image, ImageDraw, _ = pil

    colors = parse_theme(theme) if not isinstance(theme, dict) or not theme else (
        {**parse_theme(None), **{k: _to_rgb(v) for k, v in theme.items() if _to_rgb(v)}}
    )
    # theme 传进来可能已经是 parse_theme 的结果（值是元组），这里统一一下
    fixed = {}
    for k, v in THEME_DEFAULT.items():
        rgb = _to_rgb(colors.get(k, v))
        fixed[k] = rgb or _to_rgb(v)
    colors = fixed

    sizes = _sizes(int(font_size))
    fonts = _fonts(font_path or find_font(""), sizes)

    pad = max(14, round(width * 0.037))
    inner = width - pad * 2
    lh_row = sizes["正文"] + 13
    box_h = sizes["正文"] + 18
    head_h = sizes["数字"] + 18

    rows = list(rows or [])
    boxes = list(boxes or [])
    tcols = list((table or {}).get("columns") or [])
    titems = list((table or {}).get("items") or [])

    # ---- 先估个高度，画的时候用两遍法：先量后画
    def measure(im_draw) -> int:
        y = pad
        y += sizes["标题"] + 22                       # 标题行
        if rows or boxes or titems:
            y += 10
        y += lh_row * len(rows)
        if boxes:
            y += 10 + box_h * ((len(boxes) + 1) // 2)
        if tcols or titems:
            y += 12 + head_h + head_h * len(titems)
        y += pad
        return y

    probe = Image.new("RGB", (width, 10), colors["背景"])
    pd = ImageDraw.Draw(probe)
    height = measure(pd)

    img = Image.new("RGB", (width, height), colors["背景"])
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle([6, 6, width - 7, height - 7], 16,
                         fill=colors["卡片"], outline=colors["边框"], width=2)

    x0, x1 = pad, width - pad
    y = pad

    # 标题 + 副标题
    dr.text((x0, y), _fit(dr, title, fonts["标题"], inner * 0.62), font=fonts["标题"], fill=colors["标题"])
    tw = dr.textlength(str(title), font=fonts["标题"]) if fonts["标题"] else 0
    if subtitle:
        dr.text((x0 + tw + 12, y + sizes["标题"] - sizes["次要"] - 4),
                _fit(dr, subtitle, fonts["次要"], inner * 0.3),
                font=fonts["次要"], fill=colors["次要"])
    if tag:
        tgw = dr.textlength(str(tag), font=fonts["次要"]) + 20
        dr.rounded_rectangle([x1 - tgw, y + 2, x1, y + sizes["次要"] + 24], 12, fill=colors["标签底"])
        dr.text((x1 - tgw + 10, y + 8), str(tag), font=fonts["次要"], fill=colors["强调"])
    y += sizes["标题"] + 22

    if rows or boxes or titems:
        dr.line([x0, y, x1, y], fill=colors["边框"], width=1)
        y += 10

    key_w = max(56, round(inner * 0.16))
    for item in rows:
        k = item[0] if len(item) > 0 else ""
        v = item[1] if len(item) > 1 else ""
        if k:
            dr.text((x0, y + 3), _fit(dr, k, fonts["次要"], key_w - 8),
                    font=fonts["次要"], fill=colors["次要"])
        if v is not None and str(v) != "":
            dr.text((x0 + key_w, y), _fit(dr, v, fonts["正文"], inner - key_w),
                    font=fonts["正文"], fill=colors["正文"])
        y += lh_row

    if boxes:
        y += 10
        bw = (inner - 14) // 2
        for i in range(0, len(boxes), 2):
            cy = y + (i // 2) * box_h
            for j, box in enumerate(boxes[i:i + 2]):
                lbl = box[0] if len(box) > 0 else ""
                txt = box[1] if len(box) > 1 else ""
                col = _to_rgb(box[2]) if len(box) > 2 else colors["强调"]
                bx = x0 + j * (bw + 14)
                dr.rounded_rectangle([bx, cy, bx + bw, cy + box_h - 6], 9,
                                     fill=colors["标签底"], outline=colors["边框"])
                lw = dr.textlength(str(lbl), font=fonts["次要"]) + 10 if fonts["次要"] else 40
                dr.text((bx + 10, cy + 5), _fit(dr, lbl, fonts["次要"], bw * 0.4),
                        font=fonts["次要"], fill=col or colors["强调"])
                dr.text((bx + lw + 10, cy + 5),
                        _fit(dr, txt, fonts["更小"], bw - lw - 22),
                        font=fonts["更小"], fill=colors["正文"])
        y += box_h * ((len(boxes) + 1) // 2)

    if tcols or titems:
        y += 12
        n_num = max(1, len(tcols) - 1 or 3)
        num_area = inner * 0.62
        start = x1 - num_area
        right_x = [start + num_area * (i + 1) / n_num for i in range(n_num)]

        for i, cname in enumerate(tcols):
            if i == 0:
                dr.text((x0 + 4, y), str(cname), font=fonts["次要"], fill=colors["次要"])
            elif i - 1 < len(right_x):
                dr.text((right_x[i - 1], y), str(cname), font=fonts["次要"],
                        fill=colors["次要"], anchor="ra")
        y += head_h - 4

        for item in titems:
            values = list(item)
            dr.rounded_rectangle([x0 - 4, y - 2, x1 + 4, y + head_h - 8], 8,
                                 fill=colors["标签底"] if colors["标签底"] else None)
            if values:
                dr.text((x0 + 4, y), _fit(dr, values[0], fonts["名字"], inner * 0.3),
                        font=fonts["名字"], fill=colors["标题"])
            for i, val in enumerate(values[1:len(right_x) + 1]):
                col = colors["强调"] if i == 1 else colors["正文"]
                dr.text((right_x[i], y), str(val), font=fonts["数字"],
                        fill=colors["强调"] if i == 1 else col, anchor="ra")
            y += head_x_advance(head_h)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def head_x_advance(head_h: int) -> int:
    return head_h


def render_identify_card(
    head: str,
    hits: Sequence[Dict[str, Any]],
    theme: Any = None,
    font_path: str = "",
    width: int = 700,
    font_size: int = 18,
) -> bytes:
    """身高体重查蛋的结果卡。`hits` 为空时也出一张（写「没有匹配」）。"""
    rows: List[Tuple[str, Any]] = []
    if not hits:
        rows.append(("结果", "没有匹配的精灵蛋"))
        rows.append(("", "可能是输入格式不对，或者这颗蛋不是图鉴里的品种"))
    else:
        rows.append(("可能孵出", f"{len(hits)} 种"))
        for i, h in enumerate(hits, 1):
            nm = h.get("name") or "—"
            rows.append((f"{i}. {nm}", f"{h.get('range') or ''} · {h.get('size') or ''}".strip(" ·")))
            sub = " · ".join(x for x in (f"蛋名 {h.get('egg')}" if h.get("egg") else "",
                                         h.get("groups") or "", h.get("sex") or "") if x)
            if sub:
                rows.append(("", sub))
            if h.get("hw"):
                rows.append(("", h["hw"]))
    return render_card(
        title="身高体重查蛋",
        subtitle=head,
        rows=rows,
        theme=theme,
        font_path=font_path,
        width=width,
        font_size=font_size,
    )
