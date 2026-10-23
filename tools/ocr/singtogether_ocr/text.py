"""4단계 텍스트 인식: Apple Vision(한국어+영어)으로 띠 안의 글자를 읽고 코드 기호 / 가사로 나눈다.

코드 띠는 글자 덩어리(blob)마다 따로 읽어서 코드 하나하나의 x좌표를 정확히 얻는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from . import debug
from .ingest import Page
from .layout import System

# ChordText.swift의 정규식을 그대로 옮긴 것
CHORD_RE = re.compile(r"^([A-G])([#b♯♭]?)([^\s/]*)(?:/([A-G])([#b♯♭]?))?$")
QUALITY_RE = re.compile(r"^(?:maj|min|dim|aug|sus|add|alt|no|omit|m|M|Δ|°|ø|\+|-|#|b|♯|♭|\d|\(|\)|,)*$")

# OCR이 흔히 틀리는 글자
_FIXES = str.maketrans({"♯": "#", "♭": "b", "²": "2", "³": "3", "|": "/", "\\": "/", "∕": "/", "⁄": "/"})


@dataclass
class TextBox:
    text: str
    conf: float
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def cx(self) -> float: return (self.x0 + self.x1) / 2


def is_chord(s: str) -> bool:
    m = CHORD_RE.match(s)
    return bool(m) and bool(QUALITY_RE.match(m.group(3)))


def normalize_chord(s: str) -> str:
    s = s.strip().translate(_FIXES).replace(" ", "")
    s = re.sub(r"^[^A-Ga-g]+", "", s)                         # 괄호 번호("2.C2"), 점 등 앞쪽 찌꺼기
    s = s[:1].upper() + s[1:]
    s = re.sub(r"^([A-G][#b]?)I([A-G])", r"\1/\2", s)         # D/F# 을 DIF# 로 읽는 경우
    s = re.sub(r"^([A-G][#b]?)[z°º]$", r"\g<1>2", s)          # 윗첨자 2 를 z·° 로 읽는 경우 (찬양 악보의 C2)
    s = re.sub(r"[-_.,]+$", "", s)                            # 끝에 붙는 선 조각
    return s


# (여백 배수, 최소 변 길이, 언어). Vision은 여백·크기·언어에 따라 결과가 달라서 여러 번 읽고 투표한다.
# 한국어 모델은 윗첨자(C²)를 "2c2"처럼이라도 읽어 준다.
_OCR_PASSES = [(0.4, 64, ("en-US",)), (1.0, 64, ("en-US",)), (1.0, 128, ("en-US",)), (1.0, 128, ("ko-KR", "en-US"))]


def read_chords(page: Page, systems: list[System], debug_dir: Path | None = None) -> list[list[TextBox]]:
    """오선마다 코드 띠의 글자 덩어리를 하나씩 읽어 코드 문법에 맞는 것만 남긴다."""
    il = page.interline
    result, all_boxes, kept = [], [], []
    for sys_ in systems:
        y0, y1 = sys_.chord_band
        found = []
        for (bx0, by0, bx1, by1) in find_text_blobs(page.binary[y0:y1], il):
            box = (bx0, y0 + by0, bx1, y0 + by1)
            all_boxes.append(box)
            votes: dict[str, list[float]] = {}
            raw: list[str] = []
            for pad_f, min_side, langs in _OCR_PASSES:
                pad = int(pad_f * il)
                crop = page.gray[max(0, y0 + by0 - pad):y0 + by1 + pad, max(0, bx0 - pad):bx1 + pad]
                if crop.size == 0:
                    continue
                for b in recognize(crop, langs, correction=False, custom_words=CHORD_VOCAB, min_side=min_side):
                    chord = normalize_chord(b.text)
                    raw.append(chord)
                    if is_chord(chord):
                        votes.setdefault(chord, []).append(b.conf)
            weak = not votes or max(len(v) for v in votes.values()) < 2 or all(re.fullmatch(r"[A-G][#b]?", c) for c in votes)
            if weak:
                # 윗첨자가 붙은 코드(A♭, C², D⁷)는 통째로 읽으면 자주 틀린다("A6", "C'"). 큰 글자와 윗첨자를 나눠 읽는다
                hint = next((m.group(0) for s in raw if (m := re.match(r"[A-G][#b]?", s))), None)
                if (sup := read_superscript(page, box, il, hint)):
                    votes = {sup[0]: [sup[1]]}
            if votes:
                chord, confs = max(votes.items(), key=lambda kv: (len(kv[1]), max(kv[1])))
                tb = TextBox(chord, max(confs) * len(confs) / len(_OCR_PASSES), *box)
                found.append(tb)
                kept.append(tb)
        found.sort(key=lambda b: b.x0)
        result.append(found)
    if debug_dir:
        img = debug.canvas(page.gray)
        debug.boxes(img, all_boxes, "band")
        debug.boxes(img, [(b.x0, b.y0, b.x1, b.y1) for b in kept], "chord",
                    labels=[f"{b.text} {b.conf:.2f}" for b in kept])
        debug.caption(img, f"{page.name}  chords {len(kept)} / blobs {len(all_boxes)}")
        debug.save(debug_dir / f"{page.name}-4-chords.png", img)
    return result


def read_superscript(page: Page, box: tuple[int, int, int, int], il: float,
                     hint: str | None = None) -> tuple[str, float] | None:
    """덩어리 안에서 큰 글자(근음)와 오른쪽 위의 작은 글자(윗첨자)를 나눠 각각 읽는다. 둘 다 있어야 결과를 준다.
    hint: 통째로 읽었을 때 나온 근음 글자. 나눠 읽기가 실패하면 이것을 쓴다."""
    x0, y0, x1, y1 = box
    crop = page.binary[y0:y1, x0:x1]
    n, labels, stats, _ = cv2.connectedComponentsWithStats((crop < 128).astype(np.uint8), connectivity=8)
    comps = [(i, tuple(int(v) for v in stats[i][:4])) for i in range(1, n) if stats[i][3] >= 0.3 * il]
    if len(comps) < 2:
        return None
    main_i, main = max(comps, key=lambda c: c[1][3])            # 가장 키 큰 것 = 근음 글자(와 붙은 글자)
    mx, my, mw, mh = main
    right = [(i, c) for i, c in comps if i != main_i and c[0] >= mx + mw * 0.6 and c[3] < 0.75 * mh]
    sups = sorted([(i, c) for i, c in right if c[1] + c[3] < my + mh * 0.7], key=lambda ic: ic[1][0])   # 위에 붙은 것
    subs = sorted([(i, c) for i, c in right if c[1] + c[3] >= my + mh * 0.7], key=lambda ic: ic[1][0])  # 아래에 붙은 것(m, sus)
    if not sups and not subs:
        return None
    def ocr(c, words, reps=3):
        """Vision은 글자 하나짜리 조각을 무시하므로, 같은 조각을 reps번 이어 붙여 단어처럼 보이게 한다.
        여백은 0.5il이 가장 잘 읽혔다 (1il 이상이면 자주 빈 결과)."""
        cx, cy, cw, ch = c
        for pad in (int(0.5 * il), int(0.25 * il), int(il)):
            g = page.gray[max(0, y0 + cy - pad):y0 + cy + ch + pad, max(0, x0 + cx - pad):x0 + cx + cw + pad]
            gap = np.full((g.shape[0], max(4, int(0.3 * il))), 255, np.uint8)
            tiled = np.hstack(sum(([g, gap] for _ in range(reps)), [])[:-1])
            r = recognize(tiled, ("en-US",), correction=False, custom_words=words, min_side=128)
            if r:
                best = max(r, key=lambda b: b.conf)
                first = best.text.split()[0] if best.text.split() else best.text    # 띄어 읽혔으면 첫 조각이 원본
                return TextBox(_untile(first, reps), best.conf, best.x0, best.y0, best.x1, best.y1)
        return None

    root = ocr(main, CHORD_VOCAB, reps=3) or ocr(main, CHORD_VOCAB, reps=5)
    rm = re.match(r"([A-G])([#b]?)(.*)", normalize_chord(root.text)) if root else None
    if not rm and hint:
        root, rm = TextBox(hint, 0.5, 0, 0, 0, 0), re.match(r"([A-G])([#b]?)(.*)", hint)
    if not rm:
        return None
    letter, acc, rest = rm.groups()
    quality = rest if re.fullmatch(r"(m|maj|min|sus\d?|dim|aug)?(/[A-G][#b]?)?", rest) else ""
    confs = [root.conf]
    digits = ""
    for i, c in sups:
        mask = labels[c[1]:c[1] + c[3], c[0]:c[0] + c[2]] == i
        if not acc and _is_flat_glyph(mask):
            acc = "b"
            continue
        d = ocr(c, ["#", "2", "7", "9", "6", "4", "11", "13"])
        t = d.text.translate(_FIXES).strip() if d else ""
        if t == "#" and not acc:
            acc = "#"
        elif re.sub(r"\D", "", t):
            digits += re.sub(r"\D", "", t)
            confs.append(d.conf)
    if subs and not quality:
        q = ocr((subs[0][1][0], min(c[1] for _, c in subs), max(c[0] + c[2] for _, c in subs) - subs[0][1][0],
                 max(c[1] + c[3] for _, c in subs) - min(c[1] for _, c in subs)), ["m", "maj", "sus", "dim", "aug", "min"])
        if q and re.fullmatch(r"(m|maj|min|sus|dim|aug)", q.text.strip()):
            quality = q.text.strip()
    if not acc and not digits and not quality:
        return None                                            # 윗첨자를 못 읽으면 지어내지 않는다
    bass = ""
    if "/" in quality:
        quality, bass = quality.split("/", 1)
        bass = "/" + bass
    chord = letter + acc + quality + digits + bass
    return (chord, min(confs)) if is_chord(chord) else None


def _untile(text: str, reps: int) -> str:
    """같은 조각을 reps번 이어 붙여 읽은 결과("222", "EE", "A/C#A/C#")에서 한 조각만 남긴다."""
    for k in range(reps, 1, -1):
        if len(text) % k == 0 and text == text[: len(text) // k] * k:
            return text[: len(text) // k]
    if len(text) > 1 and len(set(text)) == 1:
        return text[0]
    return text


def _is_flat_glyph(mask: np.ndarray) -> bool:
    """♭: 위쪽 1/4은 왼쪽 기둥만 있고, 아래 오른쪽에 배가 있다. 숫자(2·6·7·9)는 위쪽에 가로로 넓게 잉크가 있다."""
    h, w = mask.shape
    if h < 4 or w < 2 or h < 1.2 * w:
        return False
    top_cols = np.flatnonzero(mask[: max(1, h // 4)].any(axis=0))
    if top_cols.size == 0:
        return False
    return top_cols.max() < 0.55 * w and mask[h // 2:, w // 2:].mean() > 0.2


def find_text_blobs(binary_band: np.ndarray, interline: float) -> list[tuple[int, int, int, int]]:
    """띠 안의 검정 픽셀을 가로로 뭉쳐서 글자 덩어리 상자를 만든다. 너무 작거나(점) 너무 큰(기둥·빔) 건 버린다."""
    if binary_band.size == 0:
        return []
    ink = (binary_band < 128).astype(np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, int(0.6 * interline)), max(3, int(0.25 * interline))))
    merged = cv2.dilate(ink, kernel)
    n, _, stats, _ = cv2.connectedComponentsWithStats(merged, connectivity=8)
    out = []
    for x, y, w, h, area in stats[1:]:
        if h < 0.4 * interline or h > 3.5 * interline or w < 0.3 * interline:
            continue
        out.append((int(x), int(y), int(x + w), int(y + h)))
    return sorted(out)


def recognize(gray: np.ndarray, langs=("ko-KR", "en-US"), correction: bool = True,
              custom_words=(), min_side: int = 0) -> list[TextBox]:
    """Vision 문자 인식. 좌표는 넘긴 이미지 기준 픽셀. 작은 조각은 min_side까지 키워서 읽는다."""
    import Vision
    from Foundation import NSData

    scale = 1.0
    if min_side and min(gray.shape) < min_side:
        scale = min_side / min(gray.shape)
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    h, w = gray.shape
    png = cv2.imencode(".png", gray)[1].tobytes()
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(NSData.dataWithBytes_length_(png, len(png)), {})
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(list(langs))
    req.setUsesLanguageCorrection_(correction)
    if custom_words:
        req.setCustomWords_(list(custom_words))
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(f"Vision 실패: {err}")
    out = []
    for obs in req.results() or []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        bb = obs.boundingBox()          # 0–1 정규화, 원점이 왼쪽 아래
        x0, x1 = bb.origin.x * w, (bb.origin.x + bb.size.width) * w
        y1, y0 = (1 - bb.origin.y) * h, (1 - bb.origin.y - bb.size.height) * h
        out.append(TextBox(str(cands[0].string()), float(cands[0].confidence()),
                           int(x0 / scale), int(y0 / scale), int(x1 / scale), int(y1 / scale)))
    return out


def _chord_vocab() -> list[str]:
    roots = ["C", "C#", "Db", "D", "D#", "Eb", "E", "F", "F#", "Gb", "G", "G#", "Ab", "A", "A#", "Bb", "B"]
    suffixes = ["", "m", "7", "m7", "maj7", "M7", "sus4", "sus2", "sus", "2", "add9", "add2", "dim", "aug", "6", "9", "m9", "7sus4"]
    words = [r + s for r in roots for s in suffixes]
    words += [f"{r}/{b}" for r in roots for b in roots]
    return words


CHORD_VOCAB = _chord_vocab()
