"""제자리 이조: 원본 그림에서 음표 머리·기둥 끝·조표·임시표·코드 글자만 고쳐 새 조로 다시 쓴다. 나머지(가사, 박자표, 이음줄,
도돌이, 제목…)는 손대지 않는다.

이조는 모든 음을 같은 간격으로 옮기는 것이라 모든 머리가 오선에서 같은 칸 수(steps)만큼 움직인다. 그래서 리듬을 읽을 필요가
없고, 머리 위치·조표·임시표만 알면 된다. 틀려도 틀린 음 하나가 틀릴 뿐 악보 전체가 무너지지는 않는다.

한계(첫 판): 높은음자리표 한 줄 멜로디만. 온음표(기둥 없는 속 빈 머리)는 머리 검출이 못 잡으면 제자리에 남는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from . import ingest, layout, notes, text
from .layout import Staff, System
from .model import MAJOR_BY_FIFTHS, STEP_PC
from .notes import Notehead

LETTERS = "CDEFGAB"
SHARP_ORDER = "FCGDAEB"
FLAT_ORDER = "BEADGCF"
# 높은음자리표 조표 자리: 오선 맨 아래 줄(E4)을 0으로 한 반 칸 단위. ♯: F5 C5 G5 D5 A4 E5 B4, ♭: B4 E5 A4 D5 G4 C5 F4
TREBLE_SHARP_STEPS = [8, 5, 9, 6, 3, 7, 4]
TREBLE_FLAT_STEPS = [4, 7, 3, 6, 2, 5, 1]
ACC_MATCH = 0.58          # 임시표 템플릿 일치 문턱 (정규화 상관)


@dataclass
class HeadPlan:
    head: Notehead
    step_old: int
    step_new: int
    hollow: bool
    acc_old: tuple[str, tuple[int, int, int, int]] | None = None     # 그림에 있던 임시표 (종류, 상자)
    acc_new: str | None = None                                        # 새로 그릴 임시표: "sharp" / "flat" / "natural"
    dot: tuple[int, int, int, int] | None = None                      # 점(부점) 상자: 머리와 함께 옮긴다


@dataclass
class Report:
    old_fifths: int = 0
    new_fifths: int = 0
    steps: int = 0
    heads: int = 0
    accidentals_found: int = 0
    accidentals_drawn: int = 0
    chords: int = 0
    key_source: str = "image"
    chord_source: str = "Apple Vision"
    notes_: list[str] = field(default_factory=list)

    @property
    def keys(self) -> str:
        return f"{MAJOR_BY_FIFTHS[self.old_fifths + 6]} -> {MAJOR_BY_FIFTHS[self.new_fifths + 6]}"


# MARK: 음악 계산

def key_alters(fifths: int) -> dict[str, int]:
    if fifths > 0:
        return {l: 1 for l in SHARP_ORDER[:fifths]}
    if fifths < 0:
        return {l: -1 for l in FLAT_ORDER[:-fifths]}
    return {}


def target_fifths(old: int, semitones: int) -> int:
    """반음 s만큼 옮긴 뒤의 조표(-6…6). 둘 다 가능하면(F♯/G♭) 원래 조표의 쪽 (Swift MusicXML.targetFifths와 같다)."""
    d0 = (7 * semitones) % 12
    cands = [old + d0 + 12 * k for k in range(-3, 4) if -6 <= old + d0 + 12 * k <= 6]
    if not cands:
        return old
    return max(cands) if old >= 0 else min(cands)


def diatonic_steps(old: int, new: int) -> int:
    """조표 이동에 따른 음이름 글자 이동량(칸). -3…3 (반음 -5…5 안에서는 늘 이 범위)."""
    dia = ((new - old) * 4) % 7
    return dia if dia <= 3 else dia - 7


def step_pitch(step: int) -> tuple[str, int]:
    """오선 자리(E4 = 0, 반 칸 = 1) → (글자, 옥타브)."""
    i = step + 2                                    # C=0 … E=2
    return LETTERS[i % 7], 4 + (i // 7)


def midi(letter: str, octave: int, alter: int) -> int:
    return 12 * (octave + 1) + STEP_PC[letter] + alter


# MARK: 그림 다루기

def line_thickness(ink: np.ndarray, staff: Staff) -> int:
    rows = []
    for y in staff.lines:
        n = 0
        for dy in range(-3, 4):
            yy = y + dy
            if 0 <= yy < ink.shape[0] and ink[yy, staff.left:staff.right].mean() >= 0.5:
                n += 1
        rows.append(n)
    return max(1, int(np.median(rows))) if rows else 1


def strip_staff_lines(region: np.ndarray, line_rows: list[int], t_line: int) -> np.ndarray:
    """영역(0/1)에서 오선 줄 픽셀만 지운다: 줄 행 근처의 잉크 중 (t_line+2)만큼 위와 아래가 모두 흰 열."""
    out = region.copy()
    H = region.shape[0]
    r = t_line + 2
    for yy in line_rows:
        for y in range(max(0, yy - t_line - 1), min(H, yy + t_line + 2)):
            above = region[y - r] if y - r >= 0 else np.zeros(region.shape[1], region.dtype)
            below = region[y + r] if y + r < H else np.zeros(region.shape[1], region.dtype)
            out[y][(above == 0) & (below == 0)] = 0
    return out


def erase(img: np.ndarray, box: tuple[int, int, int, int], staff: Staff, t_line: int) -> None:
    """상자를 희게 지운다. 오선 줄이 지나는 행은 상자 좌우 바깥의 줄 픽셀(열 6개씩의 중앙값)로 채워 줄을 잇는다:
    그대로 두면 ♯의 굵은 획처럼 줄 행에 걸친 글자 잉크가 토막으로 남는다."""
    x0, y0, x1, y1 = box
    H, W = img.shape[:2]
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if x1 <= x0 or y1 <= y0:
        return
    keep = np.zeros(y1 - y0, bool)
    for y in staff.lines:
        a, b = max(y0, y - t_line // 2 - 1), min(y1, y + (t_line + 1) // 2 + 1)
        if b > a:
            keep[a - y0:b - y0] = True
    img[np.flatnonzero(~keep) + y0, x0:x1] = 255
    line_rows = np.flatnonzero(keep) + y0
    if not line_rows.size:
        return
    around = np.concatenate([img[line_rows, max(0, x0 - 6):x0], img[line_rows, x1:min(W, x1 + 6)]], axis=1)
    img[line_rows, x0:x1] = (np.median(around, axis=1).astype(np.uint8)[:, None] if around.shape[1] else 255)


HEAD_TILT = 22            # 머리 타원의 기울기 (도)


def draw_head(img: np.ndarray, cx: float, cy: float, w: float, h: float, hollow: bool, il: float) -> None:
    """기울어진 타원. w × h는 head_shape가 그림의 머리에서 잰 상자 크기라, 기울여 그렸을 때 상자가 그만큼 되는 축을 푼다
    (상자 폭 W, 높이 H, 각 θ: a²cos²θ + b²sin²θ = (W/2)², a²sin²θ + b²cos²θ = (H/2)²).
    속 빈 머리는 고리만 그려 안을 지나는 오선 줄이 보이게 둔다."""
    c2, s2 = np.cos(np.radians(HEAD_TILT)) ** 2, np.sin(np.radians(HEAD_TILT)) ** 2
    P, Q = (w / 2) ** 2, (h / 2) ** 2
    a2, b2 = (P * c2 - Q * s2) / (c2 - s2), (Q * c2 - P * s2) / (c2 - s2)
    axes = (max(2, int(round(np.sqrt(max(a2, 1.0))))), max(2, int(round(np.sqrt(max(b2, 1.0))))))
    c = (int(round(cx)), int(round(cy)))
    if hollow:
        cv2.ellipse(img, c, axes, -HEAD_TILT, 0, 360, 0, max(2, int(round(0.16 * il))), cv2.LINE_AA)
    else:
        cv2.ellipse(img, c, axes, -HEAD_TILT, 0, 360, 0, -1, cv2.LINE_AA)


def head_shape(ink: np.ndarray, heads: list[Notehead], staff: Staff, t_line: int, il: float) -> tuple[float, float]:
    """그림의 머리 크기 (폭, 높이): 오선 줄 위에 앉은, 기둥 달린 속 찬 머리들의 잉크에서 줄과 기둥 열을 빼고 잰 너비·높이의 중앙값.
    악보 글꼴마다 머리 크기가 달라서(Leipzig는 1.2il × 0.85il, 표준은 1.2 × 0.82) 정해 두면 새 머리가 작거나 커 보인다.
    검출 상자(h.w, h.h)는 기둥 밑동·안티에일리어싱이 붙어 더 크다. 칸에 든 머리는 위아래 줄이 가장자리에 붙어 줄 빼기로 안
    빠지고(위아래가 머리 잉크다) 높이를 부풀리므로 줄 위 머리만 잰다 (줄이 머리 한가운데를 지나 잉크에 묻힌다); 덧줄 음은 덧줄이
    머리 밖으로 나와 폭을 부풀리므로 뺀다. 잴 머리가 없으면 1.2il × 0.82il."""
    H, W = ink.shape
    ws, hs = [], []
    for h in heads:
        ev = h.evidence
        if not ev or ev.hollow or not ev.stem:
            continue
        if not staff.top - 0.2 * il <= h.y <= staff.bottom + 0.2 * il or min(abs(h.y - y) for y in staff.lines) > 0.2 * il:
            continue
        x0, x1 = int(round(h.x - 0.8 * il)), int(round(h.x + 0.8 * il)) + 1
        y0, y1 = int(round(h.y - 0.75 * il)), int(round(h.y + 0.75 * il)) + 1
        if x0 < 0 or y0 < 0 or x1 > W or y1 > H:
            continue
        box = strip_staff_lines(ink[y0:y1, x0:x1].astype(np.uint8), [y - y0 for y in staff.lines if y0 <= y < y1], t_line)
        t_stem = max(1, int(round(0.08 * il)))
        box[:, max(0, ev.stem_x - t_stem - 1 - x0):max(0, ev.stem_x + t_stem + 2 - x0)] = 0
        rows, cols = np.flatnonzero(box.sum(axis=1) >= 3), np.flatnonzero(box.sum(axis=0) >= 3)   # 기둥 찌꺼기(2px)는 뺀다
        if rows.size < 0.5 * il or cols.size < 0.6 * il or rows.size > 1.3 * il or cols.size > 1.5 * il:
            continue
        ws.append(float(cols[-1] - cols[0] + 1))
        hs.append(float(rows[-1] - rows[0] + 1))
    if len(ws) < 2:
        return 1.2 * il, 0.82 * il
    return float(np.median(ws)), float(np.median(hs))


def draw_ledgers(img: np.ndarray, staff: Staff, step: int, cx: float, w: int, t_line: int) -> None:
    """오선 밖 자리에 덧줄. 줄 자리(짝수 칸)마다 하나, 머리 폭보다 조금 길게."""
    il = staff.interline
    half = int(round(w / 2 + 0.3 * il))
    x0, x1 = int(cx) - half, int(cx) + half
    if step > 8:
        for s in range(10, step + 1, 2):
            y = int(round(staff.bottom - s * il / 2))
            cv2.line(img, (x0, y), (x1, y), 0, t_line)
    elif step < 0:
        for s in range(-2, step - 1, -2):
            y = int(round(staff.bottom - s * il / 2))
            cv2.line(img, (x0, y), (x1, y), 0, t_line)


def glyph(kind: str, il: float) -> np.ndarray:
    """임시표 잉크 마스크 (흰 바탕 0, 잉크 255). 가운데가 음표 자리. 폭 1.4il, 높이 3.2il."""
    W, H = int(1.4 * il), int(3.2 * il)
    g = np.zeros((H, W), np.uint8)
    cx, cy = W // 2, H // 2
    t1 = max(1, int(round(0.07 * il)))           # 세로획
    t2 = max(2, int(round(0.17 * il)))           # 가로 굵은 획
    if kind == "sharp":
        for dx in (-0.17, 0.17):
            x = int(cx + dx * il)
            cv2.line(g, (x, int(cy - 1.05 * il)), (x, int(cy + 1.05 * il)), 255, t1)
        for dy in (-0.3, 0.3):
            cv2.line(g, (int(cx - 0.48 * il), int(cy + dy * il + 0.12 * il)), (int(cx + 0.48 * il), int(cy + dy * il - 0.12 * il)), 255, t2)
    elif kind == "natural":
        xl, xr = int(cx - 0.2 * il), int(cx + 0.2 * il)
        cv2.line(g, (xl, int(cy - 1.05 * il)), (xl, int(cy + 0.42 * il)), 255, t1)
        cv2.line(g, (xr, int(cy - 0.42 * il)), (xr, int(cy + 1.05 * il)), 255, t1)
        for dy in (-0.33, 0.33):
            cv2.line(g, (xl, int(cy + dy * il + 0.1 * il)), (xr, int(cy + dy * il - 0.1 * il)), 255, t2)
    else:                                        # flat: 세로획 + 오른쪽 반 고리, 고리가 음표 자리
        x = int(cx - 0.28 * il)
        cv2.line(g, (x, int(cy - 1.45 * il)), (x, int(cy + 0.55 * il)), 255, t1)
        cv2.ellipse(g, (int(cx - 0.05 * il), int(cy + 0.05 * il)), (int(0.33 * il), int(0.5 * il)), 0, -100, 95, 255, t2)
    return g


def draw_glyph(img: np.ndarray, kind: str, cx: float, cy: float, il: float) -> None:
    g = glyph(kind, il)
    h, w = g.shape
    x0, y0 = int(round(cx - w / 2)), int(round(cy - h / 2))
    H, W = img.shape[:2]
    sx0, sy0 = max(0, -x0), max(0, -y0)
    x0, y0 = max(0, x0), max(0, y0)
    hh, ww = min(h - sy0, H - y0), min(w - sx0, W - x0)
    if hh <= 0 or ww <= 0:
        return
    region = img[y0:y0 + hh, x0:x0 + ww]
    region[g[sy0:sy0 + hh, sx0:sx0 + ww] > 0] = 0


def match_accidental(ink: np.ndarray, x0: int, x1: int, cy: float, il: float) -> tuple[str, tuple[int, int, int, int], float] | None:
    """머리 왼쪽 틈(x0..x1)에서 ♯·♭·♮ 템플릿을 찾는다. (종류, 상자, 점수) 또는 None."""
    H, W = ink.shape
    y0, y1 = max(0, int(cy - 1.9 * il)), min(H, int(cy + 1.9 * il))
    x0, x1 = max(0, x0), min(W, x1)
    region = (ink[y0:y1, x0:x1] * 255).astype(np.uint8)
    best = None
    for kind in ("sharp", "flat", "natural"):
        g = glyph(kind, il)
        if region.shape[0] < g.shape[0] or region.shape[1] < g.shape[1]:
            continue
        res = cv2.matchTemplate(region, g, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if score >= ACC_MATCH and (best is None or score > best[2]):
            bx, by = x0 + loc[0], y0 + loc[1]
            best = (kind, (bx, by, bx + g.shape[1], by + g.shape[0]), float(score))
    return best


def find_key_signature(ink: np.ndarray, staff: Staff) -> tuple[int, list[tuple[int, int, int, int]], int, int]:
    """오선 시작의 조표: (fifths, 글자 상자들, 조표 시작 x, 조표 다음 잉크 x). 조표의 k번째 ♯/♭은 자리가 정해져 있어
    그 y에 맞는 덩어리를 세어 종류와 수를 안다. 조표가 없으면 (0, [], 음자리표 오른쪽, 그다음 잉크)."""
    il = staff.interline
    H, W = ink.shape
    x_lo, x_hi = staff.left + int(1.2 * il), min(W, staff.left + int(11 * il))
    y_lo, y_hi = max(0, int(staff.top - 2 * il)), min(H, int(staff.bottom + 2 * il))
    region = ink[y_lo:y_hi, x_lo:x_hi].astype(np.uint8)
    # 오선 줄만 지운다: 줄 행의 잉크 중 위아래가 흰 것(줄뿐인 곳)만. 줄을 가로지르는 ♯·♭의 획은 남는다
    cut = strip_staff_lines(region, [y - y_lo for y in staff.lines], line_thickness(ink, staff))
    n, _, stats, cents = cv2.connectedComponentsWithStats(cut, connectivity=8)
    comps = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        comps.append((x + x_lo, y + y_lo, w, h, cents[i][0] + x_lo, cents[i][1] + y_lo))
    # 음자리표: 가장 왼쪽의 키 큰 덩어리
    clef_right = staff.left + int(2.5 * il)
    for x, y, w, h, cx, cy in comps:
        if h >= 4 * il and x < staff.left + 3 * il:
            clef_right = max(clef_right, x + w)
    cands = sorted([c for c in comps if c[0] >= clef_right - int(0.2 * il) and 1.7 * il <= c[3] <= 3.4 * il and 0.3 * il <= c[2] <= 1.15 * il],
                   key=lambda c: c[0])

    def y_of(step: int) -> float:
        return staff.bottom - step * il / 2

    best = (0, [])
    for kind, steps in (("sharp", TREBLE_SHARP_STEPS), ("flat", TREBLE_FLAT_STEPS)):
        boxes, k, last_x = [], 0, clef_right - il
        for c in cands:
            if k >= 7:
                break
            x, y, w, h, cx, cy = c
            if x < last_x:
                continue
            # 글자 중심은 ♯이면 자리 그대로, ♭은 고리가 자리라 중심이 0.4il 위
            expect = y_of(steps[k]) - (0.45 * il if kind == "flat" else 0)
            if abs(cy - expect) <= 0.45 * il and x - last_x <= 2.2 * il:
                boxes.append((x, y, x + w, y + h))
                last_x, k = x + w, k + 1
            elif boxes:
                break
        if len(boxes) > len(best[1]):
            best = (len(boxes) if kind == "sharp" else -len(boxes), boxes)
    fifths, boxes = best
    start_x = boxes[0][0] if boxes else clef_right + int(0.4 * il)
    end_x = boxes[-1][2] if boxes else clef_right + int(0.3 * il)
    # 조표 다음 잉크(박자표·첫 음표)의 x: 오선 줄을 뺀 세로 투영에서 빈 열 다음의 첫 잉크. 줄 찌꺼기(두께+2)까지는 빈 열로 본다
    prof = column_profile(ink, staff, il)
    empty = line_thickness(ink, staff) + 3
    x = min(W - 1, end_x + int(0.2 * il))
    while x < W and prof[x] > empty:
        x += 1
    while x < W and prof[x] <= empty:
        x += 1
    nxt = min(x, W - 1)
    if nxt > staff.left + 14 * il:                     # 못 찾았다: 적당한 자리로 (머리 검출 범위 제한에만 쓴다)
        nxt = end_x + int(1.5 * il)
    return fifths, boxes, start_x, nxt


def column_profile(ink: np.ndarray, staff: Staff, il: float) -> np.ndarray:
    """오선 안팎(±1.2il)의 열별 잉크 수, 오선 줄 행은 뺀 것."""
    t = line_thickness(ink, staff)
    y0, y1 = max(0, int(staff.top - 1.2 * il)), min(ink.shape[0], int(staff.bottom + 1.2 * il))
    band = ink[y0:y1, :].astype(np.uint8).copy()
    for y in staff.lines:
        band[max(0, y - t - 1 - y0):max(0, y + t + 2 - y0), :] = 0
    return band.sum(axis=0)


def key_from_profile(ink: np.ndarray, staff: Staff, il: float) -> tuple[int, tuple[int, int, int, int], int, int] | None:
    """흐린 그림(웹 캡처)용: 음자리표·조표·박자표가 한 덩어리로 붙어 글자를 못 가를 때, 열 투영의 골로 무리를 나눈다.
    음자리표(첫 번째 굵은 무리) 다음에 오는, 세로 폭 1.6–3.2il짜리 무리들이 조표. 글자 수는 폭/0.72il, ♯/♭은 잉크 무게중심 y로.
    (fifths 추정, 지울 상자, 시작 x, 다음 무리 시작 x) 또는 None."""
    H, W = ink.shape
    t = line_thickness(ink, staff)
    x_lo, x_hi = staff.left, min(W, staff.left + int(13 * il))
    y0, y1 = max(0, int(staff.top - 1.6 * il)), min(H, int(staff.bottom + 1.6 * il))
    band = ink[y0:y1, x_lo:x_hi].astype(np.uint8).copy()
    for y in staff.lines:
        band[max(0, y - t - 1 - y0):max(0, y + t + 2 - y0), :] = 0
    n_cols = band.shape[1]
    cnt = band.sum(axis=0)
    ext = np.zeros(n_cols)
    cen = np.zeros(n_cols)
    for x in range(n_cols):
        rows = np.flatnonzero(band[:, x])
        if rows.size >= 2:
            ext[x] = (rows[-1] - rows[0]) / il
            cen[x] = y0 + rows.mean()
    # 음자리표: 세로로 2.5il 넘게 잉크가 있는 첫 열부터, 그런 열이 이어지는 끝까지 (오른쪽 고리 포함, 최대 4.5il)
    tall = (cnt >= 4) & (ext >= 2.5)
    cs = int(np.argmax(tall)) if tall.any() else -1
    if cs < 0 or cs > 3 * il:
        return None
    ce = cs
    for x in range(cs, min(n_cols, cs + int(4.5 * il))):
        if tall[x]:
            ce = x
    clef_end = ce + 1
    # 그다음 잉크 무리들 (열 잉크 3픽셀 이상, 0.12il 미만의 끊김은 잇는다)
    runs: list[list[int]] = []
    x = clef_end
    limit = min(n_cols, clef_end + int(8 * il))
    while x < limit:
        while x < limit and cnt[x] < 3:
            x += 1
        s = x
        while x < limit and cnt[x] >= 3:
            x += 1
        if x > s:
            if runs and s - runs[-1][1] < 0.12 * il:
                runs[-1][1] = x
            else:
                runs.append([s, x])
    # 박자표나 기둥 달린 첫 음표(세로 2.8il 이상이 무리의 절반 넘게)가 나오면 거기까지가 조표 자리.
    # 중앙값을 쓰는 까닭: 음자리표 바로 다음 열은 음자리표 고리가 아직 이어져 세로가 길다
    big = next((r for r in runs if float(np.median(ext[r[0]:r[1]])) >= 2.8 and r[1] - r[0] >= 0.3 * il), None)
    nxt = x_lo + (big[0] if big else min(n_cols - 1, clef_end + int(2.5 * il)))
    key_runs = [r for r in runs if (big is None or r[1] <= big[0]) and r[1] - r[0] >= 0.25 * il and r[0] - clef_end <= 1.8 * il]
    chained: list[list[int]] = []
    for r in key_runs:
        if chained and r[0] - chained[-1][1] > 1.2 * il:
            break
        chained.append(r)
    # 지울 자리는 음자리표 끝부터 다음 무리 직전까지 (거기엔 조표밖에 없다). 글자 수·종류 어림은 최선일 뿐이라 호출부가 코드·AI로 보강한다
    if nxt - (x_lo + clef_end) > 7 * il:
        return None
    start_x = x_lo + clef_end + int(0.3 * il)
    box = (x_lo + clef_end + 1, int(staff.top - 1.6 * il), nxt - int(0.2 * il), int(staff.bottom + 1.6 * il))
    if not chained:
        return 0, box, start_x, nxt
    n = min(7, sum(max(1, int(round((e - s) / (0.75 * il)))) for s, e in chained))
    cols = np.concatenate([np.arange(s, e) for s, e in chained])
    cols = cols[cols >= clef_end + int(0.25 * il)]               # 음자리표 고리가 이어지는 첫 열은 뺀다
    centroid = float(cen[cols][cen[cols] > 0].mean()) if cols.size and (cen[cols] > 0).any() else staff.bottom - 6 * il / 2

    def y_of(step: int) -> float:
        return staff.bottom - step * il / 2

    sharp_c = float(np.mean([y_of(s) for s in TREBLE_SHARP_STEPS[:n]]))
    flat_c = float(np.mean([y_of(s) for s in TREBLE_FLAT_STEPS[:n]])) - 0.45 * il
    fifths = n if abs(centroid - sharp_c) <= abs(centroid - flat_c) else -n
    return fifths, box, start_x, nxt


KEY_PROMPT = ("Read the image {image}. It shows the beginning of one staff of a score in treble clef: the clef, then the key "
              "signature (a group of sharps or flats right after the clef, possibly none), then a time signature or notes. "
              "Count only the sharps or flats of the key signature. A sharp (#) has two vertical strokes crossed by two slanted "
              "bars; a flat (b) is a single vertical stem with a rounded loop at its lower right. Reply with exactly one line: "
              "`sharps N`, `flats N` or `none`.")


def ask_key(provider: str, page, staff: Staff, work: Path) -> int | None:
    """흐린 그림에서 조표를 AI에게 묻는다 (첫 오선의 앞부분 한 장, 한 번). fifths 또는 None."""
    import re

    from . import llm
    il = page.interline
    H, W = page.gray.shape
    x0, x1 = max(0, staff.left - int(0.5 * il)), min(W, staff.left + int(12 * il))
    y0, y1 = max(0, int(staff.top - 3 * il)), min(H, int(staff.bottom + 3 * il))
    crop = page.gray[y0:y1, x0:x1]
    if crop.shape[1] < 900:
        f = 900 / crop.shape[1]
        crop = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    work.mkdir(parents=True, exist_ok=True)
    path = work / f"{page.name}-key.png"
    cv2.imwrite(str(path), crop)
    try:
        answer = llm.ask(provider, KEY_PROMPT, path, timeout=120)
    except Exception:
        return None
    low = answer.lower()
    if (m := re.search(r"(sharp|flat)s?\s*[:=]?\s*(\d)", low)):
        n = int(m.group(2))
        return n if m.group(1) == "sharp" else -n
    if re.search(r"\bnone\b|no sharps or flats|0 sharps", low):
        return 0
    return None


def draw_key_signature(img: np.ndarray, staff: Staff, fifths: int, start_x: int, limit_x: int, il: float,
                       sample: tuple[np.ndarray, float] | None = None, spacing: float | None = None) -> None:
    """새 조표. sample = (그림에서 거둔 같은 종류 글자의 마스크, 그 글자 중심과 자리의 y 차이): 있으면 그림의 글꼴 그대로 찍는다."""
    if fifths == 0:
        return
    kind, steps = ("sharp", TREBLE_SHARP_STEPS) if fifths > 0 else ("flat", TREBLE_FLAT_STEPS)
    n = abs(fifths)
    spacing = spacing or 0.85 * il
    avail = limit_x + 0.3 * il - start_x - 0.5 * il        # 박자표 왼쪽 여백 0.3il까지는 들어가도 된다
    if n * spacing > avail and n > 1:
        spacing = max(0.62 * il, avail / n)
    for k in range(n):
        cx = start_x + 0.45 * il + k * spacing
        if sample is not None:
            stamp(img, sample[0], cx, staff.bottom - steps[k] * il / 2 + sample[1])
        else:
            draw_glyph(img, kind, cx, staff.bottom - steps[k] * il / 2 - (0.45 * il if kind == "flat" else 0), il)


def ring_closed(ink: np.ndarray, h: Notehead) -> bool:
    """속 빈 머리의 고리가 닫혀 있나: 가운데 행의 좌우 끝과 가운데 열의 위아래 끝에 모두 잉크가 있어야 한다.
    8분음표 꼬리 끝(기둥 한쪽에만 붙은 고리)이나 이음줄 토막은 한쪽이 트여 있어 걸러진다."""
    H, W = ink.shape
    x0, y0 = int(round(h.x - h.w / 2)), int(round(h.y - h.h / 2))
    x1, y1 = x0 + max(3, int(h.w)), y0 + max(3, int(h.h))
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return False
    box = ink[y0:y1, x0:x1]
    bh, bw = box.shape
    mid_rows = box[max(0, bh // 2 - max(1, int(0.15 * bh))):bh // 2 + max(1, int(0.15 * bh)) + 1, :]
    mid_cols = box[:, max(0, bw // 2 - max(1, int(0.15 * bw))):bw // 2 + max(1, int(0.15 * bw)) + 1]
    q = max(1, int(0.22 * bw)), max(1, int(0.22 * bh))
    return bool(mid_rows[:, :q[0]].any() and mid_rows[:, -q[0]:].any() and mid_cols[:q[1], :].any() and mid_cols[-q[1]:, :].any())


def find_hollow_heads(binary: np.ndarray, sys_: System, il: float, heads: list[Notehead], min_x: float) -> list[Notehead]:
    """notes.find_noteheads가 놓친 속 빈 머리(기둥 달린 2분음표). 닫힘으로 속을 메우면 기둥 아랫부분이 머리에 붙어
    세로로 긴 덩어리가 되는데, 거기서는 '세로로 긴 것은 꼬리'라며 버린다. 여기서는 덩어리에서 가장 넓은 띠(머리)를 잘라 본다.
    가짜(박자표 숫자, 붙은 ♯♯, 꼬리)를 거르려고: 조표 뒤(min_x)만, 띠가 타원처럼 꽉 차고(≥0.68), 폭 0.9–1.6il, 속 희고 고리 진함, 기둥 있음."""
    staff = sys_.staff
    H, W = binary.shape
    y0, y1 = max(0, int(staff.top - 2.5 * il)), min(H, int(staff.bottom + 2.5 * il))
    region = binary[y0:y1, staff.left:staff.right + 1]
    ink = (region < 128).astype(np.uint8)
    close_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(0.6 * il) | 1, int(0.6 * il) | 1))
    open_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (max(3, int(0.8 * il)) | 1, max(3, int(0.6 * il)) | 1))
    closed = cv2.morphologyEx(cv2.morphologyEx(ink, cv2.MORPH_CLOSE, close_k), cv2.MORPH_OPEN, open_k)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if not (0.9 * il <= w <= 2.2 * il and 0.7 * il <= h <= 2.6 * il):
            continue
        blob = labels[y:y + h, x:x + w] == i
        rows = blob.sum(axis=1)
        wide = rows >= 0.5 * rows.max()
        k = int(rows.argmax())
        lo = hi = k
        while lo - 1 >= 0 and wide[lo - 1]:
            lo -= 1
        while hi + 1 < len(rows) and wide[hi + 1]:
            hi += 1
        head_h = hi - lo + 1
        if not (0.6 * il <= head_h <= 1.4 * il):
            continue
        band = blob[lo:hi + 1]
        xs = np.flatnonzero(band.sum(axis=0) > 0)
        head_w = int(xs[-1] - xs[0] + 1)
        cx, cy = x + (xs[0] + xs[-1]) / 2, y + (lo + hi) / 2
        if not (0.9 * il <= head_w <= 1.6 * il) or head_w < 0.9 * head_h or band[:, xs[0]:xs[-1] + 1].mean() < 0.68:
            continue
        if cx + staff.left < min_x or cy + y0 > staff.bottom + 1.6 * il:
            continue
        probe = Notehead(cx, cy, head_w, max(head_h, int(0.9 * il)))
        inner, ring = notes.hollow_stats(ink, probe)
        if inner >= 0.6 or ring < notes.HOLLOW_RING_MIN or not notes.has_stem(ink, probe, il) or not ring_closed(ink, probe):
            continue
        hx, hy = cx + staff.left, cy + y0
        if any(abs(hx - o.x) < 0.7 * il and abs(hy - o.y) < 0.7 * il for o in heads):
            continue
        if any(flag_hook(hx, hy, o, il) for o in heads):
            continue
        head = Notehead(hx, hy, head_w, max(head_h, int(0.9 * il)))
        head.evidence = notes.duration_evidence(binary, head, il)
        out.append(head)
    return out


def flag_hook(x: float, y: float, o: Notehead, il: float) -> bool:
    """(x, y)가 머리 o의 기둥 반대쪽 끝인가: 8분음표 꼬리의 고리가 거기 있고, 닫힘 연산을 거치면 속 빈 머리처럼 보인다.
    고리는 기둥에서 오른쪽으로 말리므로 x는 기둥 x에서 -0.4il…+0.9il, y는 머리에서 기둥 방향으로 1.5il 넘게 떨어져 있다."""
    ev = o.evidence
    if not ev or not ev.stem:
        return False
    if not -0.4 * il <= x - ev.stem_x <= 0.9 * il:
        return False
    return (ev.stem_dir < 0 and y < o.y - 1.5 * il) or (ev.stem_dir > 0 and y > o.y + 1.5 * il)


def find_whole_notes(binary: np.ndarray, sys_: System, il: float, heads: list[Notehead], min_x: float) -> list[Notehead]:
    """기둥 없는 속 빈 머리(온음표). notes.find_noteheads는 이음줄 덩어리를 거르느라 기둥을 요구해 온음표를 놓친다.
    여기서는 모양을 더 엄하게 본다: 가로로 긴 타원(폭/높이 1.25–2.1), 가운데 희고 고리 진함, 기둥 없음, 이미 찾은 머리와 겹치지 않음.
    온음표의 속은 0.5il 닫힘으로는 안 메워져 0.7il을 쓴다. 중심이 오선 아래 1.1il 안인 것만 (가사의 o·in 같은 글자를 빼려고)."""
    staff = sys_.staff
    H, W = binary.shape
    y0, y1 = max(0, int(staff.top - 1.5 * il)), min(H, int(staff.bottom + 1.6 * il))
    region = binary[y0:y1, staff.left:staff.right + 1]
    ink = (region < 128).astype(np.uint8)
    close_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(0.7 * il) | 1, int(0.7 * il) | 1))
    open_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (max(3, int(0.8 * il)) | 1, max(3, int(0.6 * il)) | 1))
    closed = cv2.morphologyEx(cv2.morphologyEx(ink, cv2.MORPH_CLOSE, close_k), cv2.MORPH_OPEN, open_k)
    out = []
    for h in notes._head_blobs(closed, il, median_filter=False):
        if not (1.25 <= h.w / max(1, h.h) <= 2.1 and 1.2 * il <= h.w <= 2.1 * il):
            continue
        hx, hy = h.x + staff.left, h.y + y0
        if hx < min_x or hy > staff.bottom + 1.1 * il:
            continue
        inner, ring = notes.hollow_stats(ink, h)
        if inner >= 0.45 or ring < 0.5 or notes.has_stem(ink, h, il) or not ring_closed(ink, h):
            continue
        if any(abs(hx - o.x) < 0.8 * il and abs(hy - o.y) < 0.8 * il for o in heads):
            continue
        if any(flag_hook(hx, hy, o, il) for o in heads):
            continue
        head = Notehead(hx, hy, h.w, h.h)
        head.evidence = notes.duration_evidence(binary, head, il)
        out.append(head)
    return out


def find_dot(ink: np.ndarray, h: Notehead, il: float) -> tuple[int, int, int, int] | None:
    """머리 오른쪽 0.55–1.6il, 위아래 0.6il 안의 작은 덩어리(한 변 0.12–0.45il, 꽉 참) = 부점. 줄 위 음의 점은 위 칸에 있다."""
    H, W = ink.shape
    x0, x1 = int(h.x + 0.55 * il), min(W, int(h.x + 1.6 * il))
    y0, y1 = max(0, int(h.y - 0.6 * il)), min(H, int(h.y + 0.6 * il))
    if x1 <= x0 or y1 <= y0:
        return None
    n, _, stats, _ = cv2.connectedComponentsWithStats(ink[y0:y1, x0:x1].astype(np.uint8), connectivity=8)
    best = None
    for i in range(1, n):
        x, y, w, hh, area = stats[i]
        if 0.12 * il <= w <= 0.45 * il and 0.12 * il <= hh <= 0.45 * il and area >= 0.5 * w * hh and (best is None or x < best[0]):
            best = (x, y, w, hh)
    if best is None:
        return None
    x, y, w, hh = best
    return x0 + x, y0 + y, x0 + x + w, y0 + y + hh


def harvest_glyph(ink: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
    """조표 글자 하나의 잉크 마스크 (0/255). 새 조표나 임시표를 그릴 때 그림 자체의 글꼴을 쓴다."""
    x0, y0, x1, y1 = box
    return (ink[y0:y1, x0:x1] * 255).astype(np.uint8)


def stamp(img: np.ndarray, mask: np.ndarray, cx: float, cy: float) -> None:
    h, w = mask.shape
    x0, y0 = int(round(cx - w / 2)), int(round(cy - h / 2))
    H, W = img.shape[:2]
    sx0, sy0 = max(0, -x0), max(0, -y0)
    x0, y0 = max(0, x0), max(0, y0)
    hh, ww = min(h - sy0, H - y0), min(w - sx0, W - x0)
    if hh <= 0 or ww <= 0:
        return
    region = img[y0:y0 + hh, x0:x0 + ww]
    region[mask[sy0:sy0 + hh, sx0:sx0 + ww] > 0] = 0


# MARK: 전체 흐름

def transpose_in_place(src: Path, semitones: int, out: Path, llm_name: str = "none", debug_dir: Path | None = None,
                       work: Path | None = None) -> Report:
    """src 그림을 semitones만큼 제자리에서 이조해 out에 쓴다 (out 확장자대로 PNG/JPEG). 작업은 ingest가 키운 쪽 크기로 한다.
    work: AI 답을 캐시하는 폴더 (같은 그림을 다시 옮길 때 다시 묻지 않는다)."""
    page = ingest.prepare(src, debug_dir=debug_dir)[0]
    systems = layout.find_systems(page, debug_dir)
    if not systems:
        raise RuntimeError("no staff found")
    symbols = notes.find_symbols(page, systems, debug_dir)
    ink = page.binary < 128
    img = page.gray.copy()
    il = page.interline
    rep = Report()
    work = work or out.parent / ".singtogether-work"

    # 0. 코드 글자: Vision 상자(자리)에 AI가 읽은 이름을 맞춘다 (인식 모드와 같은 hybrid.merge_chords; Vision은 윗첨자 ♭·숫자를
    #    자주 틀려 흐린 캡처에서 48~62%, AI는 91~100%). AI가 없거나 답이 그림과 안 맞으면(다른 곡·지어냄) Vision 것을 쓴다.
    #    AI 전사는 조표도 주므로 흐린 그림의 조표를 따로 묻지 않아도 된다
    try:
        chord_boxes = text.read_chords(page, systems)
    except Exception as e:
        chord_boxes = []
        rep.notes_.append(f"chords not read: {str(e).splitlines()[0][:120]}")
    from . import llm
    provider = llm.pick(llm_name) if llm_name != "none" else "none"
    label = llm.LABELS.get(provider, provider)
    ai_t = None
    if provider != "none":
        from . import hybrid
        from .engines.claude import for_provider
        try:
            ai_t = for_provider(provider).transcribe(page, work, debug_dir)
        except Exception as e:
            rep.notes_.append(f"{label} did not read the score: {str(e).splitlines()[0][:120]}")
        if ai_t is not None:
            merged, _, exact = hybrid.merge_chords(chord_boxes, ai_t, systems, il, keep_vision_min=0.0)
            n_ai, n_vision = sum(1 for n in ai_t.notes if n.chord), sum(len(r) for r in chord_boxes)
            if hybrid.trust(exact, n_ai, n_vision):
                chord_boxes, rep.chord_source = snap_to_blobs(page, systems, merged, il), label
            else:
                rep.notes_.append(f"{label}'s chords did not match the picture ({int(exact * 100)}% agree): kept Apple Vision's")
                ai_t = None
    chord_names = [b.text for row in chord_boxes for b in row]
    from . import key as key_mod
    inferred = key_mod.infer_key(chord_names) if len(chord_names) >= 3 else None

    # 1. 조표: 오선마다 글자로 읽는다(깨끗한 그림). 흐린 그림(키운 웹 캡처)에서는 글자 검출이 일부만 잡히거나(4♭ 중 1개를 ♯으로)
    #    열 투영 어림도 틀려서(4♭을 2♭으로), 읽는 데는 코드로 추론한 조(코드 3개 이상, 다이어토닉 비율 0.7 이상)를 먼저 믿고
    #    (네 곡 모두 맞았다; AI는 셋), 코드가 없으면 AI에게 첫 오선 앞부분을 한 번 묻는다(몇 초). 지우는 데는 음자리표와 박자표
    #    사이를 통째로 쓴다 (글자 상자는 일부만이라 옛 ♭이 남는다).
    blurry = page.scale > 1.5
    read: list[tuple[int, list, int, int, tuple | None]] = []
    for s in systems:
        kf, kboxes, kstart, kend = find_key_signature(ink, s.staff)
        est = key_from_profile(ink, s.staff, il) if blurry or not kboxes else None
        read.append((kf, kboxes, kstart, kend, est))
    glyph_votes = [r[0] for r in read if r[1]]
    glyph_f = max(set(glyph_votes), key=glyph_votes.count) if glyph_votes else None
    chord_f = inferred[0] if inferred and inferred[2] >= 0.7 else None
    if glyph_f is not None and (not blurry or chord_f is None or chord_f == glyph_f):
        rep.old_fifths, rep.key_source = glyph_f, "image"
    elif chord_f is not None:
        rep.old_fifths, rep.key_source = chord_f, "chords"
    else:
        if ai_t is not None:
            ai = ai_t.fifths
        else:
            ai = ask_key(provider, page, systems[0].staff, work) if provider != "none" else None
        if ai is not None:
            rep.old_fifths, rep.key_source = ai, label
        else:
            votes = [r[4][0] for r in read if r[4]] or [0]
            rep.old_fifths, rep.key_source = max(set(votes), key=votes.count), "estimate"
    if rep.key_source != "image":
        rep.notes_.append(f"key signature {MAJOR_BY_FIFTHS[rep.old_fifths + 6]} from {rep.key_source}")
    # 지우고 그릴 자리: 글자를 믿을 때는 글자 상자, 아니면 음자리표와 박자표 사이 통째(투영 상자), 그것도 없으면 글자 상자
    keysigs: list[tuple[int, list, int, int, bool]] = []
    for kf, kboxes, kstart, kend, est in read:
        if kboxes and (rep.key_source == "image" or est is None):
            keysigs.append((kf, kboxes, kstart, kend, True))
        elif est:
            # 투영 상자는 조표와 박자표가 한 덩어리로 붙은 그림에서 너무 좁게 잡히기도 한다(4♭ 중 1개 자리). 글자 수를 알면
            # 그만큼(글자당 0.8il)은 지운다
            x0, y0, x1, y1 = est[1]
            x1 = max(x1, x0 + int((0.3 + 0.8 * abs(rep.old_fifths)) * il))
            keysigs.append((est[0], [(x0, y0, x1, y1)], est[2], max(est[3], x1), False))
        else:
            keysigs.append((kf, kboxes, kstart, kend, False))
    rep.new_fifths = target_fifths(rep.old_fifths, semitones)
    rep.steps = diatonic_steps(rep.old_fifths, rep.new_fifths)
    old_alters, new_alters = key_alters(rep.old_fifths), key_alters(rep.new_fifths)
    # 그림 자체의 ♯/♭ 글자: 새 조표와 임시표를 같은 글꼴로 찍는다 (같은 종류일 때). 글자 중심과 자리의 y 차이도 같이 둔다
    samples: dict[str, tuple[np.ndarray, float]] = {}
    old_spacing = None
    for sys_, (kf, kboxes, _, _, real) in zip(systems, keysigs):
        if real and kf == rep.old_fifths and kboxes and rep.old_fifths != 0 and not samples:
            kind, steps = ("sharp", TREBLE_SHARP_STEPS) if kf > 0 else ("flat", TREBLE_FLAT_STEPS)
            b = kboxes[0]
            samples[kind] = (harvest_glyph(ink, b), (b[1] + b[3]) / 2 - (sys_.staff.bottom - steps[0] * il / 2))
            if len(kboxes) >= 2:
                old_spacing = (kboxes[1][0] + kboxes[1][2] - kboxes[0][0] - kboxes[0][2]) / 2

    plans_all: list[tuple[System, list[HeadPlan]]] = []
    for sys_, syms, (kf, kboxes, kstart, kend, real) in zip(systems, symbols, keysigs):
        st = sys_.staff
        t_line = line_thickness(ink, st)
        heads = list(syms.noteheads)
        min_x = max(min(kend + 0.2 * il, st.left + 9 * il), st.left + 3 * il)
        heads += find_hollow_heads(page.binary, sys_, il, heads, min_x)
        heads += find_whole_notes(page.binary, sys_, il, heads, min_x)
        heads.sort(key=lambda h: h.x)
        bars = sorted(syms.barlines)
        # 2. 머리마다: 자리, 왼쪽 틈의 임시표, 옛 음 → 새 음 → 새 임시표
        plans: list[HeadPlan] = []
        state_old = dict(old_alters)             # (글자, 옥타브)별 현재 변화 — 마디마다 초기화. 글자만으로 둔다 (옥타브 무시)
        state_new = dict(new_alters)
        bar_i = 0
        prev_right = st.left + int(2 * il)
        for h in heads:
            while bar_i < len(bars) and bars[bar_i] < h.x:          # 마디줄을 지나면 임시표 상태 초기화
                state_old, state_new = dict(old_alters), dict(new_alters)
                bar_i += 1
                prev_right = int(bars[bar_i - 1]) + 2
            step_old = int(round((st.bottom - h.y) / (il / 2)))
            hollow = bool(h.evidence and h.evidence.hollow)
            plan = HeadPlan(h, step_old, step_old + rep.steps, hollow)
            if h.evidence and h.evidence.dot:
                plan.dot = find_dot(ink, h, il)
            gap_x0, gap_x1 = max(prev_right, int(h.x - 2.6 * il)), int(h.x - h.w / 2 - 0.12 * il)
            if gap_x1 - gap_x0 >= 0.8 * il:
                if (m := match_accidental(ink, gap_x0, gap_x1, h.y, il)):
                    plan.acc_old = (m[0], m[1])
                    rep.accidentals_found += 1
            letter, octave = step_pitch(step_old)
            if plan.acc_old:
                state_old[letter] = {"sharp": 1, "flat": -1, "natural": 0}[plan.acc_old[0]]
            alter_old = state_old.get(letter, 0)
            new_letter, new_octave = step_pitch(plan.step_new)
            alter_new = midi(letter, octave, alter_old) + semitones - midi(new_letter, new_octave, 0)
            if abs(alter_new) >= 2:                                   # 겹임시표가 나오면 이웃 글자로 (드물다)
                plan.step_new += 1 if alter_new > 0 else -1
                new_letter, new_octave = step_pitch(plan.step_new)
                alter_new = midi(letter, octave, alter_old) + semitones - midi(new_letter, new_octave, 0)
            if alter_new != state_new.get(new_letter, 0):
                plan.acc_new = {1: "sharp", -1: "flat", 0: "natural"}.get(alter_new)
                state_new[new_letter] = alter_new
                rep.accidentals_drawn += 1
            plans.append(plan)
            prev_right = int(h.x + h.w / 2)
        plans_all.append((sys_, plans))
        rep.heads += len(plans)

        # 3. 지우기: 머리, 덧줄, 옛 임시표, 기둥의 옮길 구간, 옛 조표
        for p in plans:
            h = p.head
            pad = 2
            erase(img, (int(h.x - h.w / 2) - pad, int(h.y - h.h / 2) - pad, int(h.x + h.w / 2) + pad + 1, int(h.y + h.h / 2) + pad + 1), st, t_line)
            # 덧줄: 오선 밖 줄 자리의 짧은 선을 머리 폭 안에서 지운다
            if p.step_old > 8 or p.step_old < 0:
                rng = range(10, p.step_old + 2, 2) if p.step_old > 8 else range(-2, p.step_old - 2, -2)
                for s in rng:
                    y = int(round(st.bottom - s * il / 2))
                    erase(img, (int(h.x - h.w / 2 - 0.5 * il), y - t_line - 1, int(h.x + h.w / 2 + 0.5 * il) + 1, y + t_line + 2), st, t_line)
            if p.acc_old:
                bx0, by0, bx1, by1 = p.acc_old[1]
                erase(img, (bx0, by0, bx1, by1), st, t_line)
            if p.dot and p.step_new != p.step_old:
                bx0, by0, bx1, by1 = p.dot
                erase(img, (bx0 - 1, by0 - 1, bx1 + 1, by1 + 1), st, t_line)
            ev = h.evidence
            dy = (p.step_new - p.step_old) * il / 2 * -1             # 위로 가면 음수
            if ev and ev.stem and dy != 0:
                t_stem = max(1, int(round(0.08 * il)))
                sx = ev.stem_x
                if ev.stem_dir < 0:                                   # 위로 뻗는 기둥: 아래 끝이 머리. 머리가 올라가면 아래쪽을 지운다
                    if dy < 0:
                        erase(img, (sx - t_stem - 1, int(h.y + dy), sx + t_stem + 2, int(h.y + h.h / 2) + 1), st, t_line)
                else:                                                 # 아래로 뻗는 기둥: 위 끝이 머리. 머리가 내려가면 위쪽을 지운다
                    if dy > 0:
                        erase(img, (sx - t_stem - 1, int(h.y - h.h / 2), sx + t_stem + 2, int(h.y + dy) + 1), st, t_line)
        for b in kboxes:                                                   # 글자 상자는 흐린 가장자리 획(♯의 왼쪽 세로획)을 놓치기도 해 여유를 둔다
            erase(img, (b[0] - int(0.15 * il), b[1] - int(0.1 * il), b[2] + int(0.15 * il), b[3] + int(0.1 * il)), st, t_line)

        # 4. 그리기: 새 조표, 머리, 덧줄, 기둥 연장, 임시표
        new_kind = "sharp" if rep.new_fifths > 0 else "flat"
        draw_key_signature(img, st, rep.new_fifths, kstart, kend, il, samples.get(new_kind), old_spacing)
        filled_w, filled_h = head_shape(ink, heads, st, t_line, il)       # 지우기 전 잉크에서 잰다 (ink는 page.binary라 그대로다)
        for p in plans:
            h = p.head
            y_new = st.bottom - p.step_new * il / 2
            w, hh = filled_w, filled_h
            draw_ledgers(img, st, p.step_new, h.x, w, t_line)
            ev = h.evidence
            if ev and ev.stem:
                t_stem = max(1, int(round(0.08 * il)))
                sx = ev.stem_x
                if ev.stem_dir < 0 and y_new > h.y:                   # 위로 뻗는 기둥인데 머리가 내려갔다: 아래로 잇는다
                    cv2.line(img, (sx, int(h.y)), (sx, int(y_new)), 0, t_stem)
                elif ev.stem_dir > 0 and y_new < h.y:                 # 아래로 뻗는 기둥인데 머리가 올라갔다: 위로 잇는다
                    cv2.line(img, (sx, int(y_new)), (sx, int(h.y)), 0, t_stem)
            draw_head(img, h.x, y_new, w, hh, p.hollow, il)
            if p.dot and p.step_new != p.step_old:                     # 점은 칸에 둔다: 줄 위 음이면 바로 위 칸
                bx0, by0, bx1, by1 = p.dot
                dot_step = p.step_new if p.step_new % 2 else p.step_new + 1
                stamp(img, harvest_glyph(ink, p.dot), (bx0 + bx1) / 2, st.bottom - dot_step * il / 2)
            if p.acc_new:
                if (smp := samples.get(p.acc_new)):
                    stamp(img, smp[0], h.x - w / 2 - 0.75 * il, y_new + smp[1])
                else:
                    draw_glyph(img, p.acc_new, h.x - w / 2 - 0.75 * il, y_new, il)

    # 5. 코드 글자: Vision이 읽은 코드 상자를 Swift 이조기로 옮겨 같은 글꼴로 다시 쓴다
    try:
        rep.chords = _rewrite_chords(page, semitones, img, chord_boxes)
    except Exception as e:                                            # 코드가 안 되어도 음표는 낸다
        rep.notes_.append(f"chords not rewritten: {str(e).splitlines()[0][:120]}")

    final = compose_output(page, img, src)
    out.parent.mkdir(parents=True, exist_ok=True)
    ext = out.suffix.lower() if out.suffix.lower() in (".png", ".jpg", ".jpeg") else ".png"
    params = [cv2.IMWRITE_JPEG_QUALITY, 93] if ext in (".jpg", ".jpeg") else []
    ok, buf = cv2.imencode(ext, final, params)
    if not ok:
        raise RuntimeError("could not encode the image")
    buf.tofile(str(out))
    if debug_dir:
        from . import debug
        dbg = debug.canvas(page.gray)
        for sys_, plans in plans_all:
            st = sys_.staff
            for p in plans:
                y_new = int(st.bottom - p.step_new * il / 2)
                cv2.arrowedLine(dbg, (int(p.head.x), int(p.head.y)), (int(p.head.x), y_new), (0, 0, 220), 1, tipLength=0.3)
                if p.acc_old:
                    b = p.acc_old[1]
                    cv2.rectangle(dbg, (b[0], b[1]), (b[2], b[3]), (0, 140, 255), 1)
                if p.acc_new:
                    cv2.putText(dbg, p.acc_new[0], (int(p.head.x) - 14, y_new - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 0, 120), 1)
        debug.caption(dbg, f"{page.name}  in-place {rep.keys}  steps {rep.steps:+d}  heads {rep.heads}  accidentals {rep.accidentals_found}→{rep.accidentals_drawn}")
        debug.save(debug_dir / f"{page.name}-9-inplace.png", dbg)
    return rep


def snap_to_blobs(page, systems: list[System], rows, il: float):
    """hybrid.merge_chords가 준 상자는 Vision 중심 둘레의 작은 상자(x−0.3il…x+0.9il)라 옛 글자를 다 못 지운다(A♭의 윗첨자, Fm의 F).
    코드 띠의 잉크 덩어리(find_text_blobs: 윗첨자까지 뭉친 상자) 중 그 자리에 있는 것으로 바꾼다. Vision 상자가 있던 것(both)을
    먼저 붙이고, AI만 읽은 코드는 남은 덩어리에만 붙인다 (앞 오선 끝의 코드가 다음 오선 앞에 놓이는 일이 있어, 남의 덩어리를
    뺏지 않게). 덩어리가 없으면 그대로 두고, 그리기는 잉크가 없어 건너뛴다."""
    for sys_, row in zip(systems, rows):
        y0, y1 = sys_.chord_band
        blobs = [(bx0, y0 + by0, bx1, y0 + by1) for bx0, by0, bx1, by1 in text.find_text_blobs(page.binary[y0:y1], il)]
        taken: set[int] = set()
        for src in ("both", "claude"):
            for b in row:
                if b.src != src or not blobs:
                    continue
                cx = b.x0 + 0.3 * il
                near = [k for k, bb in enumerate(blobs) if k not in taken and bb[0] - 0.5 * il <= cx <= bb[2] + 0.5 * il]
                if near:
                    k = min(near, key=lambda k: abs((blobs[k][0] + blobs[k][2]) / 2 - cx))
                    taken.add(k)
                    b.x0, b.y0, b.x1, b.y1 = blobs[k]
    return rows


def compose_output(page, img: np.ndarray, src: Path) -> np.ndarray:
    """작업은 ingest가 키운 쪽에서 했지만 결과는 원본 크기로 낸다: 바뀐 자리만 줄여서 원본 위에 얹는다 (가사·제목은 원본 픽셀 그대로).
    원본이 색이 있고 기울기·원근 보정이 없었으면 색 원본 위에 얹는다 (바뀐 자리는 회색)."""
    changed = cv2.dilate((img != page.gray).astype(np.uint8), np.ones((5, 5), np.uint8))
    base = page.base if page.base is not None else page.gray
    h, w = base.shape[:2]
    if (h, w) != img.shape[:2]:
        img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
        changed = cv2.resize(changed, (w, h), interpolation=cv2.INTER_AREA)
    mask = changed > 0
    color = None
    if page.angle == 0 and src.suffix.lower() != ".pdf":
        raw = cv2.imdecode(np.fromfile(src, np.uint8), cv2.IMREAD_COLOR)
        if raw is not None and raw.shape[:2] == (h, w) and (raw[:, :, 0] != raw[:, :, 1]).any():
            color = raw
    if color is None:
        final = base.copy()
        final[mask] = img[mask]
        return final
    final = color.copy()
    final[mask] = img[mask][:, None]
    return final


def _rewrite_chords(page, semitones: int, img: np.ndarray, boxes) -> int:
    from . import chordoverlay
    from .chordoverlay import Chord
    chords = [Chord(b.x0, b.y0, b.x1, b.y1, b.text, "vision", col=k) for k, row in enumerate(boxes) for b in row]
    if not chords:
        return 0
    pieces = [p for c in chords for p in c.text.split("-")]
    moved, _ = chordoverlay._transpose_tokens(pieces, semitones)
    k = 0
    for c in chords:
        n_ = len(c.text.split("-"))
        c.new = "-".join(moved[k:k + n_])
        k += n_
    ink = (page.binary < 128).astype(np.uint8)
    rgb = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
    font = chordoverlay.pick_font(ink, chords)
    rgb = chordoverlay.draw(rgb, ink, chords, font, uniform=True, erase_box=page.scale > 1.5)
    img[:] = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    return sum(1 for c in chords if c.new and c.new != c.text)
