"""오선 안의 음표 머리와 마디줄 위치.

엔진들은 음표 좌표를 주지 않으므로 이미지에서 직접 찾고, 조립 단계에서 엔진의 음표 순서와 맞춘다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np

from . import debug
from .ingest import Page
from .layout import Staff, System


@dataclass
class Notehead:
    x: float
    y: float
    w: int
    h: int
    evidence: "DurationEvidence | None" = None     # 길이 단서 (find_symbols가 채운다)
    taken: bool = False                             # 엔진 음표와 짝지어졌나 (assemble이 채운다)


@dataclass
class SystemSymbols:
    noteheads: list[Notehead] = field(default_factory=list)
    barlines: list[float] = field(default_factory=list)     # x


def find_symbols(page: Page, systems: list[System], debug_dir: Path | None = None) -> list[SystemSymbols]:
    il = page.interline
    H = page.binary.shape[0]
    out = []
    for k, sys_ in enumerate(systems):
        st = sys_.staff
        y0, y1 = max(0, int(st.top - 2 * il)), min(H, int(st.bottom + 2 * il))
        heads = find_noteheads(page.binary[y0:y1, st.left:st.right + 1], il, [ln - y0 for ln in st.lines])
        skip = st.left + (5 if k == 0 else 3.5) * il        # 음자리표·조표·박자표 자리
        heads = [Notehead(h.x + st.left, h.y + y0, h.w, h.h) for h in heads if h.x + st.left > skip]
        for h in heads:
            h.evidence = duration_evidence(page.binary, h, il)
        out.append(SystemSymbols(heads, find_barlines(page.binary, st, il, heads)))
    if debug_dir:
        img = debug.canvas(page.gray)
        for sys_, syms in zip(systems, out):
            debug.boxes(img, [(h.x - h.w / 2, h.y - h.h / 2, h.x + h.w / 2, h.y + h.h / 2) for h in syms.noteheads], "note")
            for x in syms.barlines:
                cv2.line(img, (int(x), int(sys_.staff.top - il)), (int(x), int(sys_.staff.bottom + il)), debug.COLORS["staff"], 2)
        total = sum(len(s.noteheads) for s in out)
        debug.caption(img, f"{page.name}  noteheads {total}  barlines {[len(s.barlines) for s in out]}")
        debug.save(debug_dir / f"{page.name}-3-symbols.png", img)
    return out


def find_noteheads(region: np.ndarray, il: float, lines: list[int] | None = None) -> list[Notehead]:
    """오선·기둥·빔처럼 가는 것을 열림 연산으로 지우면 꽉 찬 머리만 남는다.
    속 빈 머리(2분·온음표)는 먼저 닫힘으로 채운 뒤 같은 방법으로 찾되, 원래 잉크가 성긴 것만 고른다.
    lines: 이 영역 기준 오선 줄 y (지금은 안 쓰지만 호출부가 넘긴다)."""
    ink = (region < 128).astype(np.uint8)
    open_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (max(3, int(0.8 * il)) | 1, max(3, int(0.6 * il)) | 1))
    close_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(0.6 * il) | 1, int(0.6 * il) | 1))
    filled = _head_blobs(cv2.morphologyEx(ink, cv2.MORPH_OPEN, open_k), il)
    closed = cv2.morphologyEx(cv2.morphologyEx(ink, cv2.MORPH_CLOSE, close_k), cv2.MORPH_OPEN, open_k)
    hollow = []
    for h in _head_blobs(closed, il, median_filter=False):   # 닫힘으로 커진 꽉 찬 머리들이 중앙값을 끌어올리므로 크기 범위만 본다
        if h.w < h.h:                                        # 머리는 가로가 더 길다. 세로로 긴 것은 8분음표 꼬리 (렌더링 악보에서 가짜 머리 13개)
            continue
        probe = h
        if h.w > 1.6 * il:                                   # 점(D4/2.)이 닫힘으로 붙어 넓어진 덩어리: 왼쪽 머리 부분만 본다
            w = int(1.3 * il)
            probe = Notehead(h.x - h.w / 2 + w / 2, h.y, w, h.h)
        inner, ring = hollow_stats(ink, probe)
        if inner < 0.6 and ring > HOLLOW_RING_MIN and has_stem(ink, h, il) \
                and all(abs(probe.x - f.x) > 0.5 * il for f in filled):
            hollow.append(probe)
    # 붙임줄이 붙은 2분음표는 여전히 놓친다. 기둥 끝에서 다시 찾는 방식을 시험했지만(2026-10) 가짜 머리가 늘어
    # 웹 캡처 음높이가 79→59%로 떨어져서 뺐다. 놓친 머리는 마디 수 불일치로 이어져 그 마디는 엔진 음을 그대로 둔다.
    return sorted(filled + hollow, key=lambda h: h.x)


HOLLOW_RING_MIN = 0.45


def hollow_stats(ink: np.ndarray, h: Notehead) -> tuple[float, float]:
    """속 빈 머리 판정 재료: (가운데 창의 잉크 비율, 그 바깥 고리의 잉크 비율).
    진짜 2분음표는 가운데가 희고 고리가 검다. 붙임줄·이음줄이 닫힘 연산으로 뭉친 가짜는 고리가 듬성듬성하다."""
    cx, cy = int(h.x), int(h.y)
    wx, wy = max(1, int(h.w * 0.2)), max(1, int(h.h * 0.2))
    x0, y0 = max(0, int(h.x - h.w / 2)), max(0, int(h.y - h.h / 2))
    box = ink[y0:y0 + h.h, x0:x0 + h.w]
    core = ink[max(0, cy - wy):cy + wy, max(0, cx - wx):cx + wx]
    if box.size == 0 or core.size == 0 or box.size == core.size:
        return 1.0, 0.0
    inner = float(core.mean())
    ring = float((box.sum() - core.sum()) / (box.size - core.size))
    return inner, ring


def has_stem(ink: np.ndarray, h: Notehead, il: float) -> bool:
    """머리의 왼쪽 또는 오른쪽 가장자리에서 위나 아래로 뻗는 세로선(2il 창 안에 1.5il 이상 잉크)이 있으면 기둥.
    이음줄·붙임줄이 닫힘 연산으로 뭉친 가짜 속 빈 머리를 걸러낸다. (온음표는 기둥이 없어 놓친다)"""
    H, W = ink.shape
    cy = int(h.y)
    # 점(D4/2.)이나 붙임줄이 닫힘 연산으로 머리에 붙어 덩어리가 넓어질 수 있으니 덩어리 폭 전체를 훑는다
    for x in range(int(h.x - h.w / 2) - 3, int(h.x + h.w / 2) + 4):
        if not 0 <= x < W:
            continue
        for ya, yb in ((cy - int(2.5 * il), cy - int(0.5 * il)), (cy + int(0.5 * il), cy + int(2.5 * il))):
            seg = ink[max(0, ya):max(0, yb), x]
            if seg.size and seg.sum() >= 1.5 * il:
                return True
    return False


def _head_blobs(mask: np.ndarray, il: float, median_filter: bool = True) -> list[Notehead]:
    """머리 크기 범위의 덩어리를 모은 뒤, 중앙값 크기에서 40% 넘게 벗어난 것(조표 조각, 빔 토막)은 버린다."""
    n, _, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        if 0.8 * il <= w <= 2.4 * il and 0.7 * il <= h <= 1.8 * il and 0.8 <= w / h <= 2.2:
            out.append(Notehead(float(centroids[i][0]), float(centroids[i][1]), int(w), int(h)))
    if len(out) < 3 or not median_filter:
        return out
    mw, mh = np.median([h.w for h in out]), np.median([h.h for h in out])
    return [h for h in out if abs(h.w - mw) <= 0.4 * mw and abs(h.h - mh) <= 0.4 * mh]


@dataclass
class DurationEvidence:
    """이미지에서 읽은 길이 단서 (1-b: 엔진 길이의 두 번째 의견)."""
    hollow: bool          # 속 빈 머리 (2분·온음표)
    stem: bool            # 기둥이 있나 (온음표는 없다)
    beams: int            # 빔·꼬리 수 (0 = 4분 이상, 1 = 8분, 2 = 16분)
    dot: bool             # 점

    def quarters(self) -> list[Fraction]:
        """단서와 맞는 길이들 (4분음표 = 1). 점은 1.5배."""
        if self.hollow:
            base = [Fraction(4)] if not self.stem else [Fraction(2)]
        elif self.beams >= 2:
            base = [Fraction(1, 4)]
        elif self.beams == 1:
            base = [Fraction(1, 2)]
        else:
            base = [Fraction(1)]
        return [b * Fraction(3, 2) for b in base] if self.dot else base


def duration_evidence(binary: np.ndarray, h: Notehead, il: float) -> DurationEvidence:
    """머리 하나의 길이 단서. 기둥은 머리의 오른쪽 위(위로 뻗는 기둥) 또는 왼쪽 아래(아래로)에서 찾고,
    빔·꼬리는 기둥 끝 쪽 1.5il 구간에서 기둥 옆(양쪽)의 가로 잉크 띠 수로 센다. 점은 머리 오른쪽 0.65–1.4il의 작은 덩어리."""
    ink = (binary < 128)
    H, W = ink.shape
    cx, cy = int(round(h.x)), int(round(h.y))
    inner, ring = hollow_stats(ink.astype(np.uint8), h)
    hollow = inner < 0.6 and ring > HOLLOW_RING_MIN

    def stem_len(x: int, direction: int) -> int:
        """x 열(±1)에서 머리 가장자리부터 direction으로 잉크가 이어지는 길이(px). 두 줄 비면 끝."""
        y = cy - int(0.6 * il) if direction < 0 else cy + int(0.6 * il)
        n = gap = 0
        while 0 <= y < H and gap < 2 and n < 6 * il:
            if ink[y, max(0, x - 1):x + 2].mean() >= 0.5:
                n, gap = n + 1, 0
            else:
                gap += 1
            y += direction
        return n

    best = (0, 0, 0)                                              # (길이, 기둥 x, 방향)
    for dx in range(-3, 4):
        x_up = int(h.x + h.w / 2) + dx                            # 위로 뻗는 기둥은 머리 오른쪽
        x_dn = int(h.x - h.w / 2) + dx                            # 아래로 뻗는 기둥은 머리 왼쪽
        for x, d in ((x_up, -1), (x_dn, +1)):
            if 0 <= x < W:
                n = stem_len(x, d)
                if n > best[0]:
                    best = (n, x, d)
    length, sx, d = best
    stem = length >= 1.5 * il
    beams = 0
    if stem:
        # 기둥 끝에서 머리 쪽으로 2.2il 안, 기둥 옆 좁은 열(±0.4il, ±0.7il)의 세로 프로파일에서 두꺼운(0.2il+) 잉크 띠 수.
        # 빔은 기울어져 있어 넓은 띠의 평균을 쓰면 두 빔이 하나로 뭉개진다. 오선 줄(1–3px)은 두께로 걸러진다.
        end = cy + d * (int(0.6 * il) + length)                   # 기둥 끝 y
        y0, y1 = (end, end + int(2.2 * il)) if d < 0 else (end - int(2.2 * il), end)
        y0, y1 = max(0, y0), min(H, y1)
        for off in (0.4, 0.7, -0.4, -0.7):
            x = sx + int(off * il)
            if not 1 <= x < W - 1 or y1 <= y0:
                continue
            rows = ink[y0:y1, x - 1:x + 2].mean(axis=1) >= 0.5
            bands, run = 0, 0
            for r in list(rows) + [False]:
                if r:
                    run += 1
                else:
                    if run >= 0.2 * il:
                        bands += 1
                    run = 0
            beams = max(beams, bands)
    # 점: 머리 오른쪽, 머리가 줄 위면 바로 위 칸에 찍힌다 → 세로로 넉넉히 본다
    x0, x1 = min(W, cx + int(0.65 * il)), min(W, cx + int(1.4 * il))
    y0, y1 = max(0, cy - int(0.6 * il)), min(H, cy + int(0.35 * il))
    dot = False
    if x1 > x0 and y1 > y0:
        region = ink[y0:y1, x0:x1].astype(np.uint8)
        n, _, stats, _ = cv2.connectedComponentsWithStats(region, connectivity=8)
        for i in range(1, n):
            x, y, w, hh, area = stats[i]
            if 0.12 * il <= w <= 0.45 * il and 0.12 * il <= hh <= 0.45 * il and area >= 0.5 * w * hh:
                dot = True
    return DurationEvidence(hollow, stem, min(beams, 2), dot)


def find_barlines(binary: np.ndarray, staff: Staff, il: float, heads: list[Notehead]) -> list[float]:
    """첫 줄부터 다섯째 줄까지 거의 끊기지 않은 세로선 중 마디줄만 고른다.

    오선 바로 바깥(0.5il 안)이 이어져 검으면 기둥·빔이고, 머리 바로 옆(0.9il 안)이면 기둥이다 (빔이 첫 줄 높이에 붙은
    8분음표 기둥은 바깥으로 안 뻗으므로 머리 거리로만 거른다). 1·2번 괄호 아래 마디줄은 위로 괄호 선이 이어지므로, 위끝에서
    오른쪽으로 얇은 가로선이 나가면 마디줄로 본다. 오선 왼쪽 끝의 세로선·음자리표·시작 도돌이(왼쪽에 머리가 없는 선)는
    마디의 끝이 아니므로 뺀다. 선은 제 폭만큼만 묶고(옆 기둥과 섞이지 않게), 겹줄·도돌이표(0.6il 안)는 마지막에 하나로 합친다."""
    ink = binary < 128
    top, bottom = staff.top, staff.bottom
    col = ink[top:bottom + 1, staff.left:staff.right + 1].mean(axis=0)
    xs = np.flatnonzero(col >= 0.9) + staff.left
    groups: list[list[int]] = []
    for x in xs:
        if groups and x - groups[-1][-1] <= 0.25 * il:
            groups[-1].append(int(x))
        else:
            groups.append([int(x)])

    def bracket(x: int) -> bool:
        """위끝 근처(첫 줄 위 0.3–2il)에서 오른쪽으로 얇은 가로선(1·2번 괄호, 실선 또는 점선)이 나가는가.
        빔은 두껍고(0.4il+) 꼬리는 덩어리라 걸리지 않는다. 괄호 아래의 마디줄은 위로 0.5il 넘게 뻗지 않는다."""
        if ink[max(0, int(top - 1.2 * il)):max(0, int(top - 0.5 * il)), max(0, x - 1):x + 2].mean() >= 0.5:
            return False                                           # 0.5il 위까지 계속 검으면 기둥
        rows = ink[max(0, int(top - 1.6 * il)):max(0, int(top - 0.3 * il)), min(ink.shape[1] - 1, int(x + 0.3 * il)):int(x + 2.5 * il)]
        if rows.size == 0:
            return False
        profile = rows.mean(axis=1)
        solid, dashed, faint = (profile >= 0.8).sum(), (profile >= 0.25).sum(), (profile >= 0.1).sum()
        # 점선 괄호는 몇 줄에 몰려 있다. 비스듬히 지나가는 이음줄은 옅은 잉크가 여러 줄에 퍼진다
        return (1 <= solid <= 0.25 * il) or (solid == 0 and 1 <= dashed <= 0.3 * il and faint <= 0.4 * il)

    keep: list[float] = []
    for g in groups:
        x = int(np.mean(g))
        if x < staff.left + 5 * il or not any(h.x < x - 0.3 * il for h in heads):
            continue                                               # 음자리표·조표 자리, 시스템 시작선, 시작 도돌이
        if any(abs(h.x - x) < 0.9 * il for h in heads):
            continue                                               # 머리에 붙은 기둥
        above = ink[max(0, int(top - 0.5 * il)):top, max(0, x - 1):x + 2].mean()
        below = ink[bottom + 1:int(bottom + 0.5 * il) + 1, max(0, x - 1):x + 2].mean()
        if below >= 0.5:
            continue                                               # 아래로 뻗은 기둥
        if above >= 0.5 and not bracket(x):
            continue                                               # 위로 뻗은 기둥 (괄호 선이면 통과)
        keep.append(float(x))
    out: list[float] = []
    for x in keep:
        if out and x - out[-1] <= 0.6 * il:                        # 겹줄·도돌이표는 하나로
            out[-1] = (out[-1] + x) / 2
        elif out and x - out[-1] <= 2 * il and not any(out[-1] < h.x < x for h in heads):
            continue                                               # :||: 처럼 떨어진 겹줄 (사이에 머리 없음). 온쉼표 마디는 더 넓다
        else:
            out.append(x)
    return out
