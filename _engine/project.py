# -*- coding: utf-8 -*-
"""
project.py — 项目配置加载（题材与引擎的解耦层）

所有脚本不再假设自己在哪个项目里，一律从这里取路径和配置：

    from project import P
    P.dir / P.shot_dir / P.csv / P.workflow_file("t2i") / P.style_block / ...

设计取舍：
  · 配置文件用 project.json 而不是 project.yaml —— 本机没装 pyyaml，且 JSON 在
    Python 与 Node（gen_html.js）两侧都能零依赖读取。
  · 项目定位顺序：命令行 --project > 环境变量 COMIC_PROJECT > 自动扫描工程根目录。
    自动扫描只在「根目录下唯一一个 project.json」时命中，多个则要求显式指定。

工程约定：
  I:\\Project\\AI漫剧\\
    _engine\\          ← 通用脚本，不含任何题材专属内容
    001-xxx\\          ← 项目实例
      project.json
      shots.csv        ← 唯一事实源
      shot\\ videos\\ videos_fixed\\ audio\\ srt\\
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from styles import style_from_config  # noqa: E402

ENGINE_DIR = Path(__file__).resolve().parent
ROOT_DIR = ENGINE_DIR.parent
WORKFLOWS_DIR = ENGINE_DIR / "workflows"
PROJECT_FILE = "project.json"

DEFAULTS = {
    "title": "未命名项目",
    "subtitle": "",
    "source": "",           # 出处/改编来源，写进看板副标题
    "slug": "project",      # 出图前缀 + 目录名，必须英文短横线
    "ratio": "16:9",
    "width": 1280,
    "height": 720,
    "form": "",             # 制作形态：水墨笔触 / 二维动态漫 / 国漫二次元 / ...
    "target_seconds": 180,  # qc.py 用：时长加总校验目标
    "megapixels": 0.4,      # H3 视频档位
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


class Project:
    def __init__(self, cfg: dict, root: Path):
        self._cfg = cfg
        self.dir = root
        # 画风包：优先 style_id 指向的 _engine/styles/<id>.json，
        # 老项目只有 style_block 时包成一个 inline 包，行为与 v1 完全一致。
        self.style = style_from_config(cfg)

    def __getattr__(self, name):
        # 配置优先，取不到回落到 DEFAULTS
        try:
            return self._cfg[name]
        except KeyError:
            if name in DEFAULTS:
                return DEFAULTS[name]
            raise AttributeError(
                f"project.json 里没有配置项 '{name}'，也没有默认值"
            )

    def get(self, name, fallback=None):
        return self._cfg.get(name, DEFAULTS.get(name, fallback))

    # ---- 路径 ----
    @property
    def shot_dir(self) -> Path:
        return self.dir / "shot"

    @property
    def video_dir(self) -> Path:
        return self.dir / "videos"

    @property
    def video_fixed_dir(self) -> Path:
        return self.dir / "videos_fixed"

    @property
    def csv_path(self) -> Path:
        return self.dir / self.csv

    def workflow_file(self, kind: str) -> Path:
        """返回该用途的工作流绝对路径。项目可用 workflows.<kind> 覆盖文件名。"""
        name = self.get("workflows", {}).get(kind)
        if not name:
            raise SystemExit(f"✗ project.json 的 workflows 里没有 '{kind}'")
        return WORKFLOWS_DIR / name

    # ---- 提示词 ----
    @property
    def style_block(self) -> str:
        """兼容旧代码：等于当前画风包的 positive。"""
        return self.style.positive

    # ---- 角色 / 场景锚 ----
    @property
    def anchor_dir(self) -> Path:
        return self.dir / "anchor"

    def load_anchor(self, kind: str, aid: str) -> str | None:
        """读一个锚的 anchor_prompt。kind = characters | locations。

        锚文件路径：<project>/anchor/<kind>/<aid>.json
        找不到返回 None（调用方按普通文本原样保留占位符即可）。
        """
        p = self.anchor_dir / kind / f"{aid}.json"
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        return (data.get("anchor_prompt") or "").strip() or None

    def _inject_anchors(self, text: str) -> str:
        """把提示词里的 @角色id / @场景id 占位符替换成对应 anchor_prompt。

        占位符写法：@zhangtianshi  /  @shuzhai
        找不到锚时原样保留（不静默吞掉，让 qc 能抓出来）。
        """
        import re
        out = text
        # 角色锚：@角色id
        def _repl(m):
            full = m.group(0)
            aid = m.group(1)
            ap = self.load_anchor("characters", aid)
            return ap if ap else full
        out = re.sub(r"@([a-zA-Z][\w-]*)", _repl, out)
        # 场景锚：@#场景id （用 # 前缀与角色区分）
        def _repl_loc(m):
            full = m.group(0)
            aid = m.group(1)
            ap = self.load_anchor("locations", aid)
            return ap if ap else full
        out = re.sub(r"@#([a-zA-Z][\w-]*)", _repl_loc, out)
        return out

    def compose_prompt(self, shot_prompt: str, mode: str = "t2i",
                       style=None, preserve_layout=True) -> str:
        """把画风注入到每镜提示词。每镜提示词本身不写风格。

        style —— 可选，给某个镜头换一套画风（闪回 / 梦境 / 阴阳两界）。
                 用 _engine/styles.py 的 Style.override() 造出来传进来，
                 不改全局画风包。空值时用项目的默认画风。

        mode="t2i"  —— 文生图：风格块前置，作为生成条件。
        mode="edit" —— 改图：风格块放进「保留」语义。
            实测教训（2026-09-12）：夜间化这类全局光照重绘，Edit 会把原图风格
            一起冲掉（首帧水墨 → 尾帧变暗色数字插画）。所以尾帧不能只靠输入图
            继承风格，必须显式要求保留，这正是 Lira「【更改】/【精确保留】」
            模板的用法。
        """
        st = style or self.style
        sb = st.positive.strip()
        # 锚注入：@角色id / @#场景id → anchor_prompt。在拼风格块之前做，
        # 这样 t2i 和 edit 两条路径都统一生效。
        sp = self._inject_anchors((shot_prompt or "").strip())
        if not sb:
            return sp
        if mode == "edit":
            keep = ("Preserve the original composition, camera position, framing, "
                    "object positions and scale; preserve this rendering style — " + sb
                    if preserve_layout else "Preserve this rendering style — " + sb)
            return f"{sp}\n{keep}" if sp else keep
        return f"{sb}\n{sp}" if sp else sb

    # ---- 展示 ----
    def describe(self) -> str:
        return (
            f"项目：{self.title}\n"
            f"  目录   {self.dir}\n"
            f"  形态   {self.form or '(未定)'}   画幅 {self.ratio} ({self.width}x{self.height})\n"
            f"  画风   {self.style.id}（{self.style.name}）\n"
            f"  CSV    {self.csv_path.name}\n"
            f"  工作流 " + ", ".join(f"{k}={v}" for k, v in self.get("workflows").items())
        )


def _scan_projects() -> list[Path]:
    """扫描工程根目录下所有含 project.json 的子目录（跳过 _engine 与下划线开头）。"""
    found = []
    if not ROOT_DIR.exists():
        return found
    for d in ROOT_DIR.iterdir():
        if not d.is_dir():
            continue
        if d.name.startswith("_") or d.name == "_engine":
            continue
        if (d / PROJECT_FILE).exists():
            found.append(d)
    return sorted(found)


def find_project_dir(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_dir():
            # 允许传内部的 project.json 路径
            if p.is_file() and p.name == PROJECT_FILE:
                return p.parent
            raise SystemExit(f"✗ 找不到项目目录：{explicit}")
        if not (p / PROJECT_FILE).exists():
            raise SystemExit(f"✗ {p} 里没有 {PROJECT_FILE}")
        return p

    env = os.environ.get("COMIC_PROJECT")
    if env:
        return find_project_dir(env)

    found = _scan_projects()
    if len(found) == 1:
        return found[0]
    if not found:
        raise SystemExit(
            f"✗ 在 {ROOT_DIR} 下没找到任何含 {PROJECT_FILE} 的项目目录。\n"
            f"  先跑：python _engine\\new_project.py --name <名称>"
        )
    lst = "\n".join(f"    - {d.name}" for d in found)
    raise SystemExit(
        f"✗ 找到多个项目，请用 --project 指定要跑哪一个：\n{lst}"
    )


def load_project(explicit: str | None = None) -> Project:
    root = find_project_dir(explicit)
    cfg = json.loads((root / PROJECT_FILE).read_text(encoding="utf-8"))
    return Project(cfg, root)


def _read_node_map() -> dict:
    nm_path = ENGINE_DIR / "node_map.json"
    if not nm_path.exists():
        raise SystemExit(f"✗ 缺少 {nm_path}")
    return json.loads(nm_path.read_text(encoding="utf-8"))


def load_node_map(kind: str) -> dict:
    """读该工作流的节点映射。下划线开头的键是元信息，不作为 node id 返回。"""
    data = _read_node_map()
    if kind not in data:
        raise SystemExit(
            f"✗ node_map.json 里没有 '{kind}' 的映射。可用："
            + ", ".join(k for k in data if not k.startswith("_"))
        )
    return {k: v for k, v in data[kind].items() if not k.startswith("_")}


def load_size_node(kind: str) -> str:
    """文生图工作流里负责设定宽高的 latent 节点 id（画幅由 project.json 决定）。"""
    return _read_node_map().get(kind, {}).get("_size_node", "")


def load_shots(P: Project) -> list[dict]:
    import csv as _csv
    if not P.csv_path.exists():
        raise SystemExit(f"✗ 找不到唯一事实源：{P.csv_path}")
    with open(P.csv_path, encoding="utf-8-sig", newline="") as f:
        return list(_csv.DictReader(f))


def shot_files(s: dict) -> tuple[str, str]:
    """取该镜首/尾帧文件名：CSV 填了就用，没填则按镜头号推导（S14 → s14_first.png）。"""
    sid = s["shot"].lower()
    first = (s.get("first_image") or "").strip() or f"{sid}_first.png"
    last = (s.get("last_image") or "").strip()
    if not last and (s.get("mode") or "").strip() == "firstlast":
        last = f"{sid}_last.png"
    return first, last


# 供 `python project.py` 自查
if __name__ == "__main__":
    print("=== 工程信息 ===")
    print(f"引擎目录  {ENGINE_DIR}")
    print(f"工作流目录 {WORKFLOWS_DIR}")
    print()
    print("=== 可用项目 ===")
    for d in _scan_projects():
        try:
            cfg = json.loads((d / PROJECT_FILE).read_text(encoding="utf-8"))
            print(f"  {d.name}  →  {cfg.get('title', '?')} "
                  f"[{cfg.get('form', '形态未定')} / {cfg.get('ratio', '?')}]")
        except Exception as e:
            print(f"  {d.name}  (读取失败: {e})")
