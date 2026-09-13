"""Read-only review page generation; all review mutations use assets.py."""
import argparse
import html
import json
from pathlib import Path
from urllib.parse import quote
from assets import Assets
from image_pipeline import read_rows, jobs_from_rows
from project import load_project


def esc(value):
    return html.escape(str(value))


def render(P):
    db = Assets(P.dir)
    planned = {}
    for path, is_asset in [(P.dir / 'asset_jobs.csv', True), (P.csv_path, False)]:
        if path.exists():
            for j in jobs_from_rows(read_rows(path), is_asset):
                planned[j['key']] = j
    sections = []
    labels = {'pending': '待审核', 'approved': '通过', 'rework': '返工', 'failed': '失败', 'running': '执行中'}
    for key in dict.fromkeys(list(planned) + list(db.data['items'])):
        job = planned.get(key, {})
        item = db.data['items'].get(key, {'versions': {}, 'selected': None})
        cards = []
        for v, c in item['versions'].items():
            stale = db.stale(key, v)
            selected = item.get('selected') == v
            state = '需复核' if stale else labels.get(c['status'], c['status'])
            deps = '<br>'.join(esc(f"{d['asset']}@{d['version']} — {d['purpose']}") for d in c.get('dependencies', [])) or '无图片参考'
            path = P.dir / c['file']
            picture = f'<a href="{quote(c["file"])}" target="_blank"><img loading="lazy" src="{quote(c["file"])}" alt="{esc(key)} {v}"></a>' if path.exists() else '<div class="empty">尚无成功输出</div>'
            workflow = f'<a href="{quote(c["workflow_file"])}">实际工作流 JSON</a>' if c.get('workflow_file') else ''
            cards.append(f'''<article class="card {'selected' if selected else ''}">
            <div class="badge">{esc(key)}@{v} · {state}{' · 当前选定' if selected else ''}</div>
            {picture}<p>{esc(c.get('route', ''))} · seed {esc(c.get('seed', ''))}</p>
            <p class="deps">{deps}</p><p class="error">{esc('；'.join(stale))}</p>
            <p>{esc('；'.join(c.get('notes', [])))}</p><p class="error">{esc(c.get('error',''))}</p>
            <details><summary>提示词与追溯</summary><pre>{esc(c.get('prompt',''))}</pre>{workflow}
            <pre>{esc(json.dumps(c.get('inputs', []), ensure_ascii=False, indent=2))}</pre></details></article>''')
        desc = f"路径：{job.get('image_route', 't2i')}；参考：{job.get('references', '[]')}；前置审核：{job.get('requires','无')}"
        if item.get('kind', job.get('kind')) == 'style':
            desc += '。本组只审核画风，图中人物与房间布局不自动成为正式资产。'
        sections.append(f'<section><h2>{esc(key)} · {esc(item.get("name") or job.get("name", ""))}</h2><p>{esc(desc)}</p><div class="grid">' + ''.join(cards or ['<p class="empty">尚未生成；参考未审核的任务将被拦截。</p>']) + '</div></section>')
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>''' + esc(P.title) + ''' · 图片审核</title><style>
    body{font:16px/1.65 system-ui,sans-serif;background:#f4f1eb;color:#242822;margin:0;padding:32px;max-width:1600px;margin:auto}
    h1{font-size:30px}h2{font-size:20px}section{margin:32px 0;padding-top:12px;border-top:1px solid #ccc}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px}.card{background:white;border:2px solid transparent;border-radius:12px;padding:14px;min-width:0}
    .selected{border-color:#317d62}img{width:100%;height:300px;object-fit:contain;background:#e8e6df}.badge{font-weight:650;padding:6px 0}
    .deps,.empty{color:#637065}.error{color:#a7382e}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}a{color:#226f60}
    header{background:#e3eadd;padding:20px;border-radius:12px}.hint{font-size:14px}</style>
    <header><h1>''' + esc(P.title) + ''' · 图片审核</h1><p>按“编号@版本”在对话中告诉我：选定、返工，以及要修改的具体位置。</p>
    <p class="hint">本页不保存选择。只有明确审核后，参考才能进入下游；刷新看板后显示新状态。候选之间可点击原图对照。图片质量由你审核。</p></header>''' + ''.join(sections) + '</html>'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    P = load_project(a.project)
    content = render(P)
    out = P.dir / '分镜.html'
    if a.dry_run:
        print('只读：看板可生成', len(content), '字符')
    else:
        out.write_text(content, encoding='utf-8')
        print(out)


if __name__ == '__main__':
    main()
