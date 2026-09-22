"""Read product label text locally. No ERP modules, network, or credential access."""
import sys, json, io, time, warnings
from pathlib import Path

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    sys.path.insert(0, sys.argv[1])
    from PIL import Image, ImageOps
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    warnings.simplefilter('error', Image.DecompressionBombWarning)
    Image.MAX_IMAGE_PIXELS = 24_000_000
    started = time.monotonic()
    raw = sys.stdin.buffer.read(5 * 1024 * 1024 + 1)
    if not raw or len(raw) > 5 * 1024 * 1024:
        raise ValueError('image_size')
    source = Image.open(io.BytesIO(raw))
    if source.format not in ('PNG', 'JPEG', 'WEBP') or source.width * source.height > 24_000_000:
        raise ValueError('image_format_or_dimensions')
    image = ImageOps.exif_transpose(source).convert('RGB')
    rotation = int(sys.argv[2])
    if rotation not in (0, 90, 180, 270):
        raise ValueError('rotation')
    if rotation:
        image = image.rotate(-rotation, expand=True)
    image.thumbnail((2200, 2200))
    engine = RapidOCR(det_limit_side_len=2000, rec_batch_num=6, intra_op_num_threads=4, inter_op_num_threads=2)
    result, _ = engine(np.asarray(image))
    tokens = [{'text': str(text)[:500], 'confidence': round(float(score), 3),
               'box': [[round(float(x), 1), round(float(y), 1)] for x, y in box]}
              for box, text, score in (result or [])][:300]
    print(json.dumps({'tokens': tokens, 'width': image.width, 'height': image.height,
                      'seconds': round(time.monotonic() - started, 2)}, ensure_ascii=False))

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__}))
        sys.exit(1)
