"""Stage-aware validation: unknown is never reported as passed."""
import argparse
import json
import math
import shutil
import subprocess
from pathlib import Path
from assets import Assets, digest, local_path
from image_pipeline import read_rows, jobs_from_rows, prepare
from project import load_project, shot_files


def check(P, stage='preflight', assets=False):
    errors, warnings = [], []
    rows = read_rows(P.dir / 'asset_jobs.csv' if assets else P.csv_path)
    if not rows:
        return ['任务表为空'], []
    jobs = jobs_from_rows(rows, assets)
    db = Assets(P.dir)
    if stage == 'preflight':
        for job in jobs:
            try:
                prepare(P, job, db)
            except Exception as e:
                errors.append(f'{job["key"]}: {e}')
    elif stage == 'approved':
        if P.get('image_pipeline_version') or db.path.exists():
            for job in jobs:
                try:
                    db.resolve(job['key'], '选定图片')
                except Exception as e:
                    errors.append(str(e))
        else:
            for row in rows:
                for name in filter(None, shot_files(row)):
                    if not (P.shot_dir / name).is_file():
                        errors.append(f'{row["shot"]}: 缺图 {name}')
            warnings.append('旧项目：仅检查文件存在，未验证人工审核和版本来源')
    elif stage == 'media':
        probe = shutil.which('ffprobe')
        if not probe:
            warnings.append('未验证：找不到 ffprobe，不能检查视频时长或解码信息')
        for row in rows:
            key = row.get('shot', row.get('asset', ''))
            path = next((d / f'{key}.mp4' for d in (P.video_fixed_dir, P.video_dir) if (d / f'{key}.mp4').is_file()), None)
            if not path:
                errors.append(f'{key}: 视频缺失')
                continue
            if probe:
                try:
                    r = subprocess.run([probe, '-v', 'error', '-show_entries', 'format=duration:stream=codec_type', '-of', 'json', str(path)], capture_output=True, text=True, timeout=30, check=True)
                    info = json.loads(r.stdout)
                    if not any(s.get('codec_type') == 'video' for s in info.get('streams', [])):
                        raise ValueError('无视频流')
                    seconds = float(info['format']['duration'])
                    if not math.isfinite(seconds) or seconds <= 0:
                        raise ValueError('时长无效')
                    if abs(seconds - float(row['duration'])) > .75:
                        warnings.append(f'{key}: 视频 {seconds:.3f}s 与计划 {row["duration"]}s 有偏差')
                except Exception as e:
                    errors.append(f'{key}: 未验证，ffprobe 失败：{e}')
    if not assets:
        for row in rows:
            key = row.get('shot', '?')
            try:
                sec = float(row.get('duration', ''))
                if not math.isfinite(sec) or sec <= 0:
                    raise ValueError()
            except (ValueError, TypeError):
                errors.append(f'{key}: duration 必须为正数')
            if row.get('mode') not in ('first', 'reference', 'firstlast'):
                errors.append(f'{key}: 未知视频模式')
            if row.get('mode') == 'firstlast':
                warnings.append(f'{key}: 旧 firstlast 实际是 H3 双参考图，不能视为结束帧绑定')
        try:
            total = sum(float(row['duration']) for row in rows)
            target = float(P.target_seconds)
            if math.isfinite(total) and abs(total - target) > .01:
                warnings.append(f'计划总时长 {total:g}s，项目目标 {target:g}s；图片审核不等于时间线验收')
        except (ValueError, KeyError, TypeError):
            pass  # Invalid durations were reported per row above.
    return errors, warnings


def main():
    ap = argparse.ArgumentParser(description='生成前、审核后、视频媒体分阶段检查')
    ap.add_argument('--project')
    ap.add_argument('--stage', choices=['preflight', 'approved', 'media'], default='preflight')
    ap.add_argument('--assets', action='store_true')
    ap.add_argument('--strict', action='store_true')
    a = ap.parse_args()
    try:
        errors, warnings = check(load_project(a.project), a.stage, a.assets)
    except Exception as e:
        errors, warnings = [str(e)], []
    for msg in errors:
        print('[不通过]', msg)
    for msg in warnings:
        print('[警告]', msg)
    print('结论：' + ('不通过' if errors or (warnings and a.strict) else '检查完成（含警告/未验证项）' if warnings else '本阶段通过'))
    return int(bool(errors or (a.strict and warnings)))


if __name__ == '__main__':
    raise SystemExit(main())
