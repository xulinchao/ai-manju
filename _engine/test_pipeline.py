"""Behavioral regression tests. Uses temporary synthetic assets, never story projects."""
import copy
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from assets import Assets, atomic_json, digest
from checks import check
from image_pipeline import prepare, build_workflow, jobs_from_rows, finish_candidate, main
from project import load_project
from review_board import render

ENGINE = Path(__file__).resolve().parent


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        atomic_json(self.root / 'project.json', {'title': 'Test', 'image_pipeline_version': 2,
                    'style_id': 'picturebook_clean', 'width': 64, 'height': 64,
                    'workflows': {'t2i': 't2i_z_image_turbo.json', 'edit': 'edit_qwen_image_edit_2509.json',
                                  'i2v': 'minimax_h3_i2v.json', 'r2v': 'minimax_h3_r2v.json'}})
        self.P = load_project(str(self.root))
        self.db = Assets(self.root)
        self.row = {'shot': 'S01', 'duration': '4', 'mode': 'first', 'first_prompt': 'person',
                    'h3_prompt': 'still', 'image_route': 't2i', 'first_image': 'custom.png'}
        self.write_rows([self.row])

    def tearDown(self):
        self.tmp.cleanup()

    def write_rows(self, rows):
        with open(self.root / 'shots.csv', 'w', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    def asset(self, key, approved=True, deps=None):
        v, p = self.db.reserve(key, {'dependencies': deps or []}, 3)
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (64, 64), (140, 150, 160)).save(p)
        finish_candidate(self.db, key, v, p)
        if approved:
            self.db.review(key, v, 'approved', 'synthetic test fixture')
        return v, p

    def test_pending_reference_is_blocked(self):
        self.asset('C', False)
        with self.assertRaisesRegex(ValueError, '尚未选定'):
            self.db.resolve('C')

    def test_reselection_invalidates_descendants(self):
        self.asset('C')
        _, dep = self.db.resolve('C')
        v, _ = self.asset('S', deps=[dep])
        self.asset('C')
        self.assertTrue(self.db.stale('S', v))
        with self.assertRaises(ValueError):
            self.db.resolve('S')

    def test_tamper_detected(self):
        v, p = self.asset('C')
        Image.new('RGB', (64, 64), 'red').save(p)
        with self.assertRaises(ValueError):
            self.db.resolve('C')

    def test_missing_reference_no_fallback(self):
        with self.assertRaises(ValueError):
            prepare(self.P, {'key': 'S', 'prompt': 'p', 'image_route': 'edit'}, self.db)

    def test_three_refs_and_unused_slots_removed(self):
        for key in ('A', 'B', 'C'):
            self.asset(key)
        job = {'key': 'S', 'prompt': 'p', 'image_route': 'edit',
               'references': json.dumps([{'asset': k, 'purpose': k} for k in ('A', 'B', 'C')])}
        prepared = prepare(self.P, job, self.db)
        wf, output = build_workflow(self.P, prepared, ['a.png', 'b.png', 'c.png'], 'test')
        self.assertEqual(wf['433:111']['inputs']['image3'], ['ref3', 0])
        self.assertEqual(wf['ref2']['inputs']['image'], 'b.png')
        wf, _ = build_workflow(self.P, prepared, ['a.png'], 'test')
        self.assertNotIn('ref2', wf)
        self.assertNotIn('image2', wf['433:111']['inputs'])

    def test_per_shot_style_and_custom_name(self):
        rows = [dict(self.row, style_override='STYLE A'), dict(self.row, shot='S02', style_override='STYLE B')]
        jobs = jobs_from_rows(rows)
        self.assertEqual(jobs[0]['output_name'], 'custom.png')
        a, b = [prepare(self.P, job, self.db)['prompt'] for job in jobs]
        self.assertIn('STYLE A', a)
        self.assertNotIn('STYLE B', a)
        self.assertIn('STYLE B', b)

    def test_dry_runs_write_nothing(self):
        before = {str(p.relative_to(self.root)): digest(p) for p in self.root.rglob('*') if p.is_file()}
        with patch('image_pipeline.Comfy.request', side_effect=AssertionError('dry run network')):
            self.assertEqual(main(['--project', str(self.root), '--dry-run']), 0)
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        # Missing approved image must fail, without backfill or directory creation.
        r = subprocess.run([sys.executable, str(ENGINE / 'run_batch.py'), '--project', str(self.root), '--dry-run'], env=env, capture_output=True)
        self.assertNotEqual(r.returncode, 0)
        r = subprocess.run([sys.executable, str(ENGINE / 'run_batch.py'), '--project', str(self.root), '--dry-run', '--fix-only'], env=env, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        after = {str(p.relative_to(self.root)): digest(p) for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_media_unknown_not_pass(self):
        (self.root / 'videos').mkdir()
        (self.root / 'videos/S01.mp4').write_bytes(b'not a video')
        with patch('checks.shutil.which', return_value=None):
            errors, warnings = check(self.P, 'media')
        self.assertTrue(any('未验证' in w for w in warnings))

    def test_candidate_limit_and_review_notes(self):
        self.asset('C'); self.asset('C'); self.asset('C')
        with self.assertRaisesRegex(ValueError, '上限'):
            self.asset('C')
        self.db.review('C', 'v003', 'rework', 'change sleeve')
        self.assertIsNone(self.db.item('C')['selected'])
        self.assertIn('change sleeve', self.db.candidate('C', 'v003')['notes'])

    def test_mask_protection_verifier(self):
        _, base = self.asset('C')
        mask = self.root / 'mask.png'
        Image.new('RGB', (64, 64), 'black').save(mask)
        v, out = self.db.reserve('M', {'route': 'masked', 'inputs': [{'file': base.relative_to(self.root).as_posix(), 'sha256': digest(base)}, {'file': 'mask.png', 'sha256': digest(mask)}]})
        out.parent.mkdir(parents=True)
        Image.new('RGB', (64, 64), 'red').save(out)
        with self.assertRaisesRegex(ValueError, '保护区域'):
            finish_candidate(self.db, 'M', v, out)

    def test_board_escapes_and_shows_pending(self):
        v, _ = self.asset('C', False)
        self.db.item('C')['name'] = '<script>alert(1)</script>'
        self.db.save()
        content = render(self.P)
        self.assertNotIn('<script>alert(1)</script>', content)
        self.assertIn('待审核', content)
        self.assertIn('&lt;script&gt;', content)

    def test_recipe_change_invalidates_selected_image(self):
        v, _ = self.asset('C')
        self.db.candidate('C', v)['recipe'] = {'file': 'shots.csv', 'id_column': 'shot', 'id': 'S01', 'row': dict(self.row)}
        self.db.save()
        self.write_rows([dict(self.row, first_prompt='changed pose')])
        self.assertTrue(self.db.stale('C', v))

    def test_new_camera_not_locked_to_old_composition(self):
        self.asset('ROOM')
        job = {'key': 'B', 'prompt': 'new camera', 'image_route': 'edit',
               'references': '[{"asset":"ROOM","purpose":"geography"}]'}
        p = prepare(self.P, job, self.db)
        self.assertNotIn('Preserve the original composition', p['prompt'])

    def test_asset_path_escape_rejected(self):
        from assets import local_path
        with self.assertRaises(ValueError):
            local_path(self.root, '../outside.png')

    def test_mask_dimensions_and_crop_bounds(self):
        self.asset('C')
        Image.new('RGB', (32, 32)).save(self.root / 'small.png')
        with self.assertRaisesRegex(ValueError, '尺寸'):
            prepare(self.P, {'key': 'M', 'prompt': 'edit', 'image_route': 'masked', 'edit_source': 'C', 'mask': 'small.png'}, self.db)
        with self.assertRaisesRegex(ValueError, '超出'):
            prepare(self.P, {'key': 'X', 'image_route': 'crop', 'edit_source': 'C', 'crop': '{"x":60,"y":0,"width":20,"height":20}'}, self.db)

    def test_style_gate_inherits_approved_choice(self):
        from styles import load_style
        cfg = json.loads((self.root / 'project.json').read_text(encoding='utf-8'))
        cfg['review_style_asset'] = 'LOOK'
        atomic_json(self.root / 'project.json', cfg)
        self.P = load_project(str(self.root))
        job = {'key': 'C', 'kind': 'character', 'prompt': 'person', 'image_route': 't2i'}
        with self.assertRaises(ValueError):
            prepare(self.P, job, self.db)
        v, _ = self.asset('LOOK')
        self.db.item('LOOK')['kind'] = 'style'
        self.db.candidate('LOOK', v)['style'] = load_style('woodcut_clean')._d
        prepared = prepare(self.P, job, self.db)
        self.assertEqual(prepared['style'].id, 'woodcut_clean')


if __name__ == '__main__':
    unittest.main(verbosity=2)
