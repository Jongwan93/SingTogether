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
    # Vision은 ♯을 i·l·I·|·!·H로 읽는다 (F#m → "Fim", E/G# → "E/GH"): 그렇게 바꾼 꼴로도 비교한다
    alt = re.sub(r"(?<=[A-G])[ilI|!tH](?=m|/|$|\d)", "#", w_)
    return max(SequenceMatcher(None, v, t).ratio() for v in {w_, alt} for t in tokens) >= 0.75


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
    pairs_text = {}                                   # "Gsus4G" → ("Gsus4", "G"), "GCG" → ("G", "C", "G"): 띄어쓰기를 잃고 붙어 읽힌 코드들
    for line in text_lines:
        for i in range(len(line)):
            for j in range(i + 2, min(len(line), i + 5) + 1):
                pairs_text.setdefault("".join(line[i:j]), tuple(line[i:j]))
    chord_units, run_units, other_boxes = [], [], []
    for wtxt, bb in words:
        sq = re.sub(r"\s+", "", wtxt.strip(_STRIP))
        if sq in pairs_text:
            # 한 단위로 둔다 ("G C G" → "Bb Eb Bb"를 한 번에 씀): 글자 비례로 가르면 한 글자 자리에 두 글자 코드를 아주 작게 쓰게 된다
            chord_units.append(Chord(*bb, " ".join(pairs_text[sq]), "vision", col(bb[0])))
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
            widths = [max(1, len(u.text.split())) for u in units]       # "G C G" 단위는 토큰 셋
            if sum(widths) == len(toks):
                k = 0
                for u, n_ in zip(units, widths):
                    u.text, u.src = " ".join(toks[k:k + n_]), ("both" if u.text else "text")
                    k += n_
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

_SEP = re.compile(r"([- ])")


def _pieces(text: str) -> list[str]:
    """코드 단위의 조각: 붙임표 진행("Eb-F")과 붙어 읽힌 코드들("G C G")을 코드 하나씩으로."""
    return [p for p in _SEP.split(text) if p and p not in ("-", " ")]


def _rejoin(text: str, moved: list[str]) -> str:
    """조각을 옮긴 뒤 원래 구분자(-, 공백)대로 다시 잇는다."""
    out, k = [], 0
    for part in _SEP.split(text):
        if part in ("-", " "):
            out.append(part)
        elif part:
            out.append(moved[k])
            k += 1
    return "".join(out)


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
    # Vision 상자는 글자 위를 절반 가까이 자르기도 한다 (D의 윗획이 14px 밖) — 위로는 넉넉히. 아래(바닥선)는 맞는 편이고,
    # 넓히면 바로 밑의 빔에 닿아 글자 상자가 빔까지 자란다 — 조금만
    py_top, py_bot = max(pad, int(0.7 * (c.y1 - c.y0))), max(pad, int(0.25 * (c.y1 - c.y0)))
    # 옆으로도 글자 높이만큼 본다: Vision 상자가 근음 글자를 반쯤 자르기도 한다 (C♯의 C 왼쪽 2/3이 밖). 상자 안에 폭의 15% 넘게
    # 들어온 성분만 받는다 (이웃 코드 글자가 1–2px 걸친 것은 빼고)
    px = max(pad, int(0.8 * (c.y1 - c.y0)))
    X0, Y0 = max(0, int(c.x0) - px), max(0, int(c.y0) - py_top)
    X1, Y1 = min(w, int(c.x1) + px + 1), min(h, int(c.y1) + py_bot + 1)
    region = ink[Y0:Y1, X0:X1]
    n, labels, stats, _ = cv2.connectedComponentsWithStats(region, 8)
    cx0, cx1 = max(0, int(c.x0) - pad - X0), int(c.x1) + pad - X0 + 1
    core = labels[int(c.y0) - Y0:int(c.y1) - Y0 + 1, cx0:cx1]
    keep, wide = set(), set()
    for i in set(np.unique(core)) - {0}:
        x, _, cw, _, _ = stats[i]
        if min(x + cw, cx1) - max(x, cx0) < 0.15 * cw:
            continue
        # 글자 아래쪽에 끝만 들어온 빔 토막(보혈을 지나 Em: 상자가 왼쪽 빔까지 자라 새 F♯m이 43px 왼쪽 빔 위에 써졌다)은 뺀다.
        # 상자 가운데보다 아래에서 시작하고, 상자 열 밖이 더 넓거나 잉크 대부분이 상자 밑이다. 옆이 잘린 글자(C♯의 C)는 위에서 시작한다
        _, sy, _, sh, area = stats[i]
        if sy > (c.y0 + c.y1) / 2 - Y0 and cw <= 1.5 * (cx1 - cx0) + 4:   # 넓은 덩어리는 아래에서 상자 열 안쪽만 쓴다
            rows_in = (labels[max(0, int(c.y0) - Y0):int(c.y1) - Y0 + 1] == i).sum()
            if min(x + cw, cx1) - max(x, cx0) < 0.5 * cw or rows_in < 0.3 * area:
                continue
        # 거꾸로, 상자 위에 얹힌 다른 글(이 세상 마지막 줄: A♭ 위의 작은 "Coda")은 잉크 대부분이 상자 위에 있고 밑끝만 닿는다.
        # 윗첨자(E⁷·Esus⁴의 숫자)도 위에 있지만 늘 오른쪽 끝이라, 상자 왼쪽 55% 위에 있는 것만 뺀다
        if (sy + sh < int(c.y0) - Y0 + 0.3 * (c.y1 - c.y0) and cw <= 1.5 * (cx1 - cx0) + 4
                and x + cw / 2 < cx0 + 0.55 * (cx1 - cx0)):
            rows_in = (labels[max(0, int(c.y0) - Y0):int(c.y1) - Y0 + 1] == i).sum()
            if rows_in < 0.25 * area:
                continue
        # 상자보다 훨씬 넓은 성분은 글자가 아니라 밑의 빔·기둥 묶음(글자가 거기 붙기도 한다): 원래 상자 열 안쪽만 쓴다
        # (통째로 넣으면 상자가 옆 글자(1.3. 괄호)까지 자라고, 빼면 빔에 붙은 /D가 사라진다)
        (wide if cw > 1.5 * (cx1 - cx0) + 4 else keep).add(i)
    if not keep and not wide:
        return None
    mask = np.isin(labels, list(keep))
    if wide:
        mask[:, cx0:cx1] |= np.isin(labels[:, cx0:cx1], list(wide))
    ys, xs = np.nonzero(mask)
    return X0 + int(xs.min()), Y0 + int(ys.min()), X0 + int(xs.max()) + 1, Y0 + int(ys.max()) + 1


def _longest_run(row: np.ndarray) -> int:
    """한 행에서 가장 긴 연속 잉크 길이."""
    if not row.any():
        return 0
    d = np.diff(np.r_[0, row.astype(np.int8), 0])
    return int((np.flatnonzero(d == -1) - np.flatnonzero(d == 1)).max())


def _root_letter(ink: np.ndarray, t: tuple[int, int, int, int], cap: float | None = None) -> tuple[tuple[int, int, int, int], bool] | None:
    """상자 안 근음 대문자의 상자와, 나머지 글자가 윗첨자처럼 작은지(근음의 80% 아래): (상자, 작음). 가장 키 큰 덩어리가
    근음 (같으면 왼쪽). 상자 높이의 30% 안 되는 조각(점·찌꺼기)은 뺀다.
    cap: 쪽의 대문자 키(알면). 근음이 그보다 25% 넘게 크면 곡선·빔이 글자 위나 아래에 붙은 것이다: 대문자 키보다 긴 가로 획이 있는
    행이 아래쪽에만 있으면 위 끝에서, 위쪽에만 있으면 아래 끝에서 cap만큼만 글자로 본다."""
    x0, y0, x1, y1 = t
    n, _, stats, _ = cv2.connectedComponentsWithStats(ink[y0:y1, x0:x1].astype(np.uint8), 8)
    comps = [stats[i] for i in range(1, n) if stats[i][3] >= 0.3 * (y1 - y0)]
    if not comps:
        return None
    s = max(comps, key=lambda s: (s[3], -s[0]))
    # 근음 글자에 빔·이음줄 끝이 붙었으면(모든 능력: A의 왼발이 빔 끝에 닿아 새 B가 빔 위에 써졌다) 양 끝의 납작한 열(글자 키의
    # 40% 아래; 흐린 그림의 빔은 글자 키의 30%만큼 굵다)이 글자 키의 30% 넘게 이어지는 만큼 잘라 낸다. 글자의 발·세리프는 그보다 좁다
    lab_ = cv2.connectedComponentsWithStats(ink[y0:y1, x0:x1].astype(np.uint8), 8)[1]
    comp = lab_[s[1]:s[1] + s[3], s[0]:s[0] + s[2]] == next(i for i in range(1, n) if (stats[i] == s).all())
    ext = np.array([(np.flatnonzero(c)[-1] - np.flatnonzero(c)[0] + 1) if c.any() else 0 for c in comp.T])
    flat = ext < 0.4 * s[3]
    a, b = 0, len(ext)
    while a < b and flat[a]:
        a += 1
    while b > a and flat[b - 1]:
        b -= 1
    root = s
    if b - a >= 0.3 * s[3] and (a > 0.3 * s[3] or len(ext) - b > 0.3 * s[3]):    # 전부 납작하면(낱말 밑줄 등) 그대로
        a, b = (a if a > 0.3 * s[3] else 0), (b if len(ext) - b > 0.3 * s[3] else len(ext))
        comp = comp[:, a:b]
        rows = np.flatnonzero(comp.any(axis=1))
        comp = comp[rows[0]:rows[-1] + 1]
        s = np.array([s[0] + a, s[1] + rows[0], b - a, rows[-1] - rows[0] + 1, s[4]])
    # 이음줄이 글자 밑에 닿은 E♭ (이 세상의 부요함보다 끝 줄): 바닥을 이음줄 밑에 맞추면 새 글자가 원래보다 18px 내려앉아 이음줄에 겹쳤다
    if cap and s[3] > 1.25 * cap:
        wide = np.flatnonzero([_longest_run(r) > 1.1 * cap for r in comp])
        if wide.size and wide[0] > 0.8 * cap:
            s = np.array([s[0], s[1], s[2], int(round(cap)), s[4]])
        elif wide.size and wide[-1] < s[3] - 0.8 * cap:
            s = np.array([s[0], s[1] + s[3] - int(round(cap)), s[2], int(round(cap)), s[4]])
    others = [c for c in comps if c is not root and c[0] > s[0] + 0.5 * s[2]]
    # 윗첨자 = 근음보다 작고 **바닥이 떠 있는** 것 (근음 바닥보다 0.2cap 위). 소문자 m·sus는 작아도 바닥이 같다 (Em은 윗첨자가 아니다)
    # 하나라도 그러면 윗첨자 악보다: Gm⁷·C⁴sus 처럼 m·sus는 바닥에 있고 숫자만 올린 코드가 흔하다 (전부로 재면 그런 코드가 반대표가 된다)
    small = any(c[3] <= 0.8 * s[3] and c[1] + c[3] <= s[1] + s[3] - 0.2 * s[3] for c in others)
    return (x0 + int(s[0]), y0 + int(s[1]), x0 + int(s[0] + s[2]), y0 + int(s[1] + s[3])), small


def _chord_mask(font_path: tuple[str, int], text: str, cap: int, sup: bool) -> tuple[np.ndarray, int, int]:
    """코드 글자 마스크: (마스크, 마스크 안에서 근음 대문자 윗줄의 행, 대문자 높이). sup이면 근음(글자+♯♭) 뒤를 윗첨자처럼
    62% 크기로 대문자 윗줄에 맞춰 쓴다 (E^sus4, A^/C♯ 식의 악보)."""
    m = re.match(r"([A-G][#b]?)(.*)", text)
    root, rest = (m.group(1), m.group(2)) if m else (text, "")
    font = _fit_font(font_path, root[0], cap)
    bb_cap = font.getbbox(root[0])
    cap_h = bb_cap[3] - bb_cap[1]
    if not rest or not sup:
        mask = _render_mask(font, text)
        return mask, bb_cap[1] - font.getbbox(text)[1] + 1, cap_h
    rm = _render_mask(font, root)
    r_top = bb_cap[1] - font.getbbox(root)[1] + 1
    sfont = _fit_font(font_path, "A", max(8, int(round(cap * 0.62))))
    gap = max(1, int(round(0.06 * cap)))
    # 찬양 악보의 관례: 숫자는 대문자 윗줄에 올려 쓰고(C⁷, Esus⁴), 소문자·슬래시(m·sus·/F♯)는 작게 바닥에 둔다 (Gm⁷, C⁴sus, Dm).
    # 조각마다 따로 놓는다: 올리는 조각은 윗줄, 나머지는 대문자 바닥
    pieces = [(t, bool(re.match(r"[0-9]", t))) for t in re.findall(r"[0-9]+|[^0-9]+", rest)]
    masks = [(_render_mask(sfont, t), up) for t, up in pieces]
    W = rm.shape[1] + gap + sum(m.shape[1] for m, _ in masks)
    base = r_top + cap_h                                           # 대문자 바닥 행
    H = max([rm.shape[0], base + 1] + [r_top + m.shape[0] for m, up in masks if up] + [base + 1 for m, up in masks if not up])
    out = np.zeros((H, W), np.uint8)
    out[:rm.shape[0], :rm.shape[1]] = rm
    x = rm.shape[1] + gap
    for m, up in masks:
        y = r_top if up else max(0, base + 1 - m.shape[0])
        out[y:y + m.shape[0], x:x + m.shape[1]] = np.maximum(out[y:y + m.shape[0], x:x + m.shape[1]], m)
        x += m.shape[1]
    return out, r_top, cap_h


def _fit_font(face: tuple[str, int], text: str, height: int) -> ImageFont.FreeTypeFont:
    """글자 전체(근음 대문자 + ♯ 등)의 높이가 height가 되는 크기."""
    path, index = face
    # 8pt 아래에서는 Helvetica.ttc 같은 글꼴이 FreeType 안에서 0으로 나눈다 (7px짜리 Vision 상자: because he lives)
    size = max(8, height)
    for _ in range(4):
        f = ImageFont.truetype(path, size, index=index)
        bb = f.getbbox(text)
        hh = max(1, bb[3] - bb[1])
        size = max(8, int(round(size * height / hh)))
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
    # 둘레는 상자에서 4–10px 떨어진 띠: 잉크 덩어리에 딱 맞춘 상자(AI만 읽은 코드)의 바로 둘레는 번진 회색 테라 상자가 회색으로
    # 칠해진다. 중앙값 대신 밝은 쪽 75% 값
    X0, Y0, X1, Y1 = max(0, x0 - 10), max(0, y0 - 10), min(w, x1 + 10), min(h, y1 + 10)
    ring = np.ones((Y1 - Y0, X1 - X0), bool)
    ring[max(0, y0 - 4 - Y0):y1 + 4 - Y0, max(0, x0 - 4 - X0):x1 + 4 - X0] = False
    region = rgb[Y0:Y1, X0:X1]
    bg_px = region[ring & (ink[Y0:Y1, X0:X1] == 0)]
    bg = tuple(int(v) for v in np.percentile(bg_px, 75, axis=0)) if len(bg_px) else (255, 255, 255)
    fg_px = rgb[y0:y1, x0:x1][ink[y0:y1, x0:x1] > 0]
    if len(fg_px):
        lum = fg_px.mean(axis=1)
        dark = fg_px[lum <= np.percentile(lum, 40)]
        fg = tuple(int(v) for v in np.median(dark, axis=0))
    else:
        fg = (0, 0, 0)
    return bg, fg


LOG: list[tuple] = []      # draw가 코드마다 (원래, 새 글자, 상자, 오른쪽 한계, 새 글자 폭, 쓸 수 있는 폭, 높이)를 남긴다 (디버깅용)


def draw(rgb: np.ndarray, ink: np.ndarray, chords: list[Chord], font_name: str, obstacles=(),
         uniform: bool = False, erase_box: bool = False) -> np.ndarray:
    """uniform: 모든 코드를 쪽 전체의 중앙값 높이로 쓴다 (오선 악보의 코드는 크기가 같다; 슬래시·윗첨자 상자가 커도 따라가지 않게).
    erase_box: 글자 잉크만이 아니라 상자를 통째로 지운다 (흐린 그림은 잉크 둘레에 회색 테가 남아 옛 글자가 비친다)."""
    out = rgb.copy()
    h, w = ink.shape
    font_path = FONTS[font_name]
    LOG.clear()
    draw.erased = []                                       # erase_box로 통째로 지운 상자들 (부르는 쪽이 상자에 휩쓸린 오선·머리를 되살린다)
    boxes = []
    for c in chords:
        t = _tight(ink, c)
        boxes.append(t)
    # 같은 줄의 이웃 상자에 걸친 상자는 이웃 앞에서 자른다: 넓힌 상자가 이웃 글자의 첫 획을 삼키면(C♯sus4/G♯이 C♯의 C를) 새 글자가
    # 이웃 위까지 써지고, 이웃은 오른쪽 한계로도 안 잡힌다
    for k, a in enumerate(boxes):
        if not a:
            continue
        for b in boxes:
            if b and b is not a and a[0] < b[0] < a[2] and b[2] > a[2] and min(a[3], b[3]) - max(a[1], b[1]) > 0.5 * (a[3] - a[1]):
                boxes[k] = a = (a[0], a[1], max(a[0] + 1, b[0] - 1), a[3])
    # 같은 코드 줄의 코드는 같은 크기다. 상자가 글자 일부만 잡았으면(작은 조각) 줄의 중앙 높이를 쓴다
    def band(t):
        return round(((t[1] + t[3]) / 2) / max(4, (t[3] - t[1])))
    row_h: dict = {}
    for c, t in zip(chords, boxes):
        if t:
            row_h.setdefault((c.col, c.row if c.row is not None else ("y", band(t))), []).append(t[3] - t[1])
    all_h = [t[3] - t[1] for t in boxes if t]
    page_h = float(np.median(all_h)) if all_h else 12.0
    # 오선 악보(uniform): 상자 높이는 윗첨자(A♭의 ♭, C²의 ²)가 올라간 만큼 근음 글자보다 크다. 거기에 "Ab" 전체를 맞추면 글자가
    # 1.3배쯤 커지고, 이웃이 가까운 코드만 다시 줄어 크기가 들쭉날쭉했다. 근음 대문자(상자 안 가장 키 큰 덩어리)의 높이에 맞춘다
    parts = [_root_letter(ink, t) if (uniform and t) else None for t in boxes]
    roots = [p[0] if p else None for p in parts]
    cap_hs = [r[3] - r[1] for r in roots if r]
    page_cap = float(np.median(cap_hs)) if cap_hs else page_h
    # 곡선이 붙어 대문자 키보다 큰 근음은 쪽 대문자 키로 다시 잰다
    parts = [_root_letter(ink, t, page_cap) if (p and p[0][3] - p[0][1] > 1.25 * page_cap) else p for p, t in zip(parts, boxes)]
    roots = [p[0] if p else None for p in parts]
    # 근음 뒤 글자(sus4, 7, /C♯)가 윗첨자처럼 작은 악보인가: 글자가 둘 이상인 코드들의 다수결
    # 표는 근음 뒤에 숫자·♯♭이 있는 코드만 낸다 (C⁷·B♭²·Esus⁴): 슬래시·m만 있는 코드(E/D·Dm)는 윗첨자 악보에서도 바닥에 쓰여 반대표가 된다
    # (슬래시 베이스의 ♯도 바닥에 쓰는 악보가 있어 숫자만 센다)
    digit = re.compile(r"^[A-G][#b]?.*[0-9]")
    sup_votes = [p[1] for p, c in zip(parts, chords) if p and digit.search(c.text)]
    sup_style = bool(sup_votes) and sum(sup_votes) * 2 > len(sup_votes)
    LOG.append(("sup", sup_style, [(c.text, p[1]) for p, c in zip(parts, chords) if p and digit.search(c.text)]))
    def height_for(c, t):
        if uniform:
            return page_h
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
        # 지우기: 글자 잉크와 그 1px 둘레만 (배경 무늬·워터마크를 덜 해친다). erase_box면 상자째
        m = np.zeros((h, w), np.uint8)
        if erase_box:
            m[max(0, y0 - 2):min(h, y1 + 2), max(0, x0 - 2):min(w, x1 + 2)] = 1
            draw.erased.append((max(0, x0 - 2), max(0, y0 - 2), min(w, x1 + 2), min(h, y1 + 2)))
        else:
            m[y0:y1, x0:x1] = ink[y0:y1, x0:x1]
            m = cv2.dilate(m, np.ones((3, 3), np.uint8))
        out[m > 0] = bg
        # 쓸 수 있는 폭: 같은 줄 오른쪽 코드 직전까지 (없으면 원래 폭의 3배, 이미지 끝)
        # 상자가 조금 겹치는 이웃(C♯sus4/G♯ 상자가 C♯에 8px 걸침)도 오른쪽 이웃이다 — 빼면 새 글자가 이웃 위로 넘친다
        right = [b[0] for b, cc in zip(boxes, chords) if b and cc is not c
                 and abs((b[1] + b[3]) / 2 - (y0 + y1) / 2) < (y1 - y0) and b[0] >= x1 - 0.3 * (y1 - y0) and b[2] > x1]
        right += [b[0] for b in obstacles if b[0] >= x1 - 1 and y0 - 2 <= (b[1] + b[3]) / 2 <= y1 + 2
                  and (b[3] - b[1]) < 2.5 * (y1 - y0)]                 # 같은 줄의 낱말만 (가사 줄의 키 큰 상자는 빼고)
        # 오른쪽 바로 옆의 작은 기호(늘임표·숫자 등: 대문자 키의 2배 폭, 1.6배 키 안)도 한계다 (주 이름 찬양: B를 Db로 바꾸니
        # b가 늘임표 위에 겹쳤다). 긴 이음줄·기둥은 글자 뒤로 지나가는 것이라 빼고, 상자에서 대문자 키의 1.5배 안만 본다
        if uniform:
            cap_ = max(6.0, page_cap)
            sx0, sx1 = x1 + 1, min(w, int(x1 + 1.5 * cap_))
            if sx1 > sx0:
                n_, _, st_, _ = cv2.connectedComponentsWithStats(ink[y0:y1, sx0:sx1].astype(np.uint8), 8)
                for i_ in range(1, n_):
                    bx_, by_, bw_, bh_, ba_ = st_[i_]
                    if ba_ < 0.05 * cap_ * cap_ or bx_ + bw_ >= sx1 - sx0:      # 창 밖으로 이어지는 것(긴 곡선)은 빼고
                        continue
                    wy0_, wy1_ = max(0, y0 - int(cap_)), min(h, y1 + int(cap_))
                    _, flab, fst, _ = cv2.connectedComponentsWithStats(ink[wy0_:wy1_, sx0 + bx_:sx0 + bx_ + bw_].astype(np.uint8), 8)
                    ids_ = np.unique(flab[y0 - wy0_ + by_:y0 - wy0_ + by_ + bh_][flab[y0 - wy0_ + by_:y0 - wy0_ + by_ + bh_] > 0])
                    # 그 조각을 위아래로 넓힌 창에서 다시 재서: 창 위·아래 끝에 닿으면 기둥·꼬리다 (주님 다시 오실때까지: 8분음표
                    # 꼬리 때문에 G가 작아졌다)
                    if ids_.size and bw_ <= 2 * cap_ and all(fst[j][3] <= 1.6 * cap_ and fst[j][1] > 0
                                                             and fst[j][1] + fst[j][3] < wy1_ - wy0_ for j in ids_):
                        right.append(sx0 + bx_)
        # 오른쪽 코드 앞에 여백을 둔다 (오선 악보: 대문자 높이의 0.3; 붙여 쓰면 Bbm7/Ab와 Ab가 한 낱말로 읽힌다)
        gap = max(2, int(round(0.3 * page_cap))) if uniform else 2
        limit = (min(right) - gap) if right else min(w - 2, x0 + 3 * (x1 - x0) + 4 * (y1 - y0))
        # 원래 폭을 바닥으로 두되, 오른쪽 이웃 상자와 겹치면(Vision 상자가 이웃 글자를 삼킨 경우) 이웃 앞까지만
        avail = int(max(x1 - x0, limit - x0)) if not right else int(max(limit - x0, 0.5 * (x1 - x0)))
        if roots[k]:
            # 근음 대문자 높이(쪽 중앙값)에 글꼴을 맞추고, 새 코드의 대문자 바닥을 원래 대문자 바닥에 놓는다
            rx0, ry0, rx1, ry1 = roots[k]
            cap = max(6, int(round(page_cap)))
            mask, r_top, cap_h = _chord_mask(font_path, c.new, cap, sup_style)
            own_w = _render_mask(_fit_font(font_path, c.text[0], cap), c.text[0]).shape[1] - 2   # 글꼴이 원래 글자보다 넓으면 근음 글자 폭에 맞게 좁힌다
            ratio = (rx1 - rx0) / max(1, own_w)
            if ratio < 0.92:
                cond = max(0.75, ratio)
                mask = cv2.resize(mask, (max(1, int(round(mask.shape[1] * cond))), mask.shape[0]), interpolation=cv2.INTER_AREA)
            LOG.append((c.text, c.new, (x0, y0, x1, y1), limit, mask.shape[1], avail, cap))
            if mask.shape[1] > avail:                      # 자리가 모자라면 고르게 줄인다 (75%까지), 그래도 넘치면 가로로만 (60%까지)
                cap = max(6, int(round(cap * max(0.75, avail / mask.shape[1]))))
                mask, r_top, cap_h = _chord_mask(font_path, c.new, cap, sup_style)
                if mask.shape[1] > avail:
                    cond = max(0.6, avail / mask.shape[1])
                    mask = cv2.resize(mask, (max(1, int(round(mask.shape[1] * cond))), mask.shape[0]), interpolation=cv2.INTER_AREA)
            py, px = ry1 - cap_h - r_top, x0 - 1           # 근음 대문자 바닥을 원래 바닥에
            # 상자가 근음 왼쪽의 이음줄·빔 끝까지 품었으면(근음 왼쪽에 근음 키의 60% 넘는 덩어리가 없다) 근음 왼쪽 끝에 쓴다
            # (이 세상: B♭m⁷ 상자가 왼쪽 이음줄까지 넓어 새 Cm⁷이 이음줄 위에 2il 왼쪽으로 써졌다)
            if rx0 - x0 > 0.3 * (ry1 - ry0):
                n_, _, st_, _ = cv2.connectedComponentsWithStats(ink[y0:y1, x0:rx0].astype(np.uint8), 8)
                if not any(st_[i][3] >= 0.6 * (ry1 - ry0) for i in range(1, n_)):
                    px = rx0 - 1
        else:
            hgt = int(round(height_for(c, t))) + 1
            if hgt != y1 - y0:                             # 상자가 글자 일부였다: 아래선(글자 바닥)을 기준으로 다시 잡는다
                y0 = y1 - hgt
            font = _fit_font(font_path, c.text, hgt)       # 크기는 원래 코드 글자로 정한다 (새 코드에 같은 크기)
            mask = _render_mask(font, c.new)
            # 고른 글꼴이 원래 글자보다 넓으면(좁은 글꼴로 찍은 악보) 원래 낱말 폭에 맞게 가로로 좁힌다. 낱말 전체를 잡은 상자만 믿는다
            own_w = _render_mask(font, c.text).shape[1] - 2
            ratio = (x1 - x0) / max(1, own_w)
            if ratio < 0.92 and (x1 - x0) >= (y1 - y0):
                cond = max(0.6, ratio)
                mask = cv2.resize(mask, (max(1, int(round(mask.shape[1] * cond))), mask.shape[0]), interpolation=cv2.INTER_AREA)
            LOG.append((c.text, c.new, (x0, y0, x1, y1), limit, mask.shape[1], avail, hgt))
            if mask.shape[1] > avail:                      # 자리가 모자라면 고르게 줄인다 (75%까지, 바닥선 유지)
                scale = max(0.75, avail / mask.shape[1])
                font = _fit_font(font_path, c.text, max(6, int(round(hgt * scale))))
                mask = _render_mask(font, c.new)
                y0 = y1 - max(6, int(round(hgt * scale)))
                if mask.shape[1] > avail:                  # 그래도 넘치면 가로로만 좁힌다 (50%까지). 그 뒤에는 넘친다
                    cond = max(0.5, avail / mask.shape[1])
                    mask = cv2.resize(mask, (max(1, int(round(mask.shape[1] * cond))), mask.shape[0]), interpolation=cv2.INTER_AREA)
            # 첫 글자 윗선을 원래 윗선에 맞춘다
            top_old = font.getbbox(c.text)[1] - font.getbbox(c.text[0])[1]   # 원래 글자에서 근음 윗선이 전체 윗선보다 얼마나 아래인가
            top_new = font.getbbox(c.new)[1] - font.getbbox(c.new[0])[1]
            py, px = y0 - 1 - top_old + top_new, x0 - 1            # 근음 대문자 윗선을 맞춘다
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
    pieces = [p for c in chords for p in _pieces(c.text)]
    moved, r.keys = _transpose_tokens(pieces, semitones)
    k = 0
    for c in chords:
        n_ = len(_pieces(c.text))
        c.new = _rejoin(c.text, moved[k:k + n_])
        k += n_
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    ink = (binary < 128).astype(np.uint8)
    r.font = font if font in FONTS else pick_font(ink, chords)
    result = draw(rgb, ink, chords, r.font, getattr(find_chords, "obstacles", ()))
    for line in getattr(find_chords, "lost_lines", []):
        r.notes.append(f"chord line not found in the image: {line}")
    r.replaced = sum(len(_pieces(c.text)) for c in chords if c.new)
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
