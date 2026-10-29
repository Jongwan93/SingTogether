"""코드 차트: 원본 이미지 위에서 코드 글자만 새 키로 바꿔 쓴다. 제목·가사·글꼴·워터마크·배치는 그대로 남는다.

1. 코드 자리: Apple Vision (여러 배율 합집합 + 못 읽은 잉크 덩어리 재OCR, chordsheet.py와 같은 방법)
2. 코드 글자: 받아 적은 텍스트(Claude/Gemini 전사 또는 Vision)와 순서대로 맞춰 바로잡는다. 텍스트에만 있는 코드는
   같은 줄의 아직 안 바뀐 잉크 덩어리에 붙인다.
3. 새 코드: Swift 이조(singtogether-transpose)로 한꺼번에 옮겨 ♯/♭ 표기를 앱과 똑같이 맞춘다.
4. 그리기: 글자 잉크만 배경색으로 지우고, 원래 글자색·비슷한 글꼴(후보 중 원래 글자와 가장 닮은 것)·같은 높이로 쓴다.
   자리가 모자라면 가로로 눌러 쓴다.
5. 코드 줄에 바뀌지 않은 글자 덩어리가 남으면 확인 그림에 빨갛게 표시하고 개수를 알려 준다.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import chordsheet
from .hybrid import _chord_cost, align

_SUP = "/System/Library/Fonts/Supplemental/"
_SYS = "/System/Library/Fonts/"
FONTS = {   # 이름 → (파일, 컬렉션 안 번호). 원래 글자와 가장 닮은 것을 고른다
    "Arial": (_SUP + "Arial.ttf", 0), "Arial Bold": (_SUP + "Arial Bold.ttf", 0),
    "Helvetica": (_SYS + "Helvetica.ttc", 0), "Helvetica Bold": (_SYS + "Helvetica.ttc", 1),
    "Futura": (_SUP + "Futura.ttc", 0), "Futura Bold": (_SUP + "Futura.ttc", 2),
    "Avenir": (_SYS + "Avenir.ttc", 0), "Avenir Heavy": (_SYS + "Avenir.ttc", 4),
    "Gill Sans": (_SUP + "GillSans.ttc", 0), "Gill Sans Bold": (_SUP + "GillSans.ttc", 1),
    "Verdana": (_SUP + "Verdana.ttf", 0), "Verdana Bold": (_SUP + "Verdana Bold.ttf", 0),
    "Trebuchet": (_SUP + "Trebuchet MS.ttf", 0), "Trebuchet Bold": (_SUP + "Trebuchet MS Bold.ttf", 0),
    "Times": (_SUP + "Times New Roman.ttf", 0), "Times Bold": (_SUP + "Times New Roman Bold.ttf", 0),
    "Georgia": (_SUP + "Georgia.ttf", 0), "Georgia Bold": (_SUP + "Georgia Bold.ttf", 0),
}


@dataclass
class Chord:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str                    # 원래 코드 (바로잡은 것)
    src: str                     # "both" / "vision" / "text"
    col: int = 0
    new: str = ""
    row: int | None = None


@dataclass
class Result:
    image: Path | None = None
    review: Path | None = None
    keys: str = ""
    replaced: int = 0
    missed: int = 0
    font: str = ""
    notes: list[str] = field(default_factory=list)


# MARK: 읽기

def load_color(src: Path) -> np.ndarray:
    """RGB uint8. PDF는 첫 쪽을 300dpi로."""
    if src.suffix.lower() == ".pdf":
        import fitz
        with fitz.open(src) as doc:
            pix = doc[0].get_pixmap(dpi=300, colorspace=fitz.csRGB)
            return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3).copy()
    bgr = cv2.imdecode(np.fromfile(src, np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError(f"Cannot read image: {src}")
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


_STRIP = "(),;:"


def chord_word(word: str) -> str | None:
    """'Bb9-Dm7-Eb-F', 'Am7', '(Am7)' → 코드 낱말(조각을 정규화해 '-'로 이은 것). 코드가 아니면 None."""
    core = word.strip(_STRIP)
    parts = [p for p in re.split(r"[-–—]", core)]
    if not core or any(not p for p in parts):
        return None
    toks = [chordsheet.chord_token(p) for p in parts]
    return "-".join(toks) if all(toks) else None


def _text_lines(chart_text: str) -> list[list[str]]:
    """받아 적은 텍스트의 코드 줄마다 코드 낱말 목록."""
    out = []
    for line in chart_text.splitlines():
        if not chordsheet.is_chord_line(line):
            continue
        toks = [cw for raw in line.split() if (cw := chord_word(raw))]
        if toks:
            out.append(toks)
    return out


def _words(gray: np.ndarray, k: int = 3) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Vision 낱말 + 상자 (원본 좌표, 글자 상자로 계산해 글줄 상자보다 정확하다)."""
    from .lyrics import recognize_lines
    big = cv2.resize(gray, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
    out = []
    for s, _, boxes, _ in recognize_lines(big, ("en-US",), False):
        for m in re.finditer(r"\S+", s):
            bx = [b for b in boxes[m.start():m.end()] if b]
            if bx:
                out.append((m.group(), (min(b[0] for b in bx) / k, min(b[1] for b in bx) / k,
                                        max(b[2] for b in bx) / k, max(b[3] for b in bx) / k)))
    return out


def _near_chord(word: str, tokens: set[str]) -> bool:
    """텍스트의 코드와 거의 같은 낱말인가 (한 글자쯤 잘못 읽은 코드). 대문자 A–G로 시작하고 짧아야 한다."""
    from difflib import SequenceMatcher
    w_ = word.strip(_STRIP)
    if not tokens or not re.match(r"[A-G]", w_) or len(w_) > 7 or not re.search(r"[#/0-9]|sus|maj|m", w_):
        return False
    return max(SequenceMatcher(None, w_, t).ratio() for t in tokens) >= 0.75


def find_chords(gray: np.ndarray, chart_text: str | None) -> tuple[list[Chord], list[tuple], float]:
    """(바꿀 코드 목록, 코드 줄에 남은 못 바꾼 덩어리, 글줄 높이).

    단위는 낱말이다 ('Bb9-Dm7-Eb-F'는 잉크가 이어져 있어 한 번에 지우고 한 번에 쓴다).
    1. 붙임표 진행(코드 2개 이상)은 어디에 있든 Vision이 읽은 그대로 바꾼다 ("(Repeat Chorus) Eb-F (2x)").
    2. 나머지는 이미지의 코드 줄과 받아 적은 텍스트의 코드 줄을 순서대로 맞춘 뒤, 줄 안의 '자리'(Vision 코드 낱말 +
       아무것도 안 덮은 글자 덩어리)를 x 순서로 텍스트의 코드에 배정한다. 수가 같으면 그대로, 다르면 글자 비교로 정렬."""
    lines, found = chordsheet._vision_tokens(gray)
    found += chordsheet._blob_tokens(gray, found)
    words = _words(gray)
    h, w = gray.shape
    lh = float(np.median([bb[3] - bb[1] for _, bb in words])) if words else 20.0

    def inside(x0, y0, x1, y1, boxes):
        """상자(x0..y1)가 boxes 중 하나와 같은 글자인가: 가로로 겹치고, 그 상자의 세로 중심이 이 상자 안에 있다.
        (Vision은 코드 줄과 가사 줄을 한 글줄로 묶어 키 큰 상자를 줄 때가 있어 '중심이 상자 안'만으로는 안 된다)"""
        cx = (x0 + x1) / 2
        return any(b[0] - 1 <= cx <= b[2] + 1 and y0 - 2 <= (b[1] + b[3]) / 2 <= y1 + 2 for b in boxes)

    left = [l for l in lines if l[1][2] < w * 0.52]
    right = [l for l in lines if l[1][0] > w * 0.48]
    crossing = [l for l in lines if l[1][0] < w * 0.45 and l[1][2] > w * 0.55]
    two_col = len(left) >= 5 and len(right) >= 5 and len(crossing) <= max(2, len(lines) // 10)
    col = (lambda x: 1 if two_col and x > w * 0.5 else 0)

    text_lines = _text_lines(chart_text) if chart_text else []
    squeezed_text = {"".join(t) for t in text_lines}
    text_tokens = {t for line in text_lines for t in line if "-" not in t}

    # 낱말 분류: 코드 낱말 / 붙임표 진행 / 그 밖(가사·표제·(2x))
    pairs_text = {}                                   # "Gsus4G" → ("Gsus4", "G"): 띄어쓰기를 잃고 붙어 읽힌 두 코드
    for line in text_lines:
        for a_, b_ in zip(line, line[1:]):
            pairs_text[a_ + b_] = (a_, b_)
    chord_units, run_units, other_boxes = [], [], []
    for wtxt, bb in words:
        sq = wtxt.strip(_STRIP)
        if sq in pairs_text:
            a_, b_ = pairs_text[sq]
            cut = bb[0] + (bb[2] - bb[0]) * len(a_) / len(sq)
            chord_units.append(Chord(bb[0], bb[1], cut - 1, bb[3], a_, "vision", col(bb[0])))
            chord_units.append(Chord(cut + 1, bb[1], bb[2], bb[3], b_, "vision", col(cut)))
            continue
        cw = chord_word(wtxt)
        if cw and "-" in cw:
            run_units.append(Chord(*bb, cw, "vision", col(bb[0])))
        elif cw:
            chord_units.append(Chord(*bb, cw, "vision", col(bb[0])))
        elif re.sub(r"\s+", "", wtxt) in squeezed_text:       # 띄어쓰기를 잃은 코드 줄 ("CDEm"): 덩어리로 다시 나눈다
            continue
        elif _near_chord(wtxt, text_tokens):                  # 잘못 읽은 코드 ("E/GH"): 자리만 쓰고 글자는 텍스트에서
            chord_units.append(Chord(*bb, wtxt, "vision", col(bb[0])))
        else:
            other_boxes.append(bb)
    # 여러 번 읽기에서만 나온 코드 (외따로 선 한 글자 등): 이미 있는 낱말·다른 낱말과 겹치지 않으면 더한다
    known = [(c.x0, c.y0, c.x1, c.y1) for c in chord_units + run_units]
    for x0, y0, x1, y1, tok in found:
        if not inside(x0, y0, x1, y1, known) and not inside(x0, y0, x1, y1, other_boxes):
            chord_units.append(Chord(x0, y0, x1, y1, tok, "vision", col(x0)))
            known.append((x0, y0, x1, y1))
    # 붙임표 진행 조각을 따로 읽은 것은 빼고, 진행 상자 안의 낱말 코드도 뺀다
    run_boxes = [(c.x0, c.y0, c.x1, c.y1) for c in run_units]
    chord_units = [c for c in chord_units if not inside(c.x0, c.y0, c.x1, c.y1, run_boxes)]

    # 코드 줄(y 띠): 코드 낱말이 있는 곳 + 띄어쓰기를 잃은 코드 줄
    rows: list[list] = []
    def add_row(c_, y0, y1):
        for r in rows:
            if r[0] == c_ and not (y1 < r[1] - 0.3 * lh or y0 > r[2] + 0.3 * lh):
                r[1], r[2] = min(r[1], y0), max(r[2], y1)
                return
        rows.append([c_, y0, y1])
    for c in chord_units:
        add_row(c.col, c.y0, c.y1)
    for wtxt, bb in words:
        if re.sub(r"\s+", "", wtxt) in squeezed_text and not chord_word(wtxt):
            add_row(col(bb[0]), bb[1], bb[3])
    rows.sort(key=lambda r: (r[0], r[1]))
    row_of = lambda c_, cy: next((i for i, r in enumerate(rows) if r[0] == c_ and r[1] - 0.25 * lh <= cy <= r[2] + 0.25 * lh), None)

    # 코드 줄 안의 글자 덩어리 (다른 낱말·진행은 뺌)
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    ink = (binary < 128).astype(np.uint8)
    merged = cv2.dilate(ink, cv2.getStructuringElement(cv2.MORPH_RECT, (max(2, int(lh * 0.3)), 1)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(merged, 8)
    blobs = []
    for i in range(1, n):
        x, y, bw, bh, _ = stats[i]
        if not (0.35 * lh <= bh <= 1.8 * lh) or bw > 8 * lh:
            continue
        ri = row_of(col(x), y + bh / 2)
        if ri is not None and not inside(x, y, x + bw, y + bh, other_boxes + run_boxes):
            blobs.append((x, y, x + bw, y + bh, ri))

    def covered(b, units) -> bool:
        return any(c.x0 - 2 <= (b[0] + b[2]) / 2 <= c.x1 + 2 and c.y0 - 0.3 * lh <= (b[1] + b[3]) / 2 <= c.y1 + 0.3 * lh
                   for c in units)

    for c in chord_units:
        c.row = row_of(c.col, (c.y0 + c.y1) / 2)

    if not text_lines and chord_units:
        hs = np.median([c.y1 - c.y0 for c in chord_units])
        chord_units = [c for c in chord_units if c.y1 - c.y0 <= 1.5 * hs]
    chords = list(chord_units)
    lost_lines: list[str] = []
    if text_lines:
        units_by_row: list[list] = []
        for ri in range(len(rows)):
            us = [c for c in chord_units if c.row == ri] + \
                 [Chord(b[0], b[1], b[2], b[3], "", "text", rows[ri][0], row=ri) for b in blobs
                  if b[4] == ri and not covered(b, chord_units)]
            units_by_row.append(sorted(us, key=lambda u: u.x0))

        def row_cost(units, toks):
            from difflib import SequenceMatcher
            sim = SequenceMatcher(None, [u.text for u in units], toks, autojunk=False).ratio()
            return (1 - sim) + 0.15 * abs(len(units) - len(toks))
        chords = []
        for i, j in align(units_by_row, text_lines, row_cost, 0.9, 0.9):
            if i is None:
                done = {c.text for c in run_units}
                if any("-" not in t or t not in done for t in text_lines[j]):     # 진행은 따로 바꿨다
                    lost_lines.append(" ".join(text_lines[j]))
                continue
            units = units_by_row[i]
            if j is None:                    # 텍스트의 어떤 코드 줄과도 안 맞는 줄 = 제목·가사 속 낱말("Here I Am") → 안 바꾼다
                continue
            toks = text_lines[j]
            if len(units) == len(toks):
                for u, t in zip(units, toks):
                    u.text, u.src = t, ("both" if u.text else "text")
                chords += units
            else:
                cost = lambda u, t: _chord_cost(u.text, t) if u.text else 0.6
                for a_, b_ in align(units, toks, cost, 0.8, 0.8):
                    if a_ is not None and b_ is not None:
                        u = units[a_]
                        u.text, u.src = toks[b_], ("both" if u.text else "text")
                        chords.append(u)
                    elif a_ is not None and units[a_].text:
                        chords.append(units[a_])
    matched_rows = {c.row for c in chords if c.row is not None} if text_lines else set(range(len(rows)))
    missed = [b for b in blobs if b[4] in matched_rows and not covered(b, chords)]
    find_chords.lost_lines = lost_lines                 # 텍스트에는 있는데 이미지에서 못 찾은 코드 줄
    find_chords.obstacles = other_boxes                 # 새 코드가 넘어가면 안 되는 다른 낱말
    return chords + run_units, missed, lh


# MARK: 이조

def _transpose_tokens(tokens: list[str], semitones: int) -> tuple[list[str], str]:
    """Swift 이조기로 한꺼번에 (조 판별과 ♯/♭ 표기를 앱과 똑같이). (새 코드들, "G -> A")"""
    from .render import find_transpose
    binary = find_transpose()
    if not binary:
        raise RuntimeError("singtogether-transpose not found. Run ./build.sh in the repository root first")
    with tempfile.TemporaryDirectory() as d:
        src, out = Path(d) / "chords.txt", Path(d) / "out.txt"
        src.write_text("\n".join(tokens) + "\n")
        p = subprocess.run([binary, str(src), str(semitones), "--out", str(out)], capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip())
        new = [l.strip() for l in out.read_text().splitlines()][:len(tokens)]
        keys = p.stdout.strip().splitlines()[0] if p.stdout.strip() else ""
    return new, keys


# MARK: 그리기

def _tight(ink: np.ndarray, c: Chord, pad: int = 2) -> tuple[int, int, int, int] | None:
    """상자 안 잉크에 붙은 연결 성분까지 넓힌 글자 상자 (Vision 상자는 글자 위아래를 자주 자른다)."""
    h, w = ink.shape
    py = max(pad, int(0.35 * (c.y1 - c.y0)))
    X0, Y0 = max(0, int(c.x0) - pad), max(0, int(c.y0) - py)
    X1, Y1 = min(w, int(c.x1) + pad + 1), min(h, int(c.y1) + py + 1)
    region = ink[Y0:Y1, X0:X1]
    n, labels, stats, _ = cv2.connectedComponentsWithStats(region, 8)
    core = labels[int(c.y0) - Y0:int(c.y1) - Y0 + 1, max(0, int(c.x0) - X0):int(c.x1) - X0 + 1]
    keep = set(np.unique(core)) - {0}
    if not keep:
        return None
    mask = np.isin(labels, list(keep))
    ys, xs = np.nonzero(mask)
    return X0 + int(xs.min()), Y0 + int(ys.min()), X0 + int(xs.max()) + 1, Y0 + int(ys.max()) + 1


def _fit_font(face: tuple[str, int], text: str, height: int) -> ImageFont.FreeTypeFont:
    """글자 전체(근음 대문자 + ♯ 등)의 높이가 height가 되는 크기."""
    path, index = face
    size = max(6, height)
    for _ in range(4):
        f = ImageFont.truetype(path, size, index=index)
        bb = f.getbbox(text)
        hh = max(1, bb[3] - bb[1])
        size = max(6, int(round(size * height / hh)))
    return ImageFont.truetype(path, size, index=index)


def _render_mask(font: ImageFont.FreeTypeFont, text: str) -> np.ndarray:
    bb = font.getbbox(text)
    img = Image.new("L", (bb[2] - bb[0] + 2, bb[3] - bb[1] + 2), 0)
    ImageDraw.Draw(img).text((1 - bb[0], 1 - bb[1]), text, font=font, fill=255)
    return np.array(img)


def pick_font(ink: np.ndarray, chords: list[Chord]) -> str:
    """원래 코드 글자를 후보 글꼴로 그려 잉크 모양이 가장 닮은 것 (코드 여러 개로 투표)."""
    votes: Counter = Counter()
    for c in sorted(chords, key=lambda c: -(c.x1 - c.x0))[:12]:
        t = _tight(ink, c)
        if not t or len(c.text) < 1:
            continue
        x0, y0, x1, y1 = t
        crop = ink[y0:y1, x0:x1].astype(np.float32)
        if crop.size == 0:
            continue
        best = None
        density = float(crop.mean())
        for name, face in FONTS.items():
            if not Path(face[0]).exists():
                continue
            raw = _render_mask(_fit_font(face, c.text, y1 - y0), c.text)
            nat_w = raw.shape[1]
            m = cv2.resize(raw, (x1 - x0, y1 - y0), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
            inter = float((np.minimum(m, crop)).sum())
            union = float((np.maximum(m, crop)).sum()) or 1.0
            # 폭 비율과 획 굵기(잉크 비율)도 본다. 늘려 맞춘 뒤의 IoU만으론 글꼴 폭·굵기 차이를 못 본다
            width_pen = abs(np.log(max(1, nat_w) / max(1, x1 - x0)))
            weight_pen = abs(np.log(max(1e-3, float((m > 0.5).mean())) / max(1e-3, density)))
            score = inter / union - 0.5 * width_pen - 0.6 * weight_pen
            if best is None or score > best[0]:
                best = (score, name)
        if best:
            votes[best[1]] += 1
    return votes.most_common(1)[0][0] if votes else "Arial"


def _colors(rgb: np.ndarray, ink: np.ndarray, box: tuple[int, int, int, int]) -> tuple[tuple, tuple]:
    """(배경색, 글자색). 배경 = 상자 둘레, 글자 = 상자 안 잉크 픽셀의 중앙값."""
    h, w = ink.shape
    x0, y0, x1, y1 = box
    X0, Y0, X1, Y1 = max(0, x0 - 3), max(0, y0 - 3), min(w, x1 + 3), min(h, y1 + 3)
    ring = np.ones((Y1 - Y0, X1 - X0), bool)
    ring[y0 - Y0:y1 - Y0, x0 - X0:x1 - X0] = False
    region = rgb[Y0:Y1, X0:X1]
    bg_px = region[ring & (ink[Y0:Y1, X0:X1] == 0)]
    bg = tuple(int(v) for v in np.median(bg_px, axis=0)) if len(bg_px) else (255, 255, 255)
    fg_px = rgb[y0:y1, x0:x1][ink[y0:y1, x0:x1] > 0]
    if len(fg_px):
        lum = fg_px.mean(axis=1)
        dark = fg_px[lum <= np.percentile(lum, 40)]
        fg = tuple(int(v) for v in np.median(dark, axis=0))
    else:
        fg = (0, 0, 0)
    return bg, fg


def draw(rgb: np.ndarray, ink: np.ndarray, chords: list[Chord], font_name: str, obstacles=()) -> np.ndarray:
    out = rgb.copy()
    h, w = ink.shape
    font_path = FONTS[font_name]
    boxes = []
    for c in chords:
        t = _tight(ink, c)
        boxes.append(t)
    # 같은 코드 줄의 코드는 같은 크기다. 상자가 글자 일부만 잡았으면(작은 조각) 줄의 중앙 높이를 쓴다
    def band(t):
        return round(((t[1] + t[3]) / 2) / max(4, (t[3] - t[1])))
    row_h: dict = {}
    for c, t in zip(chords, boxes):
        if t:
            row_h.setdefault((c.col, c.row if c.row is not None else ("y", band(t))), []).append(t[3] - t[1])
    all_h = [t[3] - t[1] for t in boxes if t]
    page_h = float(np.median(all_h)) if all_h else 12.0
    def height_for(c, t):
        hs = row_h.get((c.col, c.row if c.row is not None else ("y", band(t))), [t[3] - t[1]])
        ref = float(np.median(hs)) if len(hs) >= 2 else page_h
        own = t[3] - t[1]
        if (t[2] - t[0]) >= 2.5 * own:                    # 넓은 상자는 낱말 전체를 잡은 것이다: 제 높이를 믿는다 (작게 쓴 Intro 줄 등)
            return own
        return own if 0.8 * ref <= own <= 1.3 * ref else ref
    for k, (c, t) in enumerate(zip(chords, boxes)):
        if not t or not c.new or c.new == c.text:
            continue
        x0, y0, x1, y1 = t
        bg, fg = _colors(rgb, ink, t)
        # 지우기: 글자 잉크와 그 1px 둘레만 (배경 무늬·워터마크를 덜 해친다)
        m = np.zeros((h, w), np.uint8)
        m[y0:y1, x0:x1] = ink[y0:y1, x0:x1]
        m = cv2.dilate(m, np.ones((3, 3), np.uint8))
        out[m > 0] = bg
        # 쓸 수 있는 폭: 같은 줄 오른쪽 코드 직전까지 (없으면 원래 폭의 3배, 이미지 끝)
        right = [b[0] for b, cc in zip(boxes, chords) if b and cc is not c
                 and abs((b[1] + b[3]) / 2 - (y0 + y1) / 2) < (y1 - y0) and b[0] >= x1 - 1]
        right += [b[0] for b in obstacles if b[0] >= x1 - 1 and y0 - 2 <= (b[1] + b[3]) / 2 <= y1 + 2
                  and (b[3] - b[1]) < 2.5 * (y1 - y0)]                 # 같은 줄의 낱말만 (가사 줄의 키 큰 상자는 빼고)
        limit = (min(right) - 2) if right else min(w - 2, x0 + 3 * (x1 - x0) + 4 * (y1 - y0))
        hgt = int(round(height_for(c, t))) + 1
        if hgt != y1 - y0:                                 # 상자가 글자 일부였다: 아래선(글자 바닥)을 기준으로 다시 잡는다
            y0 = y1 - hgt
        font = _fit_font(font_path, c.text, hgt)           # 크기는 원래 코드 글자로 정한다 (새 코드에 같은 크기)
        mask = _render_mask(font, c.new)
        # 고른 글꼴이 원래 글자보다 넓으면(좁은 글꼴로 찍은 악보) 원래 낱말 폭에 맞게 가로로 좁힌다. 낱말 전체를 잡은 상자만 믿는다
        own_w = _render_mask(font, c.text).shape[1] - 2
        ratio = (x1 - x0) / max(1, own_w)
        if ratio < 0.92 and (x1 - x0) >= (y1 - y0):
            cond = max(0.6, ratio)
            mask = cv2.resize(mask, (max(1, int(round(mask.shape[1] * cond))), mask.shape[0]), interpolation=cv2.INTER_AREA)
        avail = int(max(x1 - x0, limit - x0))
        if mask.shape[1] > avail:                          # 자리가 모자라면 고르게 줄인다 (75%까지, 바닥선 유지)
            scale = max(0.75, avail / mask.shape[1])
            font = _fit_font(font_path, c.text, max(6, int(round(hgt * scale))))
            mask = _render_mask(font, c.new)
            y0 = y1 - max(6, int(round(hgt * scale)))
            if mask.shape[1] > avail:                      # 그래도 넘치면 가로로만 좁힌다 (60%까지). 그 뒤에는 넘친다
                cond = max(0.6, avail / mask.shape[1])
                mask = cv2.resize(mask, (max(1, int(round(mask.shape[1] * cond))), mask.shape[0]), interpolation=cv2.INTER_AREA)
        # 첫 글자 윗선을 원래 윗선에 맞춘다
        top_old = font.getbbox(c.text)[1] - font.getbbox(c.text[0])[1]   # 원래 글자에서 근음 윗선이 전체 윗선보다 얼마나 아래인가
        top_new = font.getbbox(c.new)[1] - font.getbbox(c.new[0])[1]
        py, px = y0 - 1 - top_old + top_new, x0 - 1                # 근음 대문자 윗선을 맞춘다
        mh, mw = mask.shape
        sy0, sx0 = max(0, -py), max(0, -px)
        py, px = max(0, py), max(0, px)
        mh, mw = min(mh - sy0, h - py), min(mw - sx0, w - px)
        if mh <= 0 or mw <= 0:
            continue
        a = mask[sy0:sy0 + mh, sx0:sx0 + mw].astype(np.float32)[..., None] / 255
        region = out[py:py + mh, px:px + mw].astype(np.float32)
        out[py:py + mh, px:px + mw] = (region * (1 - a) + np.array(fg, np.float32) * a).astype(np.uint8)
    return out


def review_image(rgb: np.ndarray, chords: list[Chord], missed: list[tuple], ink: np.ndarray) -> np.ndarray:
    img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    for c in chords:
        t = _tight(ink, c) or (int(c.x0), int(c.y0), int(c.x1), int(c.y1))
        color = (60, 170, 60) if c.src == "both" else (0, 140, 255)       # 초록: 텍스트와 일치, 주황: 한쪽만
        cv2.rectangle(img, (t[0] - 2, t[1] - 2), (t[2] + 2, t[3] + 2), color, 1)
    for b in missed:
        cv2.rectangle(img, (int(b[0]) - 2, int(b[1]) - 2), (int(b[2]) + 2, int(b[3]) + 2), (0, 0, 220), 2)
    return img


# MARK: 진입점

def overlay(src: Path, chart_text: str | None, semitones: int, out: Path, review_png: Path | None = None,
            font: str | None = None) -> Result:
    """원본 src 위의 코드를 semitones만큼 옮겨 out(같은 확장자 권장)에 쓴다. font: FONTS의 이름, 없으면 자동 선택."""
    rgb = load_color(src)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    chords, missed, lh = find_chords(gray, chart_text)
    r = Result()
    if not chords:
        r.notes.append("no chords found in the image")
        return r
    pieces = [p for c in chords for p in c.text.split("-")]
    moved, r.keys = _transpose_tokens(pieces, semitones)
    k = 0
    for c in chords:
        n_ = len(c.text.split("-"))
        c.new = "-".join(moved[k:k + n_])
        k += n_
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    ink = (binary < 128).astype(np.uint8)
    r.font = font if font in FONTS else pick_font(ink, chords)
    result = draw(rgb, ink, chords, r.font, getattr(find_chords, "obstacles", ()))
    for line in getattr(find_chords, "lost_lines", []):
        r.notes.append(f"chord line not found in the image: {line}")
    r.replaced = sum(len(c.text.split("-")) for c in chords if c.new)
    r.missed = len(missed)
    r.missed += len(getattr(find_chords, "lost_lines", []))
    if missed:
        r.notes.append(f"{len(missed)} spots on chord lines not rewritten (red boxes in the review image)")
    out.parent.mkdir(parents=True, exist_ok=True)
    ext = out.suffix.lower()
    if ext == ".pdf":
        import fitz
        tmp = out.with_suffix(".png")
        cv2.imencode(".png", cv2.cvtColor(result, cv2.COLOR_RGB2BGR))[1].tofile(str(tmp))
        doc = fitz.open()
        hpx, wpx = result.shape[:2]
        page = doc.new_page(width=wpx * 72 / 300, height=hpx * 72 / 300)
        page.insert_image(page.rect, filename=str(tmp))
        doc.save(out)
        doc.close()
        tmp.unlink()
    else:
        params = [cv2.IMWRITE_JPEG_QUALITY, 95] if ext in (".jpg", ".jpeg") else []
        ok, buf = cv2.imencode(ext if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff") else ".png",
                               cv2.cvtColor(result, cv2.COLOR_RGB2BGR), params)
        buf.tofile(str(out))
    r.image = out
    if review_png:
        cv2.imencode(".png", review_image(rgb, chords, missed, ink))[1].tofile(str(review_png))
        r.review = review_png
    return r
