# -*- coding: utf-8 -*-
"""
new_project.py — 新建项目脚手架

用法：
    python new_project.py --list-styles              # 先看有哪些画风包
    python new_project.py --name 新故事 --style ink_wash
    python new_project.py --name 新故事 --style ink_wash --slug xin-gushi --ratio 16:9

生成：
    <ROOT>/00X-新故事/
      project.json        ← 题材专属配置；画风只写一个 style_id
      shots.csv           ← 从 _engine/shots_template.csv 复制
      视觉设定.md          ← 从画风包渲染，不要手改
      shot/ videos/ videos_fixed/ audio/ srt/

画风为什么不写在命令行里：
    以前这里有一张 FORMS 字典硬编码四套 style_block。问题是每条都是一坨字符串，
    负向词和采样参数没地方放，改了也不影响已存在的项目。现在 packages 到
    _engine/styles/<id>.json，一个文件一套完整画风。换画风 = 改 project.json 的
    style_id；要临时微调 = 写 style_override。

编号自动递增：看已有 00X-* 目录的最大号 +1。

为什么是 project.json 而不是 yaml：本机没装 pyyaml，且 JSON 在 Python 与 Node
（gen_html.js）两侧都能零依赖读取。
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
ROOT_DIR = ENGINE_DIR.parent
TEMPLATE = ENGINE_DIR / "shots_template.csv"

sys.path.insert(0, str(ENGINE_DIR))
from styles import list_styles, load_style, Style  # noqa: E402

# 每种制作形态的画风预置原本硬编码在这个文件里 —— 那是错的，
# 因为「改一次 FORMS 字典」并不能让已存在的项目同步，且负向词/采样参数无处安放。
# v2：预置搬到 _engine/styles/*.json，一个文件一套画风，本文件只读目录。
# 新建画风：复制 styles/_template.json → 改名去下划线 → 填。

RATIO_SIZES = {
    "16:9": {"width": 1280, "height": 720,
             "note": "0.92MP，速度与当前 1152x864 相当；想更清晰可升到 1536x864（1.33MP）"},
    "4:3":  {"width": 1152, "height": 864, "note": "0.99MP，001 项目在用的档位"},
    "1:1":  {"width": 1024, "height": 1024, "note": "1.05MP"},
    "9:16": {"width": 720,  "height": 1280, "note": "0.92MP，竖屏"},
}


def next_index() -> int:
    mx = 0
    if ROOT_DIR.exists():
        for d in ROOT_DIR.iterdir():
            if not d.is_dir() or d.name.startswith("_"):
                continue
            head = d.name.split("-")[0]
            if head.isdigit():
                mx = max(mx, int(head))
    return mx + 1


def slugify(text: str) -> str:
    """中文名转英文短横线 slug；含中文时回退到 project-NNN。"""
    import re
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return s if s else ""


def main():
    ap = argparse.ArgumentParser(description="新建漫剧项目脚手架")
    ap.add_argument("--name", default="", help="项目中文名，如：新故事")
    ap.add_argument("--style", default="", help="画风包 id，见 --list-styles；留空表示之后再定")
    ap.add_argument("--list-styles", action="store_true", help="列出所有可用画风包并退出")
    ap.add_argument("--inline-block", default="",
                    help="不用画风包，直接内联一段风格块（临时验证用，不推荐）")
    ap.add_argument("--slug", default="", help="英文短横线别名，用于出图前缀")
    ap.add_argument("--ratio", default="", help="画幅，默认跟随形态")
    ap.add_argument("--source", default="", help="出处/来源，写进看板副标题")
    ap.add_argument("--subtitle", default="", help="副标题")
    ap.add_argument("--yes", action="store_true", help="画风包自查没过也照建")
    ap.add_argument("--index", type=int, default=0, help="强制指定编号")
    ap.add_argument("--target-seconds", type=int, default=160,
                    help="目标总时长（秒），qc.py 用作加总校验")
    args = ap.parse_args()

    if args.list_styles:
        print(f"画风包目录  {ENGINE_DIR / 'styles'}\n")
        for s in list_styles():
            errs = s.validate()
            flag = "" if not errs else f"  ← {len(errs)} 处问题"
            print(f"  {s.id:16s} {s.name}  [{s.form or '形态未填'} / {s.default_ratio}]{flag}")
            if s.describe:
                print(f"  {'':16s} {s.describe}")
        print("\n  python styles.py --show <id>   查看完整内容")
        sys.exit(0)

    if not args.name:
        ap.error("必须给 --name（或跑 --list-styles 只看画风包）")

    # 画风：优先 --style 指向的包，否则内联一段，都没有就是未定。
    style = None
    if args.style:
        style = load_style(args.style)
        errs = style.validate()
        if errs:
            print(f"⚠ 画风包 {args.style} 自查未通过：")
            for e in errs:
                print(f"   ✗ {e}")
            if args.yes:
                print("   （--yes，继续）")
            else:
                sys.exit("   修好 styles/<id>.json 再建项目，或加 --yes 强制")
    elif args.inline_block:
        style = Style({"id": "inline", "name": "内联画风",
                       "positive": args.inline_block}, "命令行内联")
    else:
        print("⚠ 未指定画风。项目建成后请在 project.json 里补 style_id，")
        print("  或跑 new_project.py --list-styles 挑一个。\n")

    if style is not None and style.id == "inline":
        style_part = {"style_block": style.positive}
        ratio_default = "16:9"
    elif style is not None:
        style_part = {"style_id": style.id}
        ratio_default = style.default_ratio
    else:
        style_part = {}
        ratio_default = "16:9"

    ratio = args.ratio or ratio_default
    if ratio not in RATIO_SIZES:
        sys.exit(f"✗ 不支持的画幅 {ratio}，可选：{' / '.join(RATIO_SIZES)}")
    size = RATIO_SIZES[ratio]

    idx = args.index or next_index()
    slug = args.slug or slugify(args.name) or f"project-{idx}"
    dirname = f"{idx:03d}-{args.name}"
    root = ROOT_DIR / dirname
    if root.exists():
        sys.exit(f"✗ 目录已存在：{root}")

    for sub in ("shot", "videos", "videos_fixed", "audio", "srt"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    cfg = {
        "image_pipeline_version": 2,
        "candidate_limit": 3,
        "art_direction": {},
        "title": args.name,
        "subtitle": args.subtitle,
        "source": args.source,
        "slug": slug,
        "ratio": ratio,
        "width": size["width"],
        "height": size["height"],
        "form": style.form if style else "",
        **style_part,
        "target_seconds": args.target_seconds,
        "megapixels": 0.4,
        "csv": "shots.csv",
        "random_seed": True,
        "comfy_url": "http://127.0.0.1:8188",
        "workflows": {
            "t2i": "t2i_z_image_turbo.json",
            "edit": "edit_qwen_image_edit_2509.json",
            "i2v": "minimax_h3_i2v.json",
            "r2v": "minimax_h3_r2v.json",
        },
    }
    (root / "project.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if TEMPLATE.exists():
        shutil.copyfile(TEMPLATE, root / "shots.csv")
    else:
        sys.exit(f"✗ 缺少模板 {TEMPLATE}")

    asset_header = "asset,name,kind,image_route,prompt,references,edit_source,mask,crop,requires,style_id,scene_state,seed\n"
    (root / "asset_jobs.csv").write_text(asset_header, encoding="utf-8")
    (root / "assets.json").write_text('{"schema": 1, "items": {}}', encoding="utf-8")

    # 人读的形态说明（H3 视频提示词要照着写运动预算）
    if style is not None:
        m, c = style.motion, style.continuity
        lines = [
            f"# {args.name} · 视觉设定",
            "",
            f"制作形态：{style.form or '(未填)'}",
            f"画风包：`_engine/styles/{style.id}.json`"
            + ("（内联，未纳管）" if style.id == "inline" else ""),
            f"画幅：{ratio}（{size['width']}x{size['height']}）",
            f"- {size['note']}",
            "",
            "> **本文件由画风包渲染生成，不要手改。**",
            "> 要改画风：改 `_engine/styles/<id>.json`，或在本项目 project.json 里写",
            "> `style_override` 做局部覆盖，再重新生成本文件。",
            "",
            "## 叙事职责",
            "",
            style.describe or "（画风包没填 describe，务必补上）",
            "",
            "## 风格块（gen_images.py 自动注入，每镜提示词里不要写风格）",
            "",
            "```",
            style.positive or "（本画风未定义 positive，见画风包的 notes）",
            "```",
            "",
        ]
        if style.negative:
            lines += ["## 负向词（只在有负向输入口的工作流生效）", "",
                      "```", style.negative, "```", ""]
        lines += ["## 运动预算（写 h3_prompt 时必须遵守）", "",
                  f"- 策略：**{m.get('policy', '未填')}**"]
        lines += [f"- 允许：{w}" for w in m.get("allow", [])]
        lines += [f"- 禁止：{w}" for w in m.get("forbid", [])]
        lines += ["", "## 连续性必带项（本画风下跨镜必须锚住的东西）", ""]
        lines += [f"- 必带：{w}" for w in c.get("must", [])]
        lines += [f"- 可省：{w}" for w in c.get("skip", [])]
        if style.lexicon:
            lines += ["", "## 词表（qc.py 会查）", ""]
            lines += [f"- 推荐：{w}" for w in style.lexicon.get("prefer", [])]
            lines += [f"- 禁用：{w}" for w in style.lexicon.get("forbid", [])]
        if style.notes:
            lines += ["", "## 踩坑记录", "", style.notes]
        lines += ["", "画风决策流程见 `_engine/画风决策.md`。", ""]
        (root / "视觉设定.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"✓ 项目已创建：{root}")
    print(f"  编号目录  {dirname}")
    print(f"  出图前缀  {slug}")
    print(f"  画风包    {style.id if style else '(未指定)'}   "
          f"形态 {style.form if style else '-'}   "
          f"画幅 {ratio} ({size['width']}x{size['height']})")
    print(f"  工作流    t2i/edit/i2v/r2v（全部指向 _engine/workflows/）")
    print()
    print("下一件事：把 story / 分镜填进 shots.csv，然后：")
    print(f"  python _engine\\qc.py --project \"{root}\"         # 先守规则")
    print(f"  node _engine\\gen_html.js                        # 生成看板")
    print(f"  python _engine\\gen_images.py --dry-run          # 检查清单")


if __name__ == "__main__":
    main()
