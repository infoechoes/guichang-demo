"""Infer page rotation from existing OCR boxes and a bounded text-classifier vote.

Angles are clockwise, matching the browser. No image dimensions or product data
are used as orientation hints. RapidOCR rotates vertical text crops 90° CCW
before its 0/180 classifier; translate that vote back to a whole-page angle.
"""
import re
import time
import numpy as np

MAX_CROPS = 16

def rotation_vote(vertical, labels):
    votes = [str(label) for label, score in labels if float(score) >= .90 and str(label) in {'0', '180'}]
    if len(votes) < 3:
        return None, 0
    winner = max(('0', '180'), key=votes.count)
    agreement = votes.count(winner) / len(votes)
    if agreement < .75:
        return None, agreement
    angle = (270 if winner == '0' else 90) if vertical else (0 if winner == '0' else 180)
    return angle, agreement

def detect_orientation(image, tokens, engine):
    started = time.perf_counter()
    info = {'rotation': 0, 'confident': False, 'crops': 0, 'seconds': 0, 'reason': 'insufficient_text'}
    candidates = {False: [], True: []}
    for token in tokens:
        text = token.get('text', '')
        # Digits and single Chinese glyphs are unreliable direction evidence.
        if token.get('confidence', 0) < .6 or (len(re.findall(r'[\u4e00-\u9fff]', text)) < 2 and len(re.findall(r'[a-zA-Z]', text)) < 4):
            continue
        box = np.asarray(token['box'], dtype=np.float32)
        width = max(np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[2] - box[3]))
        height = max(np.linalg.norm(box[0] - box[3]), np.linalg.norm(box[1] - box[2]))
        if min(width, height) < 6 or max(width, height) / min(width, height) < 2:
            continue
        candidates[height > width].append((len(text), box))
    total = sum(map(len, candidates.values()))
    if total >= 3:
        vertical = len(candidates[True]) > len(candidates[False])
        selected = candidates[vertical]
        if len(selected) / total >= .65:
            boxes = [box for _, box in sorted(selected, key=lambda item: item[0], reverse=True)[:MAX_CROPS]]
            crops = engine.get_crop_img_list(np.asarray(image), boxes)
            _, labels, _ = engine.text_cls(crops)
            angle, agreement = rotation_vote(vertical, labels)
            info.update(crops=len(crops), agreement=round(agreement, 3), reason='vote' if angle is not None else 'uncertain')
            if angle is not None:
                info.update(rotation=angle, confident=True)
        else:
            info['reason'] = 'mixed_directions'
    info['seconds'] = round(time.perf_counter() - started, 3)
    return info
