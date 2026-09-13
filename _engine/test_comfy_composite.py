"""Optional local integration test; synthetic pixels only, no model sampling."""
import json
import tempfile
from pathlib import Path
from PIL import Image, ImageChops
from image_pipeline import Comfy


def main():
    client = Comfy('http://127.0.0.1:8188')
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        base = Image.new('RGB', (64, 64))
        base.putdata([(x * 4, y * 4, (x + y) * 2) for y in range(64) for x in range(64)])
        base.save(root / 'base.png')
        source = Image.new('RGB', (64, 64), (250, 40, 20))
        source.save(root / 'source.png')
        mask = Image.new('RGB', (64, 64), 'black')
        for y in range(16, 48):
            for x in range(16, 48):
                mask.putpixel((x, y), (255, 255, 255))
        mask.save(root / 'mask.png')
        names = [client.upload(root / n) for n in ('base.png', 'source.png', 'mask.png')]
        wf = {
            'base': {'class_type': 'LoadImage', 'inputs': {'image': names[0]}},
            'source': {'class_type': 'LoadImage', 'inputs': {'image': names[1]}},
            'mask': {'class_type': 'LoadImageMask', 'inputs': {'image': names[2], 'channel': 'red'}},
            'composite': {'class_type': 'ImageCompositeMasked', 'inputs': {'destination': ['base', 0], 'source': ['source', 0], 'mask': ['mask', 0], 'x': 0, 'y': 0, 'resize_source': True}},
            'save': {'class_type': 'SaveImage', 'inputs': {'images': ['composite', 0], 'filename_prefix': 'pipeline_checks/composite'}}}
        result = client.run(wf, 'save', lambda pid: print('synthetic composite prompt_id:', pid), timeout=60)
        client.download(result, root / 'result.png')
        with Image.open(root / 'result.png') as out:
            expected = Image.composite(source, base, mask.getchannel('R'))
            assert ImageChops.difference(expected, out.convert('RGB')).getbbox() is None, 'pixel mismatch'
        print('PASS: native ComfyUI masked composite, exact protected and edited pixels')


if __name__ == '__main__':
    main()
