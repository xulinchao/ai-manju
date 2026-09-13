// 分镜看板生成器：shots.csv（唯一事实源）+ project.json + 文件系统实时状态 → 分镜.html
// 用法：node gen_html.js [--project <目录>]
// 改分镜 = 改 CSV 一行，跑一次本脚本，看板即更新
const fs = require('fs');
const path = require('path');

// ---------- 项目定位（同 project.py：--project > 环境变量 > 自动扫描）----------
const ENGINE = __dirname;
const ROOT = path.dirname(ENGINE);
const DEFAULTS = {
  title: '未命名项目', subtitle: '', source: '', slug: 'project',
  ratio: '16:9', form: '', csv: 'shots.csv', target_seconds: 180,
};

function projectArg() {
  const i = process.argv.indexOf('--project');
  if (i >= 0 && process.argv[i + 1]) return path.resolve(process.argv[i + 1]);
  return process.env.COMIC_PROJECT ? path.resolve(process.env.COMIC_PROJECT) : null;
}

function findProject() {
  const explicit = projectArg();
  if (explicit) return explicit;
  const found = fs.readdirSync(ROOT, { withFileTypes: true })
    .filter(d => d.isDirectory() && !d.name.startsWith('_'))
    .filter(d => fs.existsSync(path.join(ROOT, d.name, 'project.json')))
    .map(d => path.join(ROOT, d.name));
  if (found.length === 1) return found[0];
  if (!found.length) throw new Error('没找到任何含 project.json 的项目目录，先跑 new_project.py');
  throw new Error('找到多个项目，请用 --project 指定：\n  ' + found.join('\n  '));
}

const PROJECT = findProject();
const CFG = Object.assign(
  {}, DEFAULTS,
  JSON.parse(fs.readFileSync(path.join(PROJECT, 'project.json'), 'utf8'))
);
const CSVNAME = CFG.csv;
const CSV = path.join(PROJECT, CSVNAME);
const OUT = path.join(PROJECT, '分镜.html');
const SHOT_DIR = path.join(PROJECT, 'shot');
const VIDEO_DIRS = ['videos_fixed', 'videos']; // 优先看修复后的

if (!fs.existsSync(CSV)) {
  console.error('✗ 找不到唯一事实源：' + CSV);
  process.exit(1);
}
console.log(`项目：${CFG.title}　[${CFG.form || '形态未定'} / ${CFG.ratio}]`);

// New projects use the version-aware review board; old projects keep their layout.
if (CFG.image_pipeline_version || fs.existsSync(path.join(PROJECT, 'assets.json'))) {
  const child = require('child_process').spawnSync(process.env.COMIC_PYTHON || 'python',
    [path.join(ENGINE, 'review_board.py'), '--project', PROJECT,
     ...(process.argv.includes('--dry-run') ? ['--dry-run'] : [])], { stdio: 'inherit' });
  process.exit(child.status === null ? 1 : child.status);
}
if (process.argv.includes('--dry-run')) { console.log('只读：旧版看板，未写文件'); process.exit(0); }

// ---------- CSV 解析 ----------
function parseCsv(text) {
  text = text.replace(/^\uFEFF/, '');
  const rows = []; let row = [], field = '', inQ = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQ) {
      if (c === '"') { if (text[i + 1] === '"') { field += '"'; i++; } else inQ = false; }
      else field += c;
    } else if (c === '"') inQ = true;
    else if (c === ',') { row.push(field); field = ''; }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(field); field = '';
      if (row.some(x => x !== '')) rows.push(row); row = [];
    } else field += c;
  }
  if (field !== '' || row.length) { row.push(field); rows.push(row); }
  const header = rows.shift();
  return rows.map(r => Object.fromEntries(header.map((h, i) => [h, (r[i] ?? '').trim()])));
}

// ---------- 文件系统状态 ----------
function listLower(dir) {
  try { return new Set(fs.readdirSync(dir).map(f => f.toLowerCase())); } catch { return new Set(); }
}
const shotFiles = listLower(SHOT_DIR);
const videoFiles = VIDEO_DIRS.map(d => ({ d, files: listLower(path.join(PROJECT, d)) }));

function statusOf(s) {
  const id = s.shot.toLowerCase();
  const first = shotFiles.has((s.first_image || id + '_first.png').toLowerCase());
  const lastNeed = ['firstlast', 'reference'].includes(s.mode);
  const last = lastNeed ? shotFiles.has((s.last_image || id + '_last.png').toLowerCase()) : null;
  let video = 'pending', videoDir = '';
  for (const { d, files } of videoFiles) {
    if (files.has(s.shot + '.mp4') || files.has(id + '.mp4')) { video = 'done'; videoDir = d; break; }
    if (d === 'videos' && (files.has(s.shot + '.mp4') || files.has(id + '.mp4'))) video = 'raw';
  }
  return { first, last, lastNeed, video, videoDir };
}

// ---------- HTML 工具 ----------
const esc = s => (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const fmtTime = sec => `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, '0')}`;

// ---------- 组装数据 ----------
const shots = parseCsv(fs.readFileSync(CSV, 'utf8'));
for (const s of shots) s.st = statusOf(s);

const totalDur = shots.reduce((a, s) => a + (parseFloat(s.duration) || 0), 0);
const nFirst = shots.filter(s => s.st.first).length;
const nLastNeed = shots.filter(s => s.st.lastNeed).length;
const nLast = shots.filter(s => s.st.last).length;
const nVideo = shots.filter(s => s.st.video === 'done').length;
const acts = [...new Set(shots.map(s => s.act))];

// 幕起止时间
let clock = 0;
const actRange = {};
for (const s of shots) {
  if (!actRange[s.act]) actRange[s.act] = { start: clock };
  clock += parseFloat(s.duration) || 0;
  actRange[s.act].end = clock;
}

// ---------- 页面片段 ----------
const badge = (ok, yes, no) => ok
  ? `<span class="st ok">${yes}</span>`
  : `<span class="st no">${no}</span>`;

const videoBadge = s => {
  if (s.st.video === 'done') return `<span class="st ok">✓ ${s.st.videoDir === 'videos_fixed' ? '已修复' : '完成'}</span>`;
  return `<span class="st no">待生成</span>`;
};

const overviewRows = shots.map(s => `
    <tr>
      <td class="mono">${s.shot}</td><td>${esc(s.act)}</td><td class="mono">${s.duration}s</td>
      <td>${esc(s.size)}</td><td>${esc((s.vis || '').replace(/^画面[：:]\s*/, ''))}</td>
      <td>${s.mode === 'first' ? '<span class="tag mode-f">仅首帧</span>' : '<span class="tag mode-ff">双参考图</span>'}</td>
      <td>${badge(s.st.first, '✓ 已生成', '缺')}</td>
      <td>${s.st.lastNeed ? badge(s.st.last, '✓ 已生成', '缺') : '<span class="st na">—</span>'}</td>
      <td>${videoBadge(s)}</td>
    </tr>`).join('\n');

let cardsHtml = '';
acts.forEach((act, ai) => {
  const group = shots.filter(s => s.act === act);
  const r = actRange[act];
  const num = ['壹', '贰', '叁', '肆', '伍'][ai] || '';
  const name = act.split('·').pop().trim();
  cardsHtml += `
  <div class="act-title"><span class="num">${num}</span><span class="name">${esc(name)}</span><span class="time">${fmtTime(r.start)} – ${fmtTime(r.end)} · ${group.length}镜</span></div>
${group.map(s => `
  <div class="shot" id="${s.shot}">
    <div class="shot-head">
      <span class="shot-no">${s.shot}</span>
      <span class="tag dur">${s.duration}s</span>
      <span class="tag size">${esc(s.size)}</span>
      ${s.mode === 'first' ? '<span class="tag mode-f">仅首帧</span>' : '<span class="tag mode-ff">双参考图</span>'}
      ${s.sfx ? `<span class="sfx">音效：${esc(s.sfx)}</span>` : ''}
      <span class="prod">
        ${badge(s.st.first, '首帧✓', '首帧缺')}
        ${s.st.lastNeed ? badge(s.st.last, '尾帧✓', '尾帧缺') : ''}
        ${videoBadge(s)}
      </span>
    </div>
    ${s.narr ? `<div class="narr">${esc(s.narr)}</div>` : ''}
    ${s.vis ? `<div class="vis"><b>画面：</b>${esc(s.vis.replace(/^画面[：:]\s*/, ''))}</div>` : ''}
    <div class="grid2">
      ${s.first_prompt ? `<div class="copyable"><div class="lab">首帧 FIRST FRAME${s.st.first ? ' · <span class="labok">已生成 ' + esc(s.first_image || s.shot.toLowerCase() + '_first.png') + '</span>' : ''}</div>
        <button class="copy-btn" onclick="cp(this)">复制</button>
        <pre class="block">${esc(s.first_prompt)}</pre></div>` : ''}
      ${s.last_prompt ? `<div class="copyable"><div class="lab">尾帧 LAST FRAME${s.st.last ? ' · <span class="labok">已生成 ' + esc(s.last_image || s.shot.toLowerCase() + '_last.png') + '</span>' : ''}</div>
        <button class="copy-btn" onclick="cp(this)">复制</button>
        <pre class="block">${esc(s.last_prompt)}</pre></div>` : ''}
      ${s.h3_prompt ? `<div class="copyable"><div class="lab">H3 运动提示词${s.st.video === 'done' ? ' · <span class="labok">视频已产出</span>' : ''}</div>
        <button class="copy-btn" onclick="cp(this)">复制</button>
        <pre class="block">${esc(s.h3_prompt)}</pre></div>` : ''}
    </div>
  </div>`).join('\n')}
`;
});

const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>《${esc(CFG.title)}》生产看板 · ${new Date().toLocaleDateString('zh-CN')}</title>
<style>
  :root{
    --bg:#0b0f14; --card:#121924; --card2:#0e1520;
    --border:#22303f; --border2:#2e3f52;
    --text:#c9d4e0; --muted:#7a8a99; --dim:#5a6a78;
    --amber:#e8a04c; --amber-dim:#a06a2c;
    --teal:#6fbfb4; --red:#c96a5a; --green:#7fbf7a;
    --serif:"Noto Serif SC","Source Han Serif SC","Songti SC","SimSun",serif;
    --sans:"Noto Sans SC","Microsoft YaHei",system-ui,sans-serif;
    --mono:"Cascadia Code","Consolas","JetBrains Mono",monospace;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.7;padding:0 0 80px}
  .wrap{max-width:1080px;margin:0 auto;padding:0 28px}

  header{padding:56px 0 36px;border-bottom:1px solid var(--border);
    background:linear-gradient(180deg,#0d131c 0%,#0b0f14 100%)}
  .kicker{font-size:12px;letter-spacing:.35em;color:var(--amber);margin-bottom:14px}
  h1{font-family:var(--serif);font-size:42px;font-weight:700;color:#e8eef5;letter-spacing:.08em}
  h1 .lantern{color:var(--amber)}
  .sub{color:var(--muted);font-size:14px;margin-top:10px}
  .meta-chips{display:flex;flex-wrap:wrap;gap:10px;margin-top:22px}
  .chip{border:1px solid var(--border2);border-radius:999px;padding:4px 14px;font-size:12.5px;color:var(--text);background:var(--card2)}
  .chip b{color:var(--amber);font-weight:600}
  .chip.done b{color:var(--green)}

  section{margin-top:52px}
  h2{font-family:var(--serif);font-size:22px;color:#e8eef5;padding-left:14px;border-left:3px solid var(--amber);margin-bottom:20px;letter-spacing:.05em}
  p.lead{color:var(--muted);font-size:14px;margin-bottom:18px}

  .bar{display:flex;gap:8px;margin:6px 0 18px}
  .bar .seg{height:10px;border-radius:5px;background:#1a2532;overflow:hidden}
  .bar .seg i{display:block;height:100%;background:linear-gradient(90deg,var(--amber),#f0c070)}

  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th{background:#16202e;color:#9fb2c4;text-align:left;font-weight:600;padding:9px 10px;border:1px solid var(--border);font-size:12px;letter-spacing:.06em}
  td{padding:8px 10px;border:1px solid var(--border);color:var(--text);vertical-align:top}
  tr:nth-child(even) td{background:#0e1520}
  .tbl-scroll{overflow-x:auto;border-radius:10px;border:1px solid var(--border)}
  .tbl-scroll table{border:none;min-width:980px}
  .mono{font-family:var(--mono)}

  .st{display:inline-block;font-size:11.5px;border-radius:5px;padding:1px 8px;letter-spacing:.04em;white-space:nowrap}
  .st.ok{background:#12211a;color:var(--green);border:1px solid #2b4a35}
  .st.no{background:#241512;color:var(--red);border:1px solid #4a2a22}
  .st.na{background:#141b24;color:var(--dim);border:1px solid var(--border)}

  pre.block{background:#0a0e13;border:1px solid var(--border);border-radius:10px;padding:14px 16px;
    font-family:var(--mono);font-size:12.5px;line-height:1.75;color:#b8c7d6;white-space:pre-wrap;word-break:break-word;position:relative}
  .copyable{position:relative;margin:8px 0}
  .copyable .lab{font-size:11px;letter-spacing:.18em;color:var(--dim);margin-bottom:6px;font-weight:600}
  .copyable .lab .labok{color:var(--green);letter-spacing:.05em}
  .copy-btn{position:absolute;top:8px;right:8px;background:#1a2532;border:1px solid var(--border2);color:var(--muted);
    font-size:11px;border-radius:6px;padding:3px 10px;cursor:pointer;font-family:var(--sans);transition:all .15s;z-index:2}
  .copy-btn:hover{color:var(--amber);border-color:var(--amber-dim)}
  .copy-btn.ok{color:var(--teal);border-color:var(--teal)}

  .act-title{display:flex;align-items:baseline;gap:14px;margin:44px 0 16px;padding-bottom:10px;border-bottom:1px dashed var(--border2)}
  .act-title .num{font-family:var(--serif);font-size:26px;color:var(--amber)}
  .act-title .name{font-family:var(--serif);font-size:19px;color:#e8eef5;letter-spacing:.1em}
  .act-title .time{font-size:12px;color:var(--dim);margin-left:auto;letter-spacing:.05em}

  .shot{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px 22px;margin-bottom:18px}
  .shot-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}
  .shot-no{font-family:var(--serif);font-size:19px;color:#14100a;background:var(--amber);border-radius:8px;padding:1px 12px;font-weight:700}
  .tag{font-size:11.5px;border-radius:5px;padding:2px 9px;letter-spacing:.05em}
  .tag.dur{background:#1a2532;color:#9fb2c4;border:1px solid var(--border2)}
  .tag.size{background:#171f2b;color:#8ea3b8;border:1px solid var(--border2)}
  .tag.mode-ff{background:#241a10;color:var(--amber);border:1px solid #4a3517}
  .tag.mode-f{background:#101d1b;color:var(--teal);border:1px solid #23453f}
  .shot-head .sfx{font-size:12px;color:var(--dim)}
  .shot-head .prod{margin-left:auto;display:flex;gap:6px}
  .narr{font-family:var(--serif);font-size:15.5px;color:#dce6ef;line-height:2.1;background:var(--card2);
    border-left:3px solid var(--amber);border-radius:0 10px 10px 0;padding:12px 18px;margin-bottom:14px}
  .vis{font-size:13.5px;color:#a8b6c6;margin-bottom:14px;line-height:1.8}
  .vis b{color:#cfdbe7;font-weight:600}

  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 18px}

  footer{margin-top:60px;padding-top:24px;border-top:1px solid var(--border);color:var(--dim);font-size:12px;text-align:center;line-height:2}
  @media(max-width:860px){
    .grid2{grid-template-columns:1fr}
    h1{font-size:32px}
  }
</style>
</head>
<body>

<header>
  <div class="wrap">
    <div class="kicker">志怪漫剧 · 生产看板</div>
<h1>《${esc(CFG.title)}<span class="lantern">》</span></h1>
<div class="sub">${CFG.source ? esc(CFG.source) + ' · ' : ''}${CFG.subtitle ? esc(CFG.subtitle) + ' · ' : ''}${CFG.form ? '制作形态：' + esc(CFG.form) + ' · ' : ''}本页由 ${CSVNAME} 自动生成，改 CSV 后重跑 gen_html.js 即刷新</div>
    <div class="meta-chips">
      <span class="chip">时长 <b>${fmtTime(totalDur)}</b>（计划）</span>
      <span class="chip">镜头 <b>${shots.length} 镜 / ${acts.length} 幕</b></span>
      <span class="chip ${nFirst === shots.length ? 'done' : ''}">首帧图 <b>${nFirst}/${shots.length}</b></span>
      <span class="chip ${nLast === nLastNeed ? 'done' : ''}">尾帧图 <b>${nLast}/${nLastNeed}</b></span>
      <span class="chip ${nVideo === shots.length ? 'done' : ''}">视频 <b>${nVideo}/${shots.length}</b></span>
      <span class="chip">引擎 <b>MiniMax-H3 本地</b></span>
    </div>
  </div>
</header>

<div class="wrap">

<section>
  <h2>一、镜头总览与生产状态</h2>
  <p class="lead">状态实时检测自 shot/ 与 videos_fixed/ 目录，不是手工填写。红色即待办。</p>
  <div class="tbl-scroll">
  <table>
    <tr><th>镜号</th><th>幕</th><th>时长</th><th>景别</th><th>内容一句话</th><th>H3模式</th><th>首帧图</th><th>尾帧图</th><th>视频</th></tr>
${overviewRows}
  </table>
  </div>
</section>

<section>
  <h2>二、逐镜分镜与提示词</h2>
  <p class="lead">所有提示词点「复制」直接用，已内嵌风格锚与角色锚。改动请编辑 ${CSVNAME} 后重跑 gen_html.js，不要直接改本页。</p>
${cardsHtml}
</section>

<footer>
  事实源：${CSVNAME} · 生成于 ${new Date().toLocaleString('zh-CN')}<br>
  流程：改 CSV → node gen_html.js（看板）／ python gen_images.py（生图）／ python run_batch.py（生视频）
</footer>

</div>

<script>
function cp(btn){
  const pre = btn.parentElement.querySelector('pre.block');
  navigator.clipboard.writeText(pre.textContent).then(()=>{
    btn.textContent='已复制'; btn.classList.add('ok');
    setTimeout(()=>{btn.textContent='复制'; btn.classList.remove('ok');},1500);
  });
}
</script>
</body>
</html>
`;

fs.writeFileSync(OUT, html, 'utf8');
console.log(`✓ 看板已生成：${OUT}`);
console.log(`  首帧 ${nFirst}/${shots.length} · 尾帧 ${nLast}/${nLastNeed} · 视频 ${nVideo}/${shots.length} · 计划时长 ${fmtTime(totalDur)}`);
