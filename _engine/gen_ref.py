"""资产候选入口：在 asset_jobs.csv 中声明定妆、场景和道具任务。"""
import sys
from image_pipeline import main

if __name__ == "__main__":
    raise SystemExit(main(["--assets", *sys.argv[1:]]))
