# OpenMontage 与本地漫剧工作流的融合评估

结论：**能融合，但不能「合并」。** 两者是不同层级的系统——本地引擎解决「系列化一致性」，OpenMontage 解决「从想法到成片的广度」。正确做法是分层嫁接：本地引擎保持为图片与一致性内核，OpenMontage 提供视频阶段的方法论与渲染层。

## 一、定位对比

| 维度 | 本地 _engine | OpenMontage |
|---|---|---|
| 编排者 | Python 代码，确定性可复现 | AI agent 读 YAML manifest，自由度大 |
| 状态载体 | assets.json / CSV | checkpoint_<stage>.json + events.jsonl |
| 门禁 | 预检、stale 传播、候选上限、审核登记 | 审批门、pre-compose 校验、渲染后自检 |
| 供应商 | 单一本地 ComfyUI（Z-Image / Qwen Edit）+ H3 | 60+ 供应商，7 维评分选择 |
| 强项 | 系列化一致性、可返工、可复现 | 广度、快速出首稿、多平台适配 |
| 渲染 | 无（当前仅图片阶段） | Remotion / HyperFrames / FFmpeg |
| 许可 | 自有 | AGPLv3 |

两套体系在架构基因上是同源的：都是「agent + 文件状态 + 阶段门禁」。差异在编排哲学——本地引擎是**工程管线**（先定规则再执行），OpenMontage 是**创作工作室**（agent 在 manifest 约束内自由发挥）。

## 二、值得吸收的部分（按价值排序）

### 1. 渲染后自检 + slideshow 风险评分（最高价值）

OpenMontage 定义了一个关键检查：**slideshow 风险评分**，用 6 个维度诊断「动画 PPT」——重复性、装饰性画面占比、运动强度、镜头意图、文字依赖度、无依据的"电影感"宣称。

这对本项目是直击要害的。当前视频形态是「静图 + 字幕」，正是 slideshow 风险最高的一类。**在写视频规范前先引入这套评分，等于给成片质量装了一个可量化的守门人**——否则很容易产出看着完整、实际是配乐 PPT 的东西。

配套的渲染后自检清单可直接借用：ffprobe 校验、4 个位置抽帧查黑帧与破损叠加、音频静音与削波检测、字幕存在性检查。

### 2. HyperFrames 作为动效层

- OpenMontage 把 HyperFrames 定位为 motion-graphics 重型的渲染运行时（HTML + GSAP），与 Remotion（React + 数据驱动）分工明确。
- `ink-theater` 是完整可用的水墨动画引擎：墨迹笔触、手绘 boil、闭式弹簧物理、2D IK 骨骼、CMU 动捕重定向（12 个动作）。画风方向与 004 的「新中式动画电影」高度一致。
- 用途：片头、字幕卡、转场、局部强调动效——用它给静图+字幕的形态补上「真实运动」，正面解决 slideshow 风险。

### 3. 决策日志

本项目教训目前分散在对话与 memory 日志里。OpenMontage 的做法是每个选择（供应商、风格、声音、渲染器、任何降级）都记录备选方案、置信度与理由，跨阶段累积。可在 `assets.json` 旁增 `decision_log.jsonl`，把「为什么选这个画风/这个版本」结构化留存。

### 4. 实时看板思路

backlot 用文件监听 + SSE，从状态文件（checkpoint / events.jsonl）派生全部视图，且支持时间戳回放。现有 `review_board.py` 是静态生成 HTML，可升级为监听式。

### 5. stage director skill 的双文件结构

OpenMontage 每个阶段 = YAML manifest（做什么、工具、门禁）+ markdown skill（怎么做）。本项目 `图片制作流程.md` 已是 skill 形态的雏形，可直接照此扩展出视频阶段的 manifest + skill 两份文件。

## 三、不建议引入的部分

| 项 | 理由 |
|---|---|
| 100+ 工具 / 60+ 供应商编排层 | 本项目的问题是本地一致性，不是供应商选择，引入即过度工程 |
| 网络调研 stage（15-25 次检索） | 古籍改编不需要；且与反幻觉要求已有的人工核实方式重叠 |
| agent-improvised 流程替代确定性引擎 | 漫剧产出需要可复现，自由度对单条精品有利、对系列化有害 |
| 直接复制其代码 | AGPLv3 传染。本仓库现为公开仓库，只借鉴方法，不引入代码；skill 文档作知识参考 |

## 四、落地路线

1. **004 收尾后**，新建 `_engine/视频制作流程.md`（stage skill）+ `视频阶段.yaml`（manifest），明确五道门：分镜审核 → 首尾帧绑定 → 视频候选审核 → 渲染后自检 → 成片。
2. **视频候选复用现有机制**：`candidates/<shot>/vNNN.mp4` + approved 登记 + stale 传播，与图片流程保持一致的操作手感。
3. **先做 5 秒试点**：验证 H3 的 reference 语义，同时用 HyperFrames 做一版字幕卡/片头，对比「纯静图+字幕」与「加动效层」的 slideshow 风险评分差值，用数据决定投入比例。

## 五、遗留风险

- H3 firstlast → reference 的语义尚未实测，视频规范不能建立在这个假设上。
- HyperFrames 是 Node 依赖，需与图片管线做进程隔离，避免与「不同时运行多个生产进程」的约束冲突。
