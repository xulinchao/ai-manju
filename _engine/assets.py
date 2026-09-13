"""Versioned image candidates. Reading never creates files; acceptance is explicit."""
import argparse
import csv
import hashlib
import json
import re
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def local_path(root, relative):
    if not relative or Path(relative).is_absolute():
        raise ValueError(f'需要项目内相对路径：{relative}')
    path = (Path(root) / relative).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError(f'路径超出项目：{relative}')
    return path


class Assets:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / 'assets.json'
        self.data = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {'schema': 1, 'items': {}}

    def save(self):
        atomic_json(self.path, self.data)

    def item(self, key):
        if key not in self.data['items']:
            raise ValueError(f'资产未登记：{key}')
        return self.data['items'][key]

    def candidate(self, key, version):
        item = self.item(key)
        if version not in item['versions']:
            raise ValueError(f'资产版本不存在：{key}@{version}')
        return item['versions'][version]

    def stale(self, key, version, stack=None):
        stack = set(stack or ())
        token = (key, version)
        if token in stack:
            return ['循环引用']
        stack.add(token)
        c = self.candidate(key, version)
        reasons = []
        recipe = c.get('recipe')
        if recipe:
            path = local_path(self.root, recipe['file'])
            if not path.exists():
                reasons.append('生成任务表缺失')
            else:
                with path.open(encoding='utf-8-sig', newline='') as f:
                    row = next((r for r in csv.DictReader(f) if r.get(recipe['id_column']) == recipe['id']), None)
                if row != recipe['row']:
                    reasons.append('镜头或资产生成定义已修改')
        if 'art_direction' in c:
            cfg = json.loads((self.root / 'project.json').read_text(encoding='utf-8'))
            if cfg.get('art_direction', {}) != c['art_direction']:
                reasons.append('项目美术设定已修改')
        if c.get('sha256'):
            own = local_path(self.root, c['file'])
            if not own.exists() or digest(own) != c['sha256']:
                reasons.append('候选文件缺失或内容已变化')
        for dep in c.get('dependencies', []):
            try:
                d = self.candidate(dep['asset'], dep['version'])
                if self.item(dep['asset']).get('selected') != dep['version']:
                    reasons.append(f"参考已换版：{dep['asset']}")
                if d['status'] != 'approved' or self.stale(dep['asset'], dep['version'], stack):
                    reasons.append(f"参考需复核：{dep['asset']}")
                p = local_path(self.root, d['file'])
                if not p.exists() or digest(p) != dep['sha256']:
                    reasons.append(f"参考文件变化：{dep['asset']}")
            except (ValueError, KeyError):
                reasons.append('参考记录失效')
        for source in c.get('inputs', []):
            p = local_path(self.root, source['file'])
            if not p.exists() or digest(p) != source['sha256']:
                reasons.append(f"输入文件变化：{source['file']}")
        return reasons

    def resolve(self, token, purpose='参考'):
        key, sep, version = token.partition('@')
        item = self.item(key)
        version = version if sep else item.get('selected')
        if not version:
            raise ValueError(f'{purpose}尚未选定：{key}')
        c = self.candidate(key, version)
        if item.get('selected') != version or c['status'] != 'approved':
            raise ValueError(f'{purpose}不是当前已审核版本：{key}@{version}')
        reasons = self.stale(key, version)
        if reasons:
            raise ValueError(f'{purpose}需复核：{key}@{version}；' + '；'.join(reasons))
        p = local_path(self.root, c['file'])
        if not p.is_file() or digest(p) != c.get('sha256'):
            raise ValueError(f'{purpose}文件缺失或已变化：{key}@{version}')
        return p, {'asset': key, 'version': version, 'purpose': purpose, 'sha256': c['sha256']}

    def reserve(self, key, metadata, limit=3):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', key):
            raise ValueError('资产编号仅允许英文、数字、点、短横线和下划线')
        item = self.data['items'].setdefault(key, {'name': metadata.get('name', key), 'kind': metadata.get('kind', 'shot'), 'selected': None, 'versions': {}})
        if len(item['versions']) >= limit:
            raise ValueError(f'{key} 已达到 {limit} 次候选上限，请先复盘')
        version = f"v{len(item['versions']) + 1:03d}"
        rel = f'candidates/{key}/{version}.png'
        if local_path(self.root, rel).exists():
            raise ValueError('候选路径已占用，拒绝覆盖')
        c = dict(metadata, file=rel, status='running', notes=[], dependencies=metadata.get('dependencies', []))
        item['versions'][version] = c
        self.save()
        return version, local_path(self.root, rel)

    def review(self, key, version, status, note):
        c = self.candidate(key, version)
        if status == 'approved':
            p = local_path(self.root, c['file'])
            if c['status'] in ('running', 'failed') or not p.exists() or digest(p) != c.get('sha256'):
                raise ValueError('文件未完成或被修改，不能通过审核')
            if self.stale(key, version):
                raise ValueError('上游已变化，请重新生成；不能直接解除需复核状态')
            self.item(key)['selected'] = version
        elif self.item(key).get('selected') == version:
            self.item(key)['selected'] = None
        c['status'] = status
        c.setdefault('notes', []).append(note)
        self.save()


def main():
    from project import load_project
    ap = argparse.ArgumentParser(description='仅根据用户的明确审核意见选择图片版本')
    ap.add_argument('--project', required=True)
    ap.add_argument('--asset')
    ap.add_argument('--version')
    ap.add_argument('--status', choices=['approved', 'rework', 'pending'])
    ap.add_argument('--note', default='')
    ap.add_argument('--list', action='store_true')
    a = ap.parse_args()
    db = Assets(load_project(a.project).dir)
    if a.list:
        for key, item in db.data['items'].items():
            print(key, item.get('selected'), [(v, c['status'], db.stale(key, v)) for v, c in item['versions'].items()])
        return
    if not all((a.asset, a.version, a.status, a.note)):
        ap.error('审核必须给 --asset --version --status --note，意见来自用户')
    db.review(a.asset, a.version, a.status, a.note)
    print('审核记录已保存；重新生成看板即可查看')


if __name__ == '__main__':
    main()
