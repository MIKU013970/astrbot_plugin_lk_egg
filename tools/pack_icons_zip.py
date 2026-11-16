# -*- coding: utf-8 -*-
"""把插件图标打成一个 icons.zip（发布用 / 喂给控制台）。

用途：插件安装包只带 icons/index.json（17 KB），PNG 太大过不了市场审核。
这个脚本把 icons/ 打成 icons.zip，两个去处：

    python tools/pack_icons_zip.py                     # 只打包，输出到 洛克王国蛋神助手/icons.zip
    python tools/pack_icons_zip.py --copy-console      # 顺手放进控制台 data/plugin_assets/
    python tools/pack_icons_zip.py --url <GitHub Release 地址>   # 顺便 POST 给控制台下载

产出 zip 约 7 MB（PNG 已压缩，zip 帮不上忙），放 GitHub Release 当 asset，
再把地址填进插件配置 asset_url；或者直接 `--copy-console` 走本机控制台。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
#: 素材源目录：优先 洛克王国蛋神助手/icons_src（仓库里不再放 PNG），退回插件 icons/
ICONS_CANDIDATES = (
    PLUGIN.parent / "洛克王国蛋神助手" / "icons_src",
    PLUGIN / "icons",
)


def find_icons() -> Path:
    for d in ICONS_CANDIDATES:
        if (d / "pet").is_dir():
            return d
    return ICONS_CANDIDATES[0]


ICONS = find_icons()
OUT_DEFAULT = PLUGIN.parent / "洛克王国蛋神助手" / "icons.zip"
CONSOLE_ASSETS = Path(r"C:\Users\MIKU\Desktop\蛋神助手-自动买单") / "data" / "plugin_assets"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT_DEFAULT), help="输出 zip 路径")
    ap.add_argument("--copy-console", action="store_true",
                    help="同时复制到控制台 data/plugin_assets/icons.zip")
    ap.add_argument("--no-index", action="store_true", help="不把 index.json 打进包（默认打）")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    files = 0
    tmp = out.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for kind in ("pet", "egg", "shiny"):
            d = ICONS / kind
            if not d.is_dir():
                print(f"! 跳过不存在的 {kind}/")
                continue
            # 安装包不带 form_ 系列（多形态图，占体积且只在少数形态卡用）
            for f in sorted(d.glob("*.png")):
                if f.name.startswith("form_"):
                    continue
                z.write(f, f"{kind}/{f.name}")
                files += 1
        idx = ICONS / "index.json"
        if not idx.is_file():
            idx = PLUGIN / "icons" / "index.json"      # index.json 始终在仓库里
        if idx.is_file() and not args.no_index:
            z.write(idx, "index.json")
    tmp.replace(out)

    size = out.stat().st_size
    print(f"✅ {files} 张 + index.json → {out}")
    print(f"   {size / 1024 / 1024:.2f} MB（zip 对已压缩的 PNG 帮不上忙）")

    if args.copy_console:
        CONSOLE_ASSETS.mkdir(parents=True, exist_ok=True)
        dst = CONSOLE_ASSETS / "icons.zip"
        shutil.copy2(out, dst)
        print(f"✅ 已放进控制台：{dst}")
        print("   控制台重启后可访问 GET /api/plugin-assets/zip 验证")
    return 0


if __name__ == "__main__":
    sys.exit(main())
