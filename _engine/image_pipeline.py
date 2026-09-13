"""Local ComfyUI image production; immutable candidates and explicit references."""
import argparse
import copy
import csv
import json
import random
import struct
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from assets import Assets, atomic_json, digest, local_path
from project import load_project, load_node_map, load_size_node, shot_files
from styles import apply_params, load_style, Style

ENGINE = Path(__file__).resolve().parent
ROUTES = ('t2i', 'edit', 'masked', 'crop', 'faceswap')


def read_rows(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if any(None in r or any(v is None for v in r.values()) for r in rows):
        raise ValueError('CSV 行列数不一致')
    return rows


def json_field(row, field, default):
    raw = row.get(field, '')
    return json.loads(raw) if raw and raw.strip() else default


def jobs_from_rows(rows, assets=False):
    jobs = []
    seen = set()
    for row in rows:
        key = row.get('asset' if assets else 'shot', '').strip()
        if not key or key in seen:
            raise ValueError(f'编号为空或重复：{key}')
        seen.add(key)
        if assets:
            jobs.append(dict(row, key=key, prompt=row.get('prompt', ''), kind=row.get('kind', 'asset')))
            continue
        first, last = shot_files(row)
        jobs.append(dict(row, key=key + '.first', prompt=row.get('first_prompt', ''), kind='shot', output_name=first))
        if row.get('last_prompt', '').strip():
            jobs.append(dict(row, key=key + '.last', prompt=row['last_prompt'], kind='shot',
                             image_route='edit', references='[]', edit_source=key + '.first', mask='',
                             output_name=last or key.lower() + '_last.png'))
    return jobs


def prepare(P, job, db):
    route = job.get('image_route', '').strip() or 't2i'
    if route not in ROUTES:
        raise ValueError(f'未知图片路径：{route}')
    if not job.get('prompt', '').strip() and route != 'crop':
        raise ValueError('提示词为空')
    style = load_style(job['style_id']) if job.get('style_id') else P.style
    if job.get('style_override', '').strip():
        style = style.override({'positive': job['style_override'].strip()})
    refs = json_field(job, 'references', [])
    if not isinstance(refs, list) or any(not isinstance(r, dict) or not r.get('asset') or not r.get('purpose') for r in refs):
        raise ValueError('references 必须为 [{"asset":"编号@版本","purpose":"用途"}]')
    dependencies, images = [], []
    source = job.get('edit_source', '').strip()
    if source:
        p, d = db.resolve(source, '编辑底图')
        images.append(p)
        dependencies.append(d)
    for ref in refs:
        p, d = db.resolve(ref['asset'], ref['purpose'])
        if d['asset'] == job['key']:
            raise ValueError('不得以自身作为参考，请建立独立状态变体')
        images.append(p)
        dependencies.append(d)
    if route == 't2i' and images:
        raise ValueError('文生图不能忽略已声明参考，请选择 edit')
    if route in ('edit', 'masked') and not 1 <= len(images) <= 3:
        raise ValueError('Qwen 编辑需要 1—3 张已审核参考；不会退回文生图')
    if route in ('masked', 'crop') and not source:
        raise ValueError('局部修改/裁切必须指定 edit_source')
    if route == 'crop' and len(images) != 1:
        raise ValueError('裁切只允许一张底图')
    if route == 'faceswap' and (not source or len(images) != 2):
        raise ValueError('换脸需要 edit_source 底图和一张脸部参考')
    required = list(filter(None, (x.strip() for x in job.get('requires', '').split(';'))))
    style_asset = P.get('review_style_asset')
    if style_asset and job.get('kind') != 'style' and style_asset not in required:
        required.insert(0, style_asset)
    for token in required:
        _, dep = db.resolve(token, '审核前置')
        dependencies.append(dep)
        if db.item(dep['asset']).get('kind') == 'style' and not job.get('style_id'):
            style = Style(db.candidate(dep['asset'], dep['version'])['style'], '已审核画风')
            if job.get('style_override', '').strip():
                style = style.override({'positive': job['style_override'].strip()})
    if any(d['asset'] == job['key'] for d in dependencies):
        raise ValueError('自引用会导致循环依赖，请使用独立资产编号')
    mask = None
    if route == 'masked':
        mask = local_path(P.dir, job.get('mask', ''))
        if not mask.is_file():
            raise ValueError(f'遮罩缺失：{mask}')
        from PIL import Image
        with Image.open(images[0]) as base, Image.open(mask) as m:
            if base.mode != 'RGB' or base.format != 'PNG':
                raise ValueError('逐像素保护目前要求 RGB PNG 底图，避免透明度或位深被隐式转换')
            if base.size != m.size:
                raise ValueError('遮罩尺寸必须与原图完全一致；白色修改，黑色保护')
    crop = json_field(job, 'crop', {})
    if route == 'crop':
        from PIL import Image
        if set(crop) != {'x', 'y', 'width', 'height'} or any(type(v) is not int for v in crop.values()):
            raise ValueError('crop 需要整数 x/y/width/height')
        with Image.open(images[0]) as im:
            if min(crop['x'], crop['y']) < 0 or min(crop['width'], crop['height']) < 1 or crop['x'] + crop['width'] > im.width or crop['y'] + crop['height'] > im.height:
                raise ValueError('裁切范围超出原图')
    rules = '\n'.join(f'Image {i + 1} 负责{d["purpose"]}。' for i, d in enumerate(dependencies[:len(images)]))
    art = P.get('art_direction', {})
    art_text = '\n'.join(f'{k}：{v}' for k, v in art.items()) if isinstance(art, dict) else str(art)
    text = '\n'.join(filter(None, [rules, art_text, job.get('scene_state', ''), job.get('prompt', '')]))
    prompt = P.compose_prompt(text, mode='t2i' if route == 't2i' else 'edit', style=style,
                              preserve_layout=route == 'masked')
    if route == 'faceswap':
        prompt = 'face swap face from Image 1 to Image 2. swap only the face and not the hair, the same skin tone from Image 2, same pose as Image 2. ' + job['prompt']
    wf_name = {'t2i': P.workflow_file('t2i').name, 'edit': 'edit_qwen_multi.json',
               'masked': 'edit_qwen_masked.json', 'crop': 'crop_image.json',
               'faceswap': 'edit_qwen_image_edit_2509_faceswap.json'}[route]
    wf_path = ENGINE / 'workflows' / wf_name
    if not wf_path.exists():
        raise ValueError(f'工作流不存在：{wf_path}')
    return {'route': route, 'images': images, 'dependencies': dependencies, 'mask': mask,
            'crop': crop, 'prompt': prompt, 'style': style, 'workflow': wf_path,
            'seed': int(job['seed']) if job.get('seed') else random.SystemRandom().randrange(2**48)}


class Comfy:
    def __init__(self, url):
        self.url = url.rstrip('/')

    def request(self, path, data=None):
        req = urllib.request.Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def upload(self, path):
        boundary = uuid.uuid4().hex
        name = digest(path)[:16] + Path(path).suffix
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode() + Path(path).read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request(self.url + '/upload/image', data=body, headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
        with urllib.request.urlopen(req, timeout=120) as r:
            v = json.load(r)
        return '/'.join(filter(None, [v.get('subfolder'), v['name']]))

    def download(self, item, dest):
        query = urllib.parse.urlencode({k: item.get(k, 'output' if k == 'type' else '') for k in ('filename', 'subfolder', 'type')})
        with urllib.request.urlopen(self.url + '/view?' + query, timeout=300) as r:
            content = r.read()
        if content[:8] != b'\x89PNG\r\n\x1a\n':
            raise ValueError('指定输出不是 PNG 图片')
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents an accidental overwrite of a candidate.
        with open(dest, 'xb') as f:
            f.write(content)

    def run(self, wf, output, submitted, timeout=900):
        # Never interrupt or reorder other user's ComfyUI work.
        queue = self.request('/queue')
        if queue.get('queue_running') or queue.get('queue_pending'):
            raise ValueError('ComfyUI 有其他排队任务，请等其结束后重试')
        response = self.request('/prompt', {'prompt': wf, 'client_id': uuid.uuid4().hex})
        pid = response['prompt_id']
        submitted(pid)
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            hist = self.request('/history/' + pid).get(pid)
            if hist:
                if hist.get('status', {}).get('status_str') == 'error':
                    raise RuntimeError('ComfyUI 执行错误：' + json.dumps(hist['status'], ensure_ascii=False)[:1500])
                if hist.get('status', {}).get('completed'):
                    images = hist.get('outputs', {}).get(output, {}).get('images', [])
                    if not images:
                        raise RuntimeError(f'指定保存节点 {output} 没有输出')
                    return images[0]
            time.sleep(2)
        raise TimeoutError(f'等待超时，未取消服务端任务；prompt_id={pid}，可用 --recover 下载')


def set_text(node, text):
    for key in ('text', 'prompt', 'value', 'string'):
        if key in node['inputs']:
            node['inputs'][key] = text
            return
    raise ValueError('提示词节点没有文本输入')


def build_workflow(P, prepared, names, prefix):
    wf = json.loads(prepared['workflow'].read_text(encoding='utf-8'))
    route = prepared['route']
    if route == 'crop':
        wf['source']['inputs']['image'] = names[0]
        wf['crop']['inputs'].update(prepared['crop'])
        output = 'save'
    else:
        nm = load_node_map(prepared['workflow'].name)
        apply_params(wf, prepared['style'].params_for(prepared['workflow'].name))
        set_text(wf[nm['positive']], prepared['prompt'])
        if nm.get('negative'):
            set_text(wf[nm['negative']], prepared['style'].negative)
        for node in wf.values():
            for key, value in list(node['inputs'].items()):
                if key in ('seed', 'noise_seed') and isinstance(value, int):
                    node['inputs'][key] = prepared['seed']
        if route == 't2i':
            size = load_size_node(prepared['workflow'].name)
            wf[size]['inputs'].update(width=P.width, height=P.height)
        elif route == 'faceswap':
            wf[nm['input_image']]['inputs']['image'] = names[0]
            wf[nm['input_face']]['inputs']['image'] = names[1]
        else:
            wf['78']['inputs']['image'] = names[0]
            for i in (2, 3):
                if len(names) >= i:
                    wf[f'ref{i}']['inputs']['image'] = names[i - 1]
                else:
                    wf.pop(f'ref{i}', None)
                    for encoder in ('433:110', '433:111'):
                        wf[encoder]['inputs'].pop(f'image{i}', None)
            if route == 'masked':
                wf['mask']['inputs']['image'] = prepared['mask_name']
        output = nm['save']
    wf[output]['inputs']['filename_prefix'] = prefix
    return wf, output


def finish_candidate(db, key, version, dest):
    from PIL import Image
    c = db.candidate(key, version)
    with Image.open(dest) as im:
        im.verify()
    if c.get('route') == 'masked':
        from PIL import ImageChops
        source = local_path(db.root, c['inputs'][0]['file'])
        mask = local_path(db.root, c['inputs'][-1]['file'])
        with Image.open(source) as a, Image.open(dest) as b, Image.open(mask) as m:
            if a.size != b.size:
                raise ValueError('局部合成改变了原图尺寸')
            protected = m.convert('RGB').getchannel('R').point(lambda x: 255 if x == 0 else 0)
            delta = ImageChops.difference(a.convert('RGB'), b.convert('RGB'))
            for channel in delta.split():
                if ImageChops.multiply(channel, protected).getbbox():
                    raise ValueError('局部合成保护区域像素发生变化，拒绝交付')
        c['protected_pixels_verified'] = True
    c.update(status='pending', sha256=digest(dest))
    db.save()


def generate(P, job, prepared, db):
    inputs = [{'file': p.relative_to(P.dir).as_posix(), 'sha256': digest(p)} for p in prepared['images']]
    if prepared['mask']:
        inputs.append({'file': prepared['mask'].relative_to(P.dir).as_posix(), 'sha256': digest(prepared['mask'])})
    metadata = {'name': job.get('name') or job['key'], 'kind': job.get('kind', 'shot'),
                'route': prepared['route'], 'prompt': prepared['prompt'], 'seed': prepared['seed'],
                'style': prepared['style']._d, 'art_direction': P.get('art_direction', {}),
                'job': job, 'dependencies': prepared['dependencies'], 'inputs': inputs,
                'recipe': job.get('recipe'),
                'workflow_source_sha256': digest(prepared['workflow'])}
    version, dest = db.reserve(job['key'], metadata, int(P.get('candidate_limit', 3)))
    c = db.candidate(job['key'], version)
    client = Comfy(P.comfy_url)
    try:
        names = [client.upload(p) for p in prepared['images']]
        if prepared['mask']:
            prepared['mask_name'] = client.upload(prepared['mask'])
        wf, output = build_workflow(P, prepared, names, f'{P.slug}/{job["key"]}/{version}')
        c['workflow_file'] = dest.with_suffix('.workflow.json').relative_to(P.dir).as_posix()
        c['output_node'] = output
        atomic_json(dest.with_suffix('.workflow.json'), wf)
        db.save()
        def submitted(pid):
            c['prompt_id'] = pid
            db.save()
        result = client.run(wf, output, submitted)
        client.download(result, dest)
        finish_candidate(db, job['key'], version, dest)
        print(f'[待审核] {job["key"]}@{version} -> {dest}', flush=True)
    except Exception as e:
        c.update(status='failed', error=str(e))
        db.save()
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(description='本地图片候选生成；审核后才能用于下游')
    ap.add_argument('--project')
    ap.add_argument('--assets', action='store_true', help='读取 asset_jobs.csv；默认读取 shots.csv')
    ap.add_argument('--only')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--inspect', action='store_true')
    ap.add_argument('--force', action='store_true', help='新增候选，不覆盖')
    ap.add_argument('--chain', action='store_true', help='仅提示审核门，不自动生成视频')
    ap.add_argument('--style', help='此次候选画风；不修改项目配置')
    ap.add_argument('--recover', help='恢复已完成服务端任务，格式 asset@version')
    a = ap.parse_args(argv)
    P = load_project(a.project)
    db = Assets(P.dir)
    if a.recover:
        key, version = a.recover.split('@', 1)
        c = db.candidate(key, version)
        if a.dry_run:
            print('只读：将恢复', a.recover, c.get('prompt_id'))
            return 0
        if c['status'] not in ('failed', 'running'):
            raise ValueError('只允许恢复未完成候选')
        client = Comfy(P.comfy_url)
        hist = client.request('/history/' + c['prompt_id']).get(c['prompt_id'], {})
        if not hist.get('status', {}).get('completed'):
            raise ValueError('服务端任务尚未成功完成')
        dest = local_path(P.dir, c['file'])
        if not dest.exists():
            client.download(hist['outputs'][c['output_node']]['images'][0], dest)
        finish_candidate(db, key, version, dest)
        return 0
    if a.inspect:
        print('图片路径：', ', '.join(ROUTES))
        print('Qwen 多图/遮罩：', ENGINE / 'workflows' / 'edit_qwen_multi.json')
        print('所有生成结果进入 candidates；使用 assets.py 显式审核')
        return 0
    rows = read_rows(P.dir / 'asset_jobs.csv' if a.assets else P.csv_path)
    jobs = jobs_from_rows(rows, a.assets)
    for job in jobs:
        column = 'asset' if a.assets else 'shot'
        row_id = job['key'] if a.assets else job['shot']
        job['recipe'] = {'file': 'asset_jobs.csv' if a.assets else P.csv,
                         'id_column': column, 'id': row_id,
                         'row': next(r for r in rows if r[column] == row_id)}
    if a.only:
        wanted = {x.strip().lower() for x in a.only.split(',')}
        jobs = [j for j in jobs if j['key'].lower() in wanted or j.get('shot', '').lower() in wanted]
        if not jobs:
            raise ValueError('--only 没有匹配任务')
    failed = 0
    for job in jobs:
        if a.style:
            job['style_id'] = a.style
        versions = db.data['items'].get(job['key'], {}).get('versions', {})
        if versions and not a.force:
            print(f'[已有候选] {job["key"]}；需要新候选用 --force')
            continue
        try:
            if len(versions) >= int(P.get('candidate_limit', 3)):
                raise ValueError('候选达到上限，请复盘后调整方案')
            prepared = prepare(P, job, db)
            print(f'[预检] {job["key"]} / {prepared["route"]} / {len(prepared["images"])} 张参考', flush=True)
            if not a.dry_run:
                generate(P, job, prepared, db)
        except Exception as e:
            failed += 1
            print(f'[停止该镜] {job["key"]}: {e}', flush=True)
    if a.chain:
        print('审核门：未自动接力视频。请先审核分镜并显式运行视频任务。')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
