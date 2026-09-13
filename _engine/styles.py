# -*- coding: utf-8 -*-
"""
styles.py — 画风包加载器（引擎与画风的解耦层）

为什么要有这一层：
    v1 的画风只是 project.json 里一个自由字符串 style_block。它能解决「换风格改一处」，
    但解决不了三件事——
      1. 负向词没地方放（gen_images.py 里明确写着「负向保持模板原值，不动」）
      2. 采样参数写死在 workflows/*.json，改了会污染所有项目
      3. 运动预算 / 连续性必带项只写在 md 里给人看，机器读不到
    结果是：名义上一个字段搞定，真要换干净还得去 ComfyUI 改全局工作流。

v2 把画风打包成 `_engine/styles/<id>.json`，一份包 = 一个完整的画风决策：

    id / name / form          身份
    positive                  正向风格块（t2i 前置注入；edit 走保留语义）
    negative                  负向 bundle（工作流有负向节点才生效）
    params                    采样参数覆盖，按工作流或 _all 生效
    motion                    运动预算（机器可读，qc 用）
    continuity                跨镜必带 / 可省
    lexicon.prefer/forbid     推荐词 / 禁用词（qc 用）
    notes                     踩过什么坑

project.json 只写：
    "style_id": "ink_wash"
    "style_override": { "positive": "..." }     # 可选，局部微调

向后兼容：老项目只有 style_block 没有 style_id 也能继续跑，会包成一个 ad-hoc 包
（id="inline"）。所以 001 / 002 不用改。

用法：
    python styles.py                       # 列出所有可用画风包
    python styles.py --show ink_wash       # 打印某个包的全部内容
    python styles.py --check 002-水墨验证   # 检查某项目能解析出什么画风
"""

import json
import sys
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
ROOT_DIR = ENGINE_DIR.parent
STYLE_DIR = ENGINE_DIR / "styles"

INLINE_ID = "inline"  # project.json 里直接写 style_block 的旧项目

# 引擎级禁用词：与具体画风无关，是本地管线的硬事实。
# 每一条都来自实测踩坑，见 提示词写法规范.md §8。qc.py 会逐镜扫。
GLOBAL_FORBID = {
    # ① 画幅属于 project.json，写进提示词 = 画幅失控
    "vertical 9:16": "画幅由 project.json 决定，提示词里不许提比例与方向",
    "9:16": "同上；若本意不是画幅请换写法",
    "16:9": "同上",
    "aspect ratio": "同上",
    "ar:": "同上",
    "4:3": "同上",
    # ② 工作流作废标签，模型会当画风格的旗标用
    "masterpiece": "质量标签对 Z-Image 无效，还会干扰构图",
    "best quality": "同上",
    "4k": "分辨率是出图参数，不是画面描述",
    "8k": "同上",
    # ③ ghost 会被引申成实体角色
    "ghost": "实测：写成 small cold green flames 才不会被理解成幽灵",
    "ghost-fire": "同上",
    # ④ 抽象动感词会把特写镜头带跑成风景
    #    2026-09-12 003 实测：S11「水滴落在小孩额头，水光四溅」→ 画出抽象山水，人脸全无。
    #    景别已经写明「大特写」，但「四溅」这个词把画面的主导权交给了水。
    #    换成实体描述（「一滴水正落在眉心」）即可锁定主体。
    "四溅": "抽象动感词会夺走画面主导权，特写镜头尤其会被带跑成风景；改写成实体描述",
    "光斑": "同上；水墨里没有光源，光斑会破坏留白体系",
    "波光": "同上",
    "飞溅": "同上",
}


class Style:
    """一个画风包。字段缺失一律给安全的空值，不让调用方到处 try/except。"""

    def __init__(self, data: dict, source: str = ""):
        self._d = data
        self.source = source

    # ---- 身份 ----
    @property
    def id(self) -> str:
        return self._d.get("id", INLINE_ID)

    @property
    def name(self) -> str:
        return self._d.get("name", "内联画风")

    @property
    def form(self) -> str:
        return self._d.get("form", "")

    @property
    def describe(self) -> str:
        return self._d.get("describe", "")

    @property
    def default_ratio(self) -> str:
        return self._d.get("default_ratio", "16:9")

    # ---- 提示词 ----
    @property
    def positive(self) -> str:
        return (self._d.get("positive") or "").strip()

    @property
    def negative(self) -> str:
        return (self._d.get("negative") or "").strip()

    # ---- 运动 / 连续性 / 词表 ----
    @property
    def motion(self) -> dict:
        return self._d.get("motion") or {}

    @property
    def continuity(self) -> dict:
        return self._d.get("continuity") or {}

    @property
    def lexicon(self) -> dict:
        return self._d.get("lexicon") or {}

    @property
    def notes(self) -> str:
        return self._d.get("notes", "")

    # ---- 参数 ----
    @property
    def params(self) -> dict:
        return self._d.get("params") or {}

    def params_for(self, workflow_name: str) -> dict:
        """取作用于某个工作流的参数覆盖。`_all` 永远生效，被具体工作流同名键覆盖。"""
        merged = dict(self.params.get("_all") or {})
        merged.update(self.params.get(workflow_name) or {})
        return merged

    # ---- 派生 ----
    def override(self, patch: dict) -> "Style":
        """用 project.json 的 style_override / CSV 的 style_override 列做局部覆盖。

        两种写法：
            {"positive": "..."}         整段替换（项目要换一种说法时）
            {"positive_append": "..."}  追加到原风格块后面（补一句题材专属禁令时）
        后者更常用——画风包保持通用，题材专属的一两句在项目里追加。
        """
        if not patch:
            return self
        import copy
        nd = copy.deepcopy(self._d)
        patch = dict(patch)

        append = patch.pop("positive_append", "")
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(nd.get(k), dict):
                nd[k].update(v)
            else:
                nd[k] = v

        if append:
            base = (nd.get("positive") or "").strip()
            # 调用方常顺手带上前导标点，去掉再拼，避免出现「；；」
            append = append.strip().lstrip("；;,，、 ")
            sep = "" if (not base or base.endswith("；")) else "；"
            nd["positive"] = f"{base}{sep}{append}"

        return Style(nd, self.source + " +override")

    def validate(self) -> list[str]:
        """自查：返回问题清单（空列表 = 通过）。"""
        errs = []
        if not self.positive and not self.negative:
            errs.append("positive 与 negative 都为空，这个包等于没生效")

        # 程度词不可判定 —— 实测踩坑④
        vague = ["适当", "略微", "一些", "适度", "比较", "稍微", "尽量"]
        hit = [w for w in vague if w in self.positive]
        if hit:
            errs.append(
                f"positive 里有无法判定的程度词 {'/'.join(hit)}"
                " → 建议结合样图明确程度；数字比例也不代表模型能精确执行"
            )

        # 风格层越界 —— 画风决策.md §2
        banned = ["角色是", "身穿红衣", "在画面左侧", "手里拿着"]
        hit2 = [w for w in banned if w in self.positive]
        if hit2:
            errs.append(
                f"positive 疑似越界写到具体事实（{'/'.join(hit2)}）"
                " → 风格层不许决定身份/地理/剧情"
            )
        return errs

    def describe_short(self) -> str:
        return f"{self.id:16s} {self.name}  [{self.form or '形态未填'}]"

    def describe_full(self) -> str:
        out = [
            f"画风包  {self.id}  （{self.source or '未标注来源'}）",
            f"  名称      {self.name}",
            f"  形态      {self.form or '(未填)'}    默认画幅 {self.default_ratio}",
        ]
        if self.describe:
            out.append(f"  职责      {self.describe}")
        out += [
            "",
            "  ── positive ──",
            f"  {self.positive or '(空)'}",
        ]
        if self.negative:
            out += ["", "  ── negative ──", f"  {self.negative}"]
        if self.params:
            out += ["", "  ── params ──"]
            for wf, rules in self.params.items():
                out.append(f"    {wf}: {rules}")
        m = self.motion
        if m:
            out += [
                "",
                "  ── motion ──",
                f"    策略 {m.get('policy', '?')}",
                f"    可动 {m.get('allow', [])}",
                f"    禁止 {m.get('forbid', [])}",
            ]
        c = self.continuity
        if c:
            out += [
                "",
                "  ── continuity ──",
                f"    必带 {c.get('must', [])}",
                f"    可省 {c.get('skip', [])}",
            ]
        lx = self.lexicon
        if lx:
            out += [
                "",
                "  ── lexicon ──",
                f"    推荐 {lx.get('prefer', [])}",
                f"    禁用 {lx.get('forbid', [])}",
            ]
        if self.notes:
            out += ["", "  ── notes ──", f"  {self.notes}"]
        errs = self.validate()
        out += ["", "  ── 自查 ──"]
        out += [f"    ✗ {e}" for e in errs] or ["    ✓ 通过"]
        return "\n".join(out)


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def list_styles() -> list[Style]:
    """扫描 styles/ 目录，按 id 排序。"""
    if not STYLE_DIR.exists():
        return []
    out = []
    for f in sorted(STYLE_DIR.glob("*.json")):
        if f.name.startswith("_"):
            continue
        out.append(Style(_read(f), f.name))
    return out


def load_style(style_id: str) -> Style:
    p = STYLE_DIR / f"{style_id}.json"
    if not p.exists():
        avail = ", ".join(s.id for s in list_styles()) or "(styles/ 目录为空)"
        raise SystemExit(f"✗ 找不到画风包 '{style_id}'。可用：{avail}")
    return Style(_read(p), p.name)


def style_from_config(cfg: dict) -> Style:
    """从 project.json 的配置解析出画风包。

    优先级：style_id 对应的包 + style_override 覆盖  >  遗留的 style_block。
    """
    sid = (cfg.get("style_id") or "").strip()
    if sid:
        st = load_style(sid)
        patch = cfg.get("style_override") or {}
        if patch:
            st = st.override(patch)
        return st

    # 旧项目：只有自由字符串
    sb = (cfg.get("style_block") or "").strip()
    if sb:
        return Style({
            "id": INLINE_ID,
            "name": cfg.get("form") or "内联画风",
            "form": cfg.get("form", ""),
            "positive": sb,
            "default_ratio": cfg.get("ratio", "16:9"),
        }, "project.json style_block（旧格式）")

    return Style({"id": "none", "name": "无画风包"}, "(未指定)")


def apply_params(wf: dict, rules: dict) -> list[str]:
    """把参数覆盖写进工作流，返回改动清单。

    key 三种写法：
        "steps"            → 找所有含 steps 控件的节点，全部改
        "57:6:steps"       → 只改节点 57:6 的 steps（精确）
        "sampler_name"     → 同上按控件名全局改
    没有对应节点就静默跳过——换工作流不该让脚本崩，dry-run 里会提示。
    """
    if not rules:
        return []

    explicit: list[tuple[str, str, object]] = []   # (node_id, widget, value)
    by_name: list[tuple[str, object]] = []         # (widget, value)

    for key, val in rules.items():
        parts = key.split(":")
        if len(parts) == 3:
            explicit.append((parts[0] + ":" + parts[1], parts[2], val))
        elif len(parts) == 2 and parts[0] in wf:
            explicit.append((parts[0], parts[1], val))
        else:
            by_name.append((parts[-1], val))

    changed = []
    for nid, widget, val in explicit:
        node = wf.get(nid)
        if node is None:
            continue
        if widget in node.get("inputs", {}):
            old = node["inputs"][widget]
            if old != val:
                node["inputs"][widget] = val
                changed.append(f"{nid}.{widget}: {old} → {val}")

    for widget, val in by_name:
        for nid, node in wf.items():
            ins = node.get("inputs", {})
            if widget in ins and widget not in ("seed", "noise_seed"):
                old = ins[widget]
                if old != val:
                    ins[widget] = val
                    changed.append(f"{nid}.{widget}: {old} → {val}")
    return changed


if __name__ == "__main__":
    ap = __import__("argparse").ArgumentParser(description="画风包管理")
    ap.add_argument("--show", help="打印某个画风包的全部内容")
    ap.add_argument("--check", help="检查某个项目解析出的画风")
    ap.add_argument("--strict", action="store_true", help="--check 时把自查警告算失败")
    a = ap.parse_args()

    if a.show:
        print(load_style(a.show).describe_full())
        raise SystemExit(0)

    if a.check:
        sys.path.insert(0, str(ENGINE_DIR))
        import project as _pj
        PP = _pj.load_project(a.check)
        st = PP.style
        print(f"项目 {PP.title}  →  画风包 {st.id}")
        print(st.describe_full())
        errs = st.validate()
        if errs and a.strict:
            raise SystemExit(1)
        raise SystemExit(0)

    print(f"画风包目录  {STYLE_DIR}")
    print(f"可用 {len(list_styles())} 个：\n")
    for s in list_styles():
        errs = s.validate()
        flag = "" if not errs else f"  ← {len(errs)} 处问题"
        print("  " + s.describe_short() + flag)
    print("\n  python styles.py --show <id>          查看包内容")
    print("  python styles.py --check <项目目录>    看某项目用到哪个包")
