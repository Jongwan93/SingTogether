"""M2 가사 인식: Apple Vision(한국어+영어)으로 가사 띠를 읽고, 음절을 x좌표로 음표에 정렬한다.

- 띠 안의 글줄을 y로 묶어 절(verse)을 나눈다 (1절, 2절 또는 한글/영어 줄).
- 한글은 글자 하나가 음절 하나. 영어는 단어를 붙임표('-')로 나눈 조각이 음절.
- '-', '_', '—' 는 멜리스마(이어 부르기) 표시라 음절이 아니다.
- 음절 ↔ 음표는 순서를 지키며 |x 차이| 합이 최소가 되게 DP로 짝짓는다.
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
from .model import Transcription
from .text import is_chord, normalize_chord

_SKIP = set("-_—–~.·,'\"`‘’“”•:;|")
_HANGUL = re.compile(r"[가-힣]")


@dataclass
class Syllable:
    text: str
    x: float          # 글자 중심 (페이지 픽셀)
    y: float
    conf: float
    src: str = "vision"   # "both" / "vision" / "claude"


def recognize_lines(gray: np.ndarray, langs=("ko-KR", "en-US"), correction: bool = True):
    """Vision 글줄 인식. (문자열, 확신도, 글자별 (x0,y0,x1,y1) 또는 None) 목록. 좌표는 gray 기준."""
    import Vision
    from Foundation import NSData, NSMakeRange

    h, w = gray.shape
    png = cv2.imencode(".png", gray)[1].tobytes()
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(NSData.dataWithBytes_length_(png, len(png)), {})
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(list(langs))
    req.setUsesLanguageCorrection_(correction)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(f"Vision 실패: {err}")
    out = []
    for obs in req.results() or []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        cand = cands[0]
        s = str(cand.string())
        boxes = []
        for i in range(len(s)):
            r, _ = cand.boundingBoxForRange_error_(NSMakeRange(i, 1), None)
            if r is None:
                boxes.append(None)
                continue
            b = r.boundingBox()
            boxes.append((b.origin.x * w, (1 - b.origin.y - b.size.height) * h, (b.origin.x + b.size.width) * w, (1 - b.origin.y) * h))
        bb = obs.boundingBox()
        out.append((s, float(cand.confidence()), boxes, (bb.origin.x * w, (1 - bb.origin.y - bb.size.height) * h,
                                                          (bb.origin.x + bb.size.width) * w, (1 - bb.origin.y) * h)))
    return out


_MELISMA = set("-_—–~")


def syllables_of(line: str, conf: float, boxes: list, dx: float = 0, dy: float = 0) -> list[Syllable]:
    """글줄 → 음절. 한글은 글자마다, 영어는 붙임표로 나뉜 조각마다 하나.
    '-'·'_' 같은 멜리스마 표시는 글자 없는 자리 표시자(text='')로 남겨 음표 하나를 차지하게 한다 ("에 -서-")."""
    out = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch in _MELISMA:
            j = i
            while j < len(line) and line[j] in _MELISMA:        # "---" 는 하나로
                j += 1
            bs = [b for b in boxes[i:j] if b]
            if bs:
                out.append(Syllable("", (min(b[0] for b in bs) + max(b[2] for b in bs)) / 2 + dx,
                                    (min(b[1] for b in bs) + max(b[3] for b in bs)) / 2 + dy, conf))
            i = j
            continue
        if ch.isspace() or ch in _SKIP:
            i += 1
            continue
        if _HANGUL.match(ch) or not ch.isalpha():
            j = i + 1
        else:                                    # 영어: 붙임표·공백 전까지 한 조각
            j = i
            while j < len(line) and line[j].isalpha():
                j += 1
        bs = [b for b in boxes[i:j] if b]
        if bs:
            x = (min(b[0] for b in bs) + max(b[2] for b in bs)) / 2
            y = (min(b[1] for b in bs) + max(b[3] for b in bs)) / 2
            out.append(Syllable(line[i:j], x + dx, y + dy, conf))
        i = j
    return out


def read_lyrics(page: Page, systems: list[System], debug_dir: Path | None = None) -> list[list[list[Syllable]]]:
    """오선마다 [절][음절]. 절은 글줄의 y로 묶는다 (위에서 아래로 1절, 2절…)."""
    il = page.interline
    result = []
    overlay = debug.canvas(page.gray) if debug_dir else None
    for k, sys_ in enumerate(systems):
        y0, y1 = sys_.lyric_band
        band = page.gray[y0:y1]
        next_top = systems[k + 1].staff.top if k + 1 < len(systems) else None
        rows: list[tuple[float, list[Syllable]]] = []
        if band.size and band.shape[0] >= 4:
            for s, conf, boxes, bb in recognize_lines(band):
                cy = (bb[1] + bb[3]) / 2 + y0
                if next_top is not None and cy > next_top - 3.5 * il and _looks_like_chords(s):
                    continue                     # 다음 오선의 코드 줄
                sylls = syllables_of(s, conf, boxes, dy=y0)
                if not any(x.text for x in sylls):            # 멜리스마 표시만 있는 줄은 가사가 아니다
                    continue
                for row in rows:                 # 같은 줄(높이 차 < 1.2il)이면 합친다
                    if abs(row[0] - cy) < 1.2 * il:
                        row[1].extend(sylls)
                        break
                else:
                    rows.append((cy, sylls))
        rows.sort(key=lambda r: r[0])
        verses = [sorted(r[1], key=lambda s: s.x) for r in rows if _is_lyric_row(r[1], r[0], next_top, il)]
        result.append(verses)
        if overlay is not None:
            debug.band(overlay, y0, y1, "lyric")
            for v, sylls in enumerate(verses):
                debug.boxes(overlay, [(s.x - 0.4 * il, s.y - 0.5 * il, s.x + 0.4 * il, s.y + 0.5 * il) for s in sylls], "lyric")
                debug.text(overlay, [((s.x - 0.4 * il, s.y - 0.6 * il), s.text or "·") for s in sylls], "lyric", size=11)
    if overlay is not None:
        total = sum(len(s) for vs in result for s in vs)
        debug.caption(overlay, f"{page.name}  lyric syllables {total}  verses/staff {[len(v) for v in result]}")
        debug.save(debug_dir / f"{page.name}-4-lyrics.png", overlay)
    return result


def _is_lyric_row(sylls: list[Syllable], cy: float, next_top: float | None, il: float) -> bool:
    """가사 줄 판정. 한글이 2자 이상이면 가사. 아니면 영어 조각이 3개 이상이고 다음 오선 코드 자리(3.5il 위)가 아닐 때만."""
    hangul = sum(1 for s in sylls if s.text and _HANGUL.search(s.text))
    if hangul >= 2:
        return True
    words = sum(1 for s in sylls if s.text and s.text.isalpha())
    near_next = next_top is not None and cy > next_top - 3.5 * il
    return words >= 3 and not near_next


def _looks_like_chords(line: str) -> bool:
    """글줄의 토큰 절반 이상이 코드 기호면 코드 줄. 한글이 있으면 가사."""
    if _HANGUL.search(line):
        return False
    tokens = [t for t in re.split(r"\s+", line.strip()) if t and t not in _SKIP]
    if not tokens:
        return False
    hits = sum(1 for t in tokens if is_chord(normalize_chord(t)))
    return hits >= 0.5 * len(tokens)


def attach_lyrics(t: Transcription, lyrics: list[list[list[Syllable]]], interline: float) -> None:
    """절마다 음절을 그 오선의 음표(쉼표 제외, x 있음)에 순서대로 짝짓는다. 음표 수 > 음절 수가 보통이라 음표 쪽에만 빈칸 허용."""
    for si, verses in enumerate(lyrics):
        notes = [n for m in t.measures if m.system == si for n in m.notes if not n.is_rest and n.x is not None]
        if not notes:
            continue
        for v, sylls in enumerate(verses):
            if not sylls:
                continue
            # 글자 있는 음절을 건너뛰면 비싸고(2il), 자리 표시자는 싸게(0.3il) 건너뛴다: 자리가 모자라면 표시자가 양보
            pairs = _align([s.x for s in sylls], [n.x for n in notes],
                           gap=[2.0 * interline if s.text else 0.3 * interline for s in sylls])
            for si_, ni in pairs:
                n = notes[ni]
                while len(n.lyrics) <= v:
                    n.lyrics.append("")
                if sylls[si_].text:
                    n.lyrics[v] = sylls[si_].text
                    if v == 0:
                        n.lyric_conf, n.lyric_src = sylls[si_].conf, sylls[si_].src
            if any(s.text for s in sylls):
                t.has_lyrics = True


def _align(xs: list[float], ns: list[float], gap: float | list[float]) -> list[tuple[int, int]]:
    """순서를 지키는 1:1 짝짓기 (음절 i → 음표 j). 비용 = |x 차이|, 음표를 건너뛰면 0, 음절 i를 건너뛰면 gap[i]."""
    I, J = len(xs), len(ns)
    gaps = gap if isinstance(gap, list) else [gap] * I
    INF = float("inf")
    cost = [[INF] * (J + 1) for _ in range(I + 1)]
    back = [[None] * (J + 1) for _ in range(I + 1)]
    cost[0][0] = 0
    for i in range(I + 1):
        for j in range(J + 1):
            if cost[i][j] == INF:
                continue
            if i < I and j < J:                   # 짝
                c = cost[i][j] + abs(xs[i] - ns[j])
                if c < cost[i + 1][j + 1]:
                    cost[i + 1][j + 1], back[i + 1][j + 1] = c, (i, j, True)
            if j < J:                             # 음표 건너뜀 (멜리스마)
                if cost[i][j] < cost[i][j + 1]:
                    cost[i][j + 1], back[i][j + 1] = cost[i][j], (i, j, False)
            if i < I:                             # 음절 건너뜀 (오인식)
                c = cost[i][j] + gaps[i]
                if c < cost[i + 1][j]:
                    cost[i + 1][j], back[i + 1][j] = c, (i, j, False)
    pairs, i, j = [], I, J
    while (i, j) != (0, 0) and back[i][j]:
        pi, pj, matched = back[i][j]
        if matched:
            pairs.append((pi, pj))
        i, j = pi, pj
    return pairs[::-1]


def read_title(page: Page, systems: list[System]) -> tuple[str, str]:
    """첫 오선 위 영역에서 가장 큰 글줄 = 제목. '작곡'·'작사'가 있거나 오른쪽에 치우친 작은 글줄 = 작곡자."""
    if not systems:
        return "", ""
    top = page.gray[: systems[0].chord_band[0]]
    if top.size == 0 or top.shape[0] < 4:
        return "", ""
    lines = [(s.strip(), conf, bb) for s, conf, _, bb in recognize_lines(top) if s.strip()]
    if not lines:
        return "", ""
    title = max(lines, key=lambda l: l[2][3] - l[2][1])
    composer = ""
    others = [(s, bb) for s, _, bb in lines if s != title[0] and len(s) <= 20 and re.search(r"[가-힣A-Za-z]", s)]
    for s, bb in others:                                      # '작곡 ○○○' 이나 오른쪽 끝의 이름이 우선
        if re.search(r"작곡|작사|words|music|by\b", s, re.I) or bb[0] > page.gray.shape[1] * 0.55:
            composer = s
            break
    if not composer and others:                               # 아니면 제목 말고 남은 짧은 글줄 (예: 왼쪽 위 '박희정')
        composer = others[0][0]
    composer = re.sub(r"^(작곡|작사|words|music|by)\s*[:：]?\s*", "", composer, flags=re.I).strip()
    return re.sub(r"\s{2,}", " ", title[0]), composer
