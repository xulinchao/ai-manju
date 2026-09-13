# -*- coding: utf-8 -*-
"""
run_batch.py — 《阴兵过境》H3 批量提交脚本
读取同目录 shots.csv，把每镜的首/尾帧图片和 H3 提示词灌进 ComfyUI 工作流，逐镜排队生成。

用法：
  1. 把 ComfyUI 导出的两个工作流放到本目录（Dev mode → Export (API)）：
     - workflow_i2v.json   图生视频工作流（仅首帧镜用）
     - workflow_fl2v.json  首尾帧工作流（首尾帧镜用）
  2. python run_batch.py --inspect     # 第一次先跑这个：打印两个工作流所有节点，确认 NODE_MAP
  3. 填好下方 NODE_MAP_I2V / NODE_MAP_FL2V 后：
     python run_batch.py               # 跑全部未完成的镜头（按 mode 自动选工作流）
     python run_batch.py --only S11,S12  # 只跑指定镜头
     python run_batch.py --only S11 --force  # 强制重跑 S11（忽略已有产出）
     python run_batch.py --fix-only    # 只做修复回填（videos/ → videos_fixed/），不提交生成
     python run_batch.py --dry-run     # 只检查清单和图片，不提交

断点续跑：已有 videos/Sxx.mp4 的镜头自动跳过，中途中断后重跑同一条命令即可接着跑。
自动修复：每镜下载完成后立即无损重封装+去音轨，副本存 videos_fixed/（进剪映用这个目录）；
         启动时也会自动回填 videos/ 里缺副本的旧视频，fix_h3.bat 不再需要。

依赖：仅 Python 标准库（urllib），无需 pip install。
"""
import argparse
import csv
import json
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from project import load_project, load_node_map  # noqa: E402

# ============ 配置区：全部由 project.json 驱动，无任何题材专属内容 ============
_PROJECT_ARG = None
for _i, _a in enumerate(sys.argv):
    if _a == "--project" and _i + 1 < len(sys.argv):
        _PROJECT_ARG = sys.argv[_i + 1]
        break

P = load_project(_PROJECT_ARG)

BASE_URL          = P.comfy_url
PROJECT_DIR       = P.dir
SHOT_DIR          = P.shot_dir
VIDEO_DIR         = P.video_dir
VIDEO_FIXED_DIR   = P.video_fixed_dir     # 修复副本（重封装 + 去音轨），进剪映用这个目录
CSV_PATH          = P.csv_path
WORKFLOW_I2V      = P.workflow_file("i2v")   # 图生视频（仅首帧）
WORKFLOW_FL2V     = P.workflow_file("r2v")   # r2v 双参考图（首尾帧）

# 节点映射外置到 _engine\node_map.json，换工作流不必改代码
NODE_MAP_I2V      = load_node_map(WORKFLOW_I2V.name)
NODE_MAP_FL2V     = load_node_map(WORKFLOW_FL2V.name)

# 每镜随机种子（否则所有镜头共用工作流里固定的 seed）
RANDOM_SEED       = P.random_seed

# 统一分辨率档位，由 project.json 的 megapixels 决定
MEGAPIXELS_OVERRIDE = P.megapixels

# 自动识别的关键词（inspect 之后按实际情况调整）
AUTO_FIRST_KEYWORDS = ("first", "首帧", "start")
AUTO_LAST_KEYWORDS = ("last", "尾帧", "end")
# ============================================================================


def api_post(path: str, data: dict) -> dict:
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def api_get(path: str) -> dict:
    with urllib.request.urlopen(BASE_URL + path, timeout=60) as r:
        return json.loads(r.read())


def upload_image(path: Path) -> str:
    """上传图片到 ComfyUI，返回服务器端文件名。"""
    boundary = uuid.uuid4().hex
    body = []
    body.append(f"--{boundary}\r\n".encode())
    body.append(
        f'Content-Disposition: form-data; name="image"; filename="{path.name}"\r\n'.encode()
    )
    body.append(b"Content-Type: image/png\r\n\r\n")
    body.append(path.read_bytes())
    body.append(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        BASE_URL + "/upload/image",
        data=b"".join(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["name"]


def load_shots() -> list[dict]:
    # 唯一事实源路径与文件名由 project.json 决定（默认 shots.csv）
    csv_path = CSV_PATH
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return rows


def shot_files(s: dict) -> tuple[str, str]:
    """取该镜的首/尾帧文件名：CSV 填了就用，没填则按镜头号自动推导（s14 → s14_first.png）。"""
    if P.get("image_pipeline_version"):
        from assets import Assets
        db = Assets(P.dir)
        first, _ = db.resolve(s["shot"] + ".first", "视频首图")
        last = ""
        if s["mode"] in ("firstlast", "reference"):
            last, _ = db.resolve(s["shot"] + ".last", "视频第二参考图")
        return str(first), str(last)
    sid = s["shot"].lower()
    first = (s.get("first_image") or "").strip() or f"{sid}_first.png"
    last = (s.get("last_image") or "").strip()
    if not last and s["mode"] == "firstlast":
        last = f"{sid}_last.png"
    return first, last


def check_assets(shots: list[dict]) -> list[dict]:
    """校验每镜图片是否齐备，返回可跑的镜头列表。"""
    ready, missing = [], []
    for s in shots:
        need_last = s["mode"] == "firstlast"
        try:
            first_name, last_name = shot_files(s)
        except ValueError as e:
            missing.append(s)
            print(f"  [未就绪] {s['shot']}: {e}")
            continue
        fpath = SHOT_DIR / first_name
        lpath = SHOT_DIR / last_name if need_last else None
        ok = fpath.exists() and (not need_last or (lpath and lpath.exists()))
        (ready if ok else missing).append(s)
        if not ok:
            lack = []
            if not fpath.exists():
                lack.append(first_name)
            if need_last and not (lpath and lpath.exists()):
                lack.append(last_name)
            print(f"  [缺图] {s['shot']}: 缺 {', '.join(lack)}")
    return ready


def inspect_workflow(name: str, workflow: dict):
    """打印所有节点，帮助确定 NODE_MAP。"""
    print(f"===== {name}（共 {len(workflow)} 个节点）=====")
    for nid, node in workflow.items():
        title = (node.get("_meta") or {}).get("title", "")
        ct = node.get("class_type", "?")
        widgets = {
            k: (v if isinstance(v, (int, float, str)) and len(str(v)) < 80 else "...")
            for k, v in node.get("inputs", {}).items()
            if not isinstance(v, list)
        }
        print(f"  [{nid}] {ct}  {title or ''}")
        if widgets:
            print(f"        控件: {widgets}")
    print()


def resolve_nodes(workflow: dict, node_map: dict) -> dict:
    """确定各节点 ID：优先手动配置，否则按标题/类型自动猜。"""
    resolved = dict(node_map)
    load_nodes = []  # (nid, title)
    for nid, node in workflow.items():
        ct = node.get("class_type", "").lower()
        title = ((node.get("_meta") or {}).get("title") or "").lower()
        if "loadimage" in ct:
            load_nodes.append((nid, title))
        if not resolved["prompt_text"] and "text" in ct and isinstance(
            node.get("inputs", {}).get("text"), str
        ):
            resolved["prompt_text"] = nid
        for key in ("duration",):
            if not resolved[key]:
                for w in node.get("inputs", {}):
                    if w.lower() in ("duration", "seconds", "duration_seconds", "num_frames"):
                        resolved[key] = nid
                        break

    if not resolved.get("first_image") and load_nodes:
        for nid, title in load_nodes:
            if any(k in title for k in AUTO_FIRST_KEYWORDS):
                resolved["first_image"] = nid
                break
        if not resolved.get("first_image"):
            resolved["first_image"] = load_nodes[0][0]
    if not resolved.get("last_image") and len(load_nodes) > 1:
        for nid, title in load_nodes:
            if any(k in title for k in AUTO_LAST_KEYWORDS):
                resolved["last_image"] = nid
                break
        if not resolved.get("last_image"):
            resolved["last_image"] = load_nodes[1][0]
    elif "last_image" not in resolved:
        resolved["last_image"] = ""  # i2v 单图工作流，无尾帧

    # i2v 工作流只有一张图，last_image 不参与校验
    required = [k for k in resolved if k != "duration"]
    if len(load_nodes) < 2:
        required = [k for k in required if k != "last_image"]
    missing = [k for k in required if not resolved[k]]
    if missing:
        sys.exit(f"✗ 以下节点未能确定：{missing}。请先跑 --inspect，手动填 NODE_MAP_I2V / NODE_MAP_FL2V。")
    print(f"节点映射: {resolved}")
    return resolved


def _set_text_input(node: dict, text: str, nid: str):
    """往节点里写文本：自动找到现有的字符串控件键（value/text/prompt/string）。"""
    for key in ("value", "text", "prompt", "string"):
        v = node.get("inputs", {}).get(key)
        if isinstance(v, str):
            node["inputs"][key] = text
            return
        if v is None and key in node.get("inputs", {}):
            node["inputs"][key] = text
            return
    sys.exit(
        f"✗ 提示词节点 [{nid}] ({node.get('class_type')}) 没有可写的文本控件。"
        f"现有控件: {list(node.get('inputs', {}).keys())}"
    )


def patch_workflow(workflow: dict, nodes: dict, shot: dict, first_name: str, last_name: str | None):
    import random

    wf = json.loads(json.dumps(workflow))  # deep copy
    wf[nodes["first_image"]]["inputs"]["image"] = first_name
    if last_name and nodes.get("last_image"):
        wf[nodes["last_image"]]["inputs"]["image"] = last_name
    _set_text_input(wf[nodes["prompt_text"]], shot["h3_prompt"], nodes["prompt_text"])

    # 时长：PrimitiveFloat 的 value 键直接写秒；其他写法按控件名兜底
    if nodes.get("duration"):
        node = wf[nodes["duration"]]
        if "value" in node.get("inputs", {}):
            node["inputs"]["value"] = float(shot["duration"])
        else:
            for w in list(node.get("inputs", {})):
                wl = w.lower()
                if wl in ("duration", "seconds", "duration_seconds"):
                    node["inputs"][w] = float(shot["duration"])
                    break
                elif wl == "num_frames":
                    node["inputs"][w] = int(shot["duration"]) * 24 + 1
                    break

    # 随机种子
    if RANDOM_SEED:
        for node in wf.values():
            if node.get("class_type") == "RandomNoise":
                node["inputs"]["noise_seed"] = random.randint(0, 2**48)

    # 分辨率档位覆盖
    if MEGAPIXELS_OVERRIDE:
        for node in wf.values():
            if node.get("class_type") == "ResolutionSelector":
                node["inputs"]["megapixels"] = MEGAPIXELS_OVERRIDE

    return wf


def wait_for(prompt_id: str, timeout: int = 1800) -> list[str]:
    """轮询直到任务完成，返回输出视频文件名列表。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(5)
        try:
            hist = api_get(f"/history/{prompt_id}")
        except Exception:
            continue
        if prompt_id in hist:
            status = hist[prompt_id].get("status", {})
            if status.get("completed"):
                outputs = hist[prompt_id].get("outputs", {})
                vids = []
                for node_out in outputs.values():
                    for key in ("gifs", "videos", "images"):
                        for item in node_out.get(key, []):
                            if item.get("type", "output") == "output" or key != "images":
                                vids.append((item["filename"], item.get("subfolder", "")))
                return vids
            if status.get("status_str") == "error":
                raise RuntimeError("ComfyUI 报错，查看终端日志")
    raise TimeoutError("等待超时（30 分钟）")


def download_video(filename: str, subfolder: str, dest: Path):
    """先写 .tmp 再改名，避免中途中断留下半截文件被误判为已完成。"""
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    q = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": "output"})
    with urllib.request.urlopen(f"{BASE_URL}/view?{q}", timeout=300) as r, open(tmp, "wb") as f:
        f.write(r.read())
    if tmp.stat().st_size < 1024:
        tmp.unlink()
        raise RuntimeError("下载文件异常（小于 1KB）")
    tmp.replace(dest)


def find_ffmpeg() -> str | None:
    """定位 ffmpeg：PATH 里找，找不到再试 ComfyUI 常见自带位置。"""
    p = shutil.which("ffmpeg")
    if p:
        return p
    for cand in (
        Path(sys.executable).parent / "ffmpeg.exe",
        Path(sys.executable).parent / "Library" / "bin" / "ffmpeg.exe",
        Path(__file__).resolve().parent / "ffmpeg.exe",
    ):
        if cand.exists():
            return str(cand)
    return None


FFMPEG = find_ffmpeg()


def fix_video(src: Path, dest: Path) -> bool:
    """无损重封装 + 剥离音轨：先出 .tmp 再改名，避免留下半截副本。"""
    if not FFMPEG:
        print(f"  [WARN] 找不到 ffmpeg，跳过修复。副本未生成：{dest.name}")
        return False
    dest.parent.mkdir(exist_ok=True)
    tmp = dest.with_suffix(".tmp.mp4")
    r = subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-i", str(src), "-map", "0:v", "-c:v", "copy", "-an", str(tmp)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"  [WARN] ffmpeg 修复失败：{src.name} {r.stderr.strip()[:120]}")
        if tmp.exists():
            tmp.unlink()
        return False
    tmp.replace(dest)
    return True


def backfill_fixed() -> int:
    """把 videos/ 里还没有修复副本的视频补一遍（启动时自动执行）。"""
    if not VIDEO_DIR.exists():
        return 0
    n = 0
    for v in sorted(VIDEO_DIR.glob("*.mp4")):
        dest = VIDEO_FIXED_DIR / v.name
        if dest.exists():
            continue
        if fix_video(v, dest):
            print(f"  [FIX] {v.name} → videos_fixed/{v.name}")
            n += 1
    return n


def load_workflows() -> dict:
    """加载两种工作流：i2v（仅首帧镜用）和 fl2v（首尾帧镜用）。"""
    wfs = {}
    if WORKFLOW_I2V.exists():
        wfs["first"] = json.loads(WORKFLOW_I2V.read_text(encoding="utf-8"))
    if WORKFLOW_FL2V.exists():
        wfs["firstlast"] = json.loads(WORKFLOW_FL2V.read_text(encoding="utf-8"))
    if not wfs:
        sys.exit(
            "✗ 找不到工作流文件。在 ComfyUI：设置(齿轮) → 开启 Dev mode → 菜单 Workflow → Export (API)，\n"
            "  分别打开「图生视频」工作流导出为 workflow_i2v.json、\n"
            "  「首尾帧」工作流导出为 workflow_fl2v.json，放到：\n"
            f"  {PROJECT_DIR}"
        )
    for mode in ("first", "firstlast"):
        if mode not in wfs:
            sys.exit(f"✗ 缺少 {WORKFLOW_I2V if mode == 'first' else WORKFLOW_FL2V}（{mode} 模式的工作流）")
    return wfs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=_PROJECT_ARG)
    ap.add_argument("--ack-reference-semantics", action="store_true", help="明确接受旧 firstlast 仅为双参考图")
    ap.add_argument("--only", help="只跑指定镜头，逗号分隔，如 S11,S12")
    ap.add_argument("--dry-run", action="store_true", help="只校验，不提交")
    ap.add_argument("--inspect", action="store_true", help="打印两个工作流的节点后退出")
    ap.add_argument("--force", action="store_true", help="强制重跑（忽略已有产出，通常配合 --only 用）")
    ap.add_argument("--fix-only", action="store_true", help="只做修复回填（videos/ → videos_fixed/），不提交生成")
    args = ap.parse_args()

    if args.fix_only and args.dry_run:
        print("只读：未执行视频修复回填")
        return

    if args.fix_only:
        n = backfill_fixed()
        print(f"修复完成：本次回填 {n} 条，videos_fixed/ 共 {len(list(VIDEO_FIXED_DIR.glob('*.mp4')))} 条。")
        return

    workflows = load_workflows()

    if args.inspect:
        if "first" in workflows:
            inspect_workflow("workflow_i2v.json（图生视频 · 仅首帧）", workflows["first"])
        if "firstlast" in workflows:
            inspect_workflow("minimax_h3_r2v.json（多参考图，非首尾帧绑定）", workflows["firstlast"])
        print("→ 从上面找到对应节点 ID，分别填进脚本顶部的 NODE_MAP_I2V / NODE_MAP_FL2V。")
        print("  首帧/尾帧 = LoadImage 类节点；提示词 = 文本控件节点；时长 = 带 duration/seconds/num_frames 控件的节点")
        return

    shots = load_shots()
    if args.only:
        wanted = {s.strip().upper() for s in args.only.split(",")}
        shots = [s for s in shots if s["shot"] in wanted]

    legacy = [s["shot"] for s in shots if s["mode"] == "firstlast"]
    if legacy:
        print("警告：firstlast 实际连接 H3 多参考图，不是首尾帧时间绑定：" + ", ".join(legacy))
        if not args.dry_run and not args.ack_reference_semantics:
            sys.exit("请明确使用 reference 模式，或确认语义后加 --ack-reference-semantics；未提交生成")
    for s in shots:
        if s["mode"] == "reference":
            s["mode"] = "firstlast"  # internal dispatch only; the user-facing mode is reference
        if s["mode"] not in ("first", "firstlast"):
            sys.exit(f"未知视频模式：{s['mode']}")
    print("== 资产检查 ==")
    ready = check_assets(shots)
    print(f"可跑 {len(ready)} 镜 / 共 {len(shots)} 镜")

    if args.dry_run:
        if len(ready) != len(shots):
            sys.exit(1)
        return

    # 启动时先回填：videos/ 里有、videos_fixed/ 里还没有的，先补上修复副本
    n = backfill_fixed()
    if n:
        print(f"（回填修复副本 {n} 条 → videos_fixed/）")

    if not ready:
        sys.exit("✗ 没有可跑的镜头")

    # 按模式分别解析节点映射
    nodes_by_mode = {}
    for mode in ("first", "firstlast"):
        wf = workflows[mode]
        print(f"\n== 解析 {mode} 工作流节点 ==")
        nodes_by_mode[mode] = resolve_nodes(wf, NODE_MAP_I2V if mode == "first" else NODE_MAP_FL2V)

    VIDEO_DIR.mkdir(exist_ok=True)

    failures = 0
    for s in ready:
        shot_id = s["shot"]
        out_file = VIDEO_DIR / f"{shot_id}.mp4"
        if out_file.exists() and not args.force:
            print(f"[SKIP] {shot_id} 已有产出（要重跑加 --force）")
            continue
        workflow = workflows[s["mode"]]
        nodes = nodes_by_mode[s["mode"]]
        first_name_csv, last_name_csv = shot_files(s)
        print(f"[RUN ] {shot_id} ({s['duration']}s, {s['mode']}) …", flush=True)
        try:
            first_name = upload_image(SHOT_DIR / first_name_csv)
            last_name = upload_image(SHOT_DIR / last_name_csv) if last_name_csv else None
            wf = patch_workflow(workflow, nodes, s, first_name, last_name)
            resp = api_post("/prompt", {"prompt": wf, "client_id": uuid.uuid4().hex})
            prompt_id = resp["prompt_id"]
            vids = wait_for(prompt_id)
            if vids:
                download_video(vids[0][0], vids[0][1], out_file)
                fixed = VIDEO_FIXED_DIR / f"{shot_id}.mp4"
                ok = fix_video(out_file, fixed)
                print(f"[DONE] {shot_id} → videos/{out_file.name}" + (f" + videos_fixed/{fixed.name}" if ok else ""))
            else:
                failures += 1
                print(f"[FAIL] {shot_id} 完成但没找到视频输出，检查节点输出名")
        except Exception as e:
            failures += 1
            print(f"[FAIL] {shot_id}: {e}")

    print("\n== 批量结束 ==")
    done = len(list(VIDEO_DIR.glob("*.mp4")))
    fixed = len(list(VIDEO_FIXED_DIR.glob("*.mp4"))) if VIDEO_FIXED_DIR.exists() else 0
    print(f"videos/ 里现有 {done} 条；videos_fixed/ 里 {fixed} 条修复副本（进剪映用 videos_fixed/）。")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
