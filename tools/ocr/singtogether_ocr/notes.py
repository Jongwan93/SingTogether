"""오선 안의 음표 머리와 마디줄 위치.

엔진들은 음표 좌표를 주지 않으므로 이미지에서 직접 찾고, 조립 단계에서 엔진의 음표 순서와 맞춘다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
        heads = find_noteheads(page.binary[y0:y1, st.left:st.right + 1], il)
        skip = st.left + (5 if k == 0 else 3.5) * il        # 음자리표·조표·박자표 자리
        heads = [Notehead(h.x + st.left, h.y + y0, h.w, h.h) for h in heads if h.x + st.left > skip]
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


def find_noteheads(region: np.ndarray, il: float) -> list[Notehead]:
    """오선·기둥·빔처럼 가는 것을 열림 연산으로 지우면 꽉 찬 머리만 남는다.
    속 빈 머리(2분·온음표)는 먼저 닫힘으로 채운 뒤 같은 방법으로 찾되, 원래 잉크가 성긴 것만 고른다."""
    ink = (region < 128).astype(np.uint8)
    open_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (max(3, int(0.8 * il)) | 1, max(3, int(0.6 * il)) | 1))
    close_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(0.6 * il) | 1, int(0.6 * il) | 1))
    filled = _head_blobs(cv2.morphologyEx(ink, cv2.MORPH_OPEN, open_k), il)
    closed = cv2.morphologyEx(cv2.morphologyEx(ink, cv2.MORPH_CLOSE, close_k), cv2.MORPH_OPEN, open_k)
    hollow = []
    for h in _head_blobs(closed, il, median_filter=False):   # 닫힘으로 커진 꽉 찬 머리들이 중앙값을 끌어올리므로 크기 범위만 본다
        inner, ring = hollow_stats(ink, h)
        if inner < 0.6 and ring > HOLLOW_RING_MIN and has_stem(ink, h, il) \
                and all(abs(h.x - f.x) > 0.5 * il for f in filled):
            hollow.append(h)
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


def find_barlines(binary: np.ndarray, staff: Staff, il: float, heads: list[Notehead]) -> list[float]:
    """첫 줄부터 다섯째 줄까지 거의 끊기지 않은 세로선 중, 오선 밖으로 더 뻗지 않고(기둥·빔 제외)
    바로 옆에 음표 머리가 없는 것 = 마디줄. 겹줄·도돌이표는 하나로 합친다."""
    ink = binary < 128
    top, bottom = staff.top, staff.bottom
    col = ink[top:bottom + 1, staff.left:staff.right + 1].mean(axis=0)
    xs = np.flatnonzero(col >= 0.9) + staff.left
    groups: list[list[int]] = []
    for x in xs:
        if groups and x - groups[-1][-1] <= 0.6 * il:
            groups[-1].append(int(x))
        else:
            groups.append([int(x)])
    out = []
    for g in groups:
        x = int(np.mean(g))
        if x < staff.left + 0.3 * il:
            continue
        above = ink[max(0, int(top - 1.2 * il)):int(top - 0.3 * il), max(0, x - 1):x + 2].mean()
        below = ink[int(bottom + 0.3 * il):int(bottom + 1.2 * il), max(0, x - 1):x + 2].mean()
        near_head = any(abs(h.x - x) < 0.9 * il for h in heads)
        if above < 0.3 and below < 0.3 and not near_head:
            out.append(float(x))
    return out
