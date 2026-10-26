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
import json
import os
import re
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


def _png_bytes(img) -> bytes:
    """编码 PNG。

    先用最快的一档（不 optimize、compress_level=6）——optimize 要多花 7 倍时间，
    却只小 1.6%，不划算。
    超过 100 KB 的图（比如帮助菜单那种长卡片）再换成 128 色调色板压一遍：
    文字卡只有几百种颜色，换调色板能小到 1/3，发图也快。
    """
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False, compress_level=6)
    data = buf.getvalue()
    if len(data) > 100_000:
        try:
            Image, _, _ = _pil()  # type: ignore[misc]
            buf2 = io.BytesIO()
            img.convert("P", palette=Image.ADAPTIVE, colors=128).save(
                buf2, "PNG", optimize=True, compress_level=9)
            if buf2.tell() < len(data):
                data = buf2.getvalue()
        except Exception:
            pass
    return data

#: 图标目录（和 card.py 同级）
ICON_DIR = Path(__file__).resolve().parent / "icons"
_ICON_INDEX: Optional[Dict[str, Dict[str, str]]] = None


def _icon_index() -> Dict[str, Dict[str, str]]:
    global _ICON_INDEX
    if _ICON_INDEX is None:
        _ICON_INDEX = {"pet": {}, "egg": {}, "pet_form": {}, "egg_form": {},
                       "pet_shiny": {}}
        try:
            p = ICON_DIR / "index.json"
            if p.is_file():
                d = json.loads(p.read_text(encoding="utf-8"))
                for k in ("pet", "egg", "pet_form", "egg_form", "pet_shiny"):
                    _ICON_INDEX[k] = d.get(k) or {}
        except Exception:
            pass
    return _ICON_INDEX


def icon_path(kind: str, name: str, form: str = "") -> Optional[str]:
    """名字 → 图标文件路径。kind='pet'（精灵）| 'egg'（蛋）。找不到返回 None。

    多形态精灵（鸭吉吉 6 种、海枝枝 4 种…）在宠物库里是**不同的 petId、不同的图**，
    所以传了 form 就先按「名字|形态」精确找，找不到再退回按名字找。
    """
    full = _icon_index()
    n0 = str(name or "").strip()
    f0 = str(form or "").strip()
    if kind == "shiny":
        # 异色宠物图：assets/pets/<id>/shiny.png
        sm = full.get("pet_shiny") or {}
        base_names = [n0]
        b = n0
        for suf in ("的蛋蛋", "的蛋", "蛋"):
            if b.endswith(suf):
                base_names.append(b[: -len(suf)])
                break
        cands = []
        for bn in base_names:
            if not bn:
                continue
            if f0:
                cands.append(f"{bn}|{f0}")
            cands.append(bn)
        for k in cands:
            sl = sm.get(k)
            if sl:
                p = ICON_DIR / "shiny" / f"{sl}.png"
                if p.is_file():
                    return str(p)
        return None
    if kind == "pet" and n0 and f0:
        pf = (full.get("pet_form") or {}).get(f"{n0}|{f0}")
        if not pf:                       # 形态名可能带「名字（形态）」这种写法
            short = f0.replace(n0, "").strip("（）() ")
            if short:
                pf = (full.get("pet_form") or {}).get(f"{n0}|{short}")
        if pf:
            p = ICON_DIR / "pet" / f"{pf}.png"
            if p.is_file():
                return str(p)
    if kind == "egg" and n0 and f0:
        # 同一只精灵的不同形态，蛋名可能一样但蛋图不一样
        # （鸭吉吉 5 个形态都叫「鸭吉吉蛋」，图标却分别是 107009/107017/…）
        # 索引键用的是**精灵名**，调用方可能传的是**蛋名**，先归一化再查
        base_names = [n0, str(f0).split("（")[0].strip()]
        b = n0
        for suf in ("的蛋蛋", "的蛋", "蛋"):
            if b.endswith(suf):
                base_names.append(b[: -len(suf)])
                break
        forms = [f0, f0.replace("（", "(").split("(")[-1].rstrip("）) ").strip()]
        for bn in base_names:
            for fm in forms:
                if not bn or not fm:
                    continue
                ef = (full.get("egg_form") or {}).get(f"{bn}|{fm}")
                if ef:
                    p = ICON_DIR / "egg" / f"{ef}.png"
                    if p.is_file():
                        return str(p)
    idx = full.get(kind) or {}
    n = n0
    if not n or not idx:
        return None
    cands = [n]
    if n.endswith("的蛋"):
        cands.append(n[:-2])
        cands.append(n[:-2] + "蛋")
    elif n.endswith("蛋"):
        cands.append(n[:-1])
    else:
        cands.extend([n + "蛋", n + "的蛋"])
    slug = None
    for c in cands:
        slug = idx.get(c)
        if slug:
            break
    if not slug:
        return None
    f = ICON_DIR / kind / f"{slug}.png"
    return str(f) if f.is_file() else None


def has_icons() -> bool:
    """图标库装了没（打包时可能没带）。"""
    return bool((_icon_index().get("egg") or {}) or (_icon_index().get("pet") or {}))


def _load_icon(path, size: int):
    """读一张图标，缩放到 size×size，返回 RGBA 图；读不了返回 None。"""
    pil = _pil()
    if not pil or not path:
        return None
    Image, _, _ = pil
    try:
        im = Image.open(str(path)).convert("RGBA")
        if im.size != (size, size):
            im = im.resize((size, size), Image.LANCZOS)
        return im
    except Exception:
        return None


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
    head_icons: Optional[Sequence[str]] = None,
    tiles: Optional[Sequence[Sequence[Any]]] = None,
    tiles_per_row: int = 4,
    tile_icon: int = 64,
    hits: Optional[Sequence[Sequence[Any]]] = None,
) -> bytes:
    """画一张卡片，返回 PNG bytes。

    rows  : [(左, 右), ...]                       普通键值行
    boxes : [(标签, 内容, (r,g,b)), ...]          两列色块（大小头那种）
    table : {"columns": [...], "items": [...]}    行情表
    head_icons : 标题**前面**的图（精灵图 / 蛋图），最多 4 张
    tiles : [(图标路径, [说明行...]), ...]         图标格（货架用，说明最多 3 行）
    hits  : [(图标路径, 名字, 首行值, [子行...]), ...]
            名字在左栏，图标塞在**名字下面**那块空白里，子行排在右边
    """
    pil = _pil()
    if not pil:
        raise RuntimeError("没装 Pillow")
    Image, ImageDraw, _ = pil

    colors = _norm_theme(theme)
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
    hi = [x for x in (head_icons or []) if x][:4]
    tl = [x for x in (tiles or []) if x]
    hl = [x for x in (hits or []) if x]

    tile_ico = int(tile_icon)
    hi_size = tile_ico
    hi_w = (len(hi) * hi_size + (len(hi) - 1) * 8 + 12) if hi else 0
    title_h = max(sizes["标题"] + 22, (hi_size + 10) if hi else 0)

    tile_line = sizes["更小"] + 6
    per_row = max(1, int(tiles_per_row))
    tile_w = inner // per_row
    max_caps = min(3, max((len(x[1]) for x in tl if len(x) > 1 and x[1]), default=0))
    tile_h = (tile_ico + tile_line * max_caps + 12) if tl else 0
    tile_rows = ((len(tl) + per_row - 1) // per_row) if tl else 0

    hit_ico = tile_ico
    key_w = max(56, round(inner * 0.16))

    def hit_h(item) -> int:
        subs = item[3] if len(item) > 3 and item[3] else []
        return max(hit_ico, (1 + len(list(subs))) * lh_row) + 10

    probe = Image.new("RGB", (width, 10), colors["背景"])
    pd = ImageDraw.Draw(probe)

    def measure(_d) -> int:
        y = pad
        y += title_h
        if rows or boxes or titems or tl or hl:
            y += 10
        y += lh_row * len(rows)
        if boxes:
            y += 10 + box_h * ((len(boxes) + 1) // 2)
        if tcols or titems:
            y += 12 + head_h + head_h * len(titems)
        if hl:
            y += 12 + sum(hit_h(x) for x in hl)
        if tl:
            y += 12 + tile_h * tile_rows
        y += pad
        return y

    height = measure(pd)

    img = Image.new("RGB", (width, height), colors["背景"])
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle([6, 6, width - 7, height - 7], 16,
                         fill=colors["卡片"], outline=colors["边框"], width=2)

    x0, x1 = pad, width - pad
    y = pad

    # ---- 标题行：图在最前面
    if hi:
        cx = x0
        for pth in hi:
            ic = _load_icon(pth, hi_size)
            if ic is None:
                continue
            img.paste(ic, (cx, y), ic)
            cx += hi_size + 8
    tx = x0 + hi_w
    title_txt = _fit(dr, title, fonts["标题"], max(60, inner - hi_w - 90))
    dr.text((tx, y), title_txt, font=fonts["标题"], fill=colors["标题"])
    tw = dr.textlength(title_txt, font=fonts["标题"]) if fonts["标题"] else 0
    if subtitle:
        dr.text((tx + tw + 12, y + sizes["标题"] - sizes["次要"] - 4),
                _fit(dr, subtitle, fonts["次要"], max(40, inner - hi_w - tw - 30)),
                font=fonts["次要"], fill=colors["次要"])
    if tag:
        tgw = dr.textlength(str(tag), font=fonts["次要"]) + 20
        dr.rounded_rectangle([x1 - tgw, y + 2, x1, y + sizes["次要"] + 24], 12,
                             fill=colors["标签底"])
        dr.text((x1 - tgw + 10, y + 8), str(tag), font=fonts["次要"], fill=colors["强调"])
    y += title_h

    if rows or boxes or titems or tl or hl:
        dr.line([x0, y, x1, y], fill=colors["边框"], width=1)
        y += 10

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
        num_start = x1 - num_area
        right_x = [num_start + num_area * (i + 1) / n_num for i in range(n_num)]

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
                                 fill=colors["标签底"])
            if values:
                dr.text((x0 + 4, y), _fit(dr, values[0], fonts["名字"], inner * 0.3),
                        font=fonts["名字"], fill=colors["标题"])
            for i, val in enumerate(values[1:len(right_x) + 1]):
                col = colors["强调"] if i == 1 else colors["正文"]
                dr.text((right_x[i], y), str(val), font=fonts["数字"],
                        fill=colors["强调"] if i == 1 else col, anchor="ra")
            y += head_h

    # ---- 每条结果：名字在左栏，图塞在名字下面那块空白里
    if hl:
        y += 12
        for item in hl:
            ipath = item[0] if len(item) > 0 else None
            name = str(item[1]) if len(item) > 1 else ""
            first = str(item[2]) if len(item) > 2 else ""
            subs = [str(x) for x in (item[3] if len(item) > 3 and item[3] else []) if str(x)]
            hh = hit_h(item)
            dr.text((x0, y), _fit(dr, name, fonts["名字"], key_w - 8),
                    font=fonts["名字"], fill=colors["标题"])
            if first:
                dr.text((x0 + key_w, y), _fit(dr, first, fonts["正文"], inner - key_w),
                        font=fonts["正文"], fill=colors["正文"])
            ic = _load_icon(ipath, hit_ico)
            if ic is not None:
                img.paste(ic, (x0, y + lh_row), ic)
            for i, sub in enumerate(subs):
                dr.text((x0 + key_w, y + lh_row * (i + 1)),
                        _fit(dr, sub, fonts["正文"], inner - key_w),
                        font=fonts["正文"], fill=colors["正文"])
            y += hh

    # ---- 图标格（货架）
    if tl:
        y += 12
        for i, tile in enumerate(tl):
            col = i % per_row
            rw = i // per_row
            tx2 = x0 + col * tile_w
            ty = y + rw * tile_h
            icon = _load_icon(tile[0] if len(tile) > 0 else None, tile_ico)
            if icon is not None:
                img.paste(icon, (tx2 + (tile_w - tile_ico) // 2, ty), icon)
            caps = [str(c) for c in (tile[1] if len(tile) > 1 and tile[1] else [])][:max_caps]
            cy = ty + tile_ico + 4
            for cap in caps:
                dr.text((tx2 + tile_w // 2, cy),
                        _fit(dr, cap, fonts["更小"], tile_w - 8),
                        font=fonts["更小"], fill=colors["正文"], anchor="ma")
                cy += tile_line

    return _png_bytes(img)

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

# ---------------------------------------------------------------- 任意文字 → 卡片

#: 分隔符用的字符集
_DIV_CHARS = "-—=_*~"

_COLSPLIT_RE = re.compile(r"^(\S.*?)\s{2,}(\S.*)$")
_DIV_MID_RE = re.compile(r"^[-—=_*~]{2,}.*[-—=_*~]{2,}$")


def _split_columns(line: str):
    """'查询 火花      说明' → ('查询 火花', '说明')；拆不开就 None。"""
    s = str(line or "")
    if not s.strip():
        return None
    m = _COLSPLIT_RE.match(s.strip())
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def _divider_label(line: str):
    """'———— 在售 3 件 ————' → '在售 3 件'；不是分隔线就 None。"""
    s = str(line or "").strip()
    if not s:
        return None
    core = s.strip(_DIV_CHARS + " ")
    if not core:
        return "" if len(s) >= 3 else None
    if _DIV_MID_RE.match(s):
        return core
    return None


def _wrap(draw, text: str, font, limit: float):
    """按像素宽度硬折行（中文没有词边界，逐字累加）。"""
    text = "" if text is None else str(text)
    if font is None or not text:
        return [text]
    out, cur = [], ""
    for ch in text:
        cand = cur + ch
        try:
            w = draw.textlength(cand, font=font)
        except Exception:
            w = 0
        if w > limit and cur:
            out.append(cur)
            cur = ch
        else:
            cur = cand
    out.append(cur)
    return out or [""]


def _norm_theme(theme) -> Dict[str, Tuple[int, int, int]]:
    """把 theme（str 配置 / 已解析的 dict）统一成完整配色表。"""
    colors = {}
    if isinstance(theme, dict) and theme:
        colors = dict(theme)
    else:
        colors = parse_theme(theme if isinstance(theme, str) else None)
    fixed = {}
    for k, default in THEME_DEFAULT.items():
        rgb = _to_rgb(colors.get(k, default)) or _to_rgb(default)
        fixed[k] = rgb
    return fixed


def render_text_card(
    text: str,
    theme=None,
    font_path: str = "",
    width: int = 700,
    font_size: int = 18,
    max_lines: int = 80,
    head_icons: Optional[Sequence[str]] = None,
    tile_icon: int = 64,
) -> bytes:
    """把一段普通文字画成卡片，返回 PNG bytes。

    认三种行：
      - 第一行当标题（放大、加重）
      - `———— 小节 ————` 当分隔线 + 居中标签
      - `命令<两个以上空格>说明` 拆成两列，左列对齐
    其余按正文折行。行数超过 max_lines 就截断并注明。
    """
    pil = _pil()
    if not pil:
        raise RuntimeError("没装 Pillow")
    Image, ImageDraw, _ = pil

    colors = _norm_theme(theme)
    sizes = _sizes(int(font_size))
    fonts = _fonts(font_path or find_font(""), sizes)

    pad = max(14, round(width * 0.037))
    inner = width - pad * 2
    lh = sizes["正文"] + 12

    raw = str(text or "").replace("\r\n", "\n").replace("\t", "    ").split("\n")
    raw = [x.rstrip() for x in raw]
    while raw and not raw[0].strip():
        raw.pop(0)
    while raw and not raw[-1].strip():
        raw.pop()

    title = None
    body = raw
    if len(raw) > 1 and _divider_label(raw[0]) is None:
        title = raw[0].strip()
        body = raw[1:]

    probe = Image.new("RGB", (width, 10), colors["背景"])
    pd = ImageDraw.Draw(probe)

    # 两列基准：所有能拆的行的左列最大宽度
    max_left = 0.0
    for line in body:
        cols = _split_columns(line)
        if cols and fonts["正文"] is not None:
            try:
                max_left = max(max_left, pd.textlength(cols[0], font=fonts["正文"]))
            except Exception:
                pass
    col_x = min(max_left + 18, inner * 0.62) if max_left else inner * 0.42
    col_x = max(col_x, 60)

    blocks = []
    used = 0
    for line in body:
        if not line.strip():
            blocks.append(("gap", 10))
            continue
        dl = _divider_label(line)
        if dl is not None:
            blocks.append(("div", dl))
            used += 0
            continue
        cols = _split_columns(line)
        if cols:
            try:
                left_w = pd.textlength(cols[0], font=fonts["正文"])
            except Exception:
                left_w = 0
            if left_w <= col_x - 6:
                wrapped = _wrap(pd, cols[1], fonts["正文"], inner - col_x)
                blocks.append(("col", cols[0], wrapped))
                used += len(wrapped)
                if used > max_lines:
                    break
                continue
        indent_ch = len(line) - len(line.lstrip())
        ind = min(indent_ch * 3, 24)
        wrapped = _wrap(pd, line.strip(), fonts["正文"], inner - ind)
        blocks.append(("text", ind, wrapped))
        used += len(wrapped)
        if used > max_lines:
            break

    truncated = used > max_lines
    if truncated:
        blocks.append(("text", 0, ["……（后面还有内容，回复里看不全）"]))

    hi = [x for x in (head_icons or []) if x][:4]
    hi_size = max(40, int(tile_icon)) if hi else 0
    hi_w = (len(hi) * hi_size + (len(hi) - 1) * 8 + 12) if hi else 0
    title_h = max(sizes["标题"] + 20, (hi_size + 10) if hi else 0)

    # 估高度
    h = pad
    if title:
        h += title_h
    for b in blocks:
        if b[0] == "gap":
            h += b[1]
        elif b[0] == "div":
            h += sizes["次要"] + 22
        else:
            h += lh * len(b[2])
    h += pad
    h = max(h, pad * 2 + 40)

    img = Image.new("RGB", (width, h), colors["背景"])
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle([6, 6, width - 7, h - 7], 16,
                         fill=colors["卡片"], outline=colors["边框"], width=2)

    x0, x1 = pad, width - pad
    y = pad
    if hi:
        cx = x0
        for pth in hi:
            ic = _load_icon(pth, hi_size)
            if ic is None:
                continue
            img.paste(ic, (cx, y), ic)
            cx += hi_size + 8
    if title:
        dr.text((x0 + hi_w, y), _fit(dr, title, fonts["标题"], max(60, inner - hi_w)),
                font=fonts["标题"], fill=colors["标题"])
    if title or hi:
        y += title_h

    for b in blocks:
        if y > h - pad:
            break
        if b[0] == "gap":
            y += b[1]
        elif b[0] == "div":
            label = b[1]
            cy = y + sizes["次要"] // 2 + 4
            dr.line([x0, cy, x1, cy], fill=colors["边框"], width=1)
            if label:
                tw = dr.textlength(label, font=fonts["次要"]) + 16
                cx = x0 + (inner - tw) // 2
                dr.rectangle([cx - 2, cy - sizes["次要"], cx + tw + 2, cy + sizes["次要"]],
                             fill=colors["卡片"])
                dr.text((cx + 8, cy - sizes["次要"] // 2 - 4), label,
                        font=fonts["次要"], fill=colors["强调"])
            y += sizes["次要"] + 22
        elif b[0] == "col":
            left, wrapped = b[1], b[2]
            dr.text((x0, y), _fit(dr, left, fonts["正文"], col_x - 6),
                    font=fonts["正文"], fill=colors["标题"])
            for w_ in wrapped:
                dr.text((x0 + col_x, y), w_, font=fonts["正文"], fill=colors["正文"])
                y += lh
        else:
            ind, wrapped = b[1], b[2]
            for w_ in wrapped:
                dr.text((x0 + ind, y), w_, font=fonts["正文"], fill=colors["正文"])
                y += lh

    return _png_bytes(img)
