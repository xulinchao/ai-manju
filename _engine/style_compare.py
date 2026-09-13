"""Compare candidate styles without editing project.json or existing images."""
import argparse
from image_pipeline import main as generate

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True)
    ap.add_argument('--styles', default='picturebook_clean,woodcut_clean')
    ap.add_argument('--shot', default='S01')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    failed = 0
    for style in a.styles.split(','):
        args = ['--project', a.project, '--only', a.shot, '--style', style.strip(), '--force']
        if a.dry_run:
            args.append('--dry-run')
        failed += generate(args)
    return int(bool(failed))

if __name__ == '__main__':
    raise SystemExit(main())
