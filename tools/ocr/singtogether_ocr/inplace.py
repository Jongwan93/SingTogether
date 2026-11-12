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
import re
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
    arcs: int = 0                                 # 옮긴 이음줄·붙임줄 수
    key_source: str = "image"
    chord_source: str = "Apple Vision"
    notes_: list[str] = field(default_factory=list)
    # 검수(audit)용 내부 상태: 작업 크기의 결과 그림, 쪽, 오선, 오선별 계획·마디줄·조표 끝, 그린 코드 (JSON에는 안 나간다)
    state: dict = field(default_factory=dict, repr=False)

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
    """오선 줄 두께(px): 줄 자리 ±6행 중 폭의 절반 넘게 검은 행의 수, 다섯 줄의 중앙값. (±3으로 재면 4배로 키운 흐린 캡처의
    7px 줄을 4로 재서 줄 빼기가 줄을 반만 지웠다 — 속 빈 머리의 구멍이 둘로 갈라졌다.)"""
    rows = []
    for y in staff.lines:
        n = 0
        for dy in range(-6, 7):
            yy = y + dy
            if 0 <= yy < ink.shape[0] and ink[yy, staff.left:staff.right].mean() >= 0.5:
                n += 1
        rows.append(n)
    return max(1, int(np.median(rows))) if rows else 1


def strip_staff_lines(region: np.ndarray, line_rows: list[int], t_line: int) -> np.ndarray:
    """영역(0/1)에서 오선 줄 픽셀만 지운다. 줄마다 실제 행 구간을 다시 잰다(빗 자리는 흐린 그림에서 3px까지 어긋나고, 그러면
    머리 안의 줄 토막이 반만 지워져 구멍이 둘로 갈라진다): 빗 자리 ±6행 중 잉크 합이 가장 큰 t_line행 창. 그 구간(±1)의 잉크 중
    구간 바로 바깥(±2행)이 위아래 모두 흰 열을 지운다. 세로로 이어지는 것(기둥, 머리 고리의 옆면)은 남고, 칸에 든 머리의
    고리가 줄에 닿아도 고리 쪽은 바깥이 잉크라 남는다."""
    out = region.copy()
    H, W = region.shape
    sums = region.sum(axis=1)
    zero = np.zeros(W, region.dtype)
    t = max(1, t_line)
    for yy in line_rows:
        lo, hi = max(0, yy - 6), min(H, yy + 7)
        if hi - lo < t:
            continue
        starts = range(lo, hi - t + 1)
        top = max(starts, key=lambda s: sums[s:s + t].sum())
        bot = top + t - 1
        above = region[top - 2] if top - 2 >= 0 else zero
        below = region[bot + 2] if bot + 2 < H else zero
        clear = (above == 0) & (below == 0)
        for y in range(max(0, top - 1), min(H, bot + 2)):
            out[y][clear] = 0
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


def head_shape(ink: np.ndarray, heads: list[Notehead], staff: Staff, t_line: int, il: float) -> tuple[float, float] | None:
    """그림의 머리 크기 (폭, 높이): 오선 줄 위에 앉은, 기둥 달린 속 찬 머리들의 잉크에서 줄과 기둥 열을 빼고 잰 너비·높이의 중앙값.
    악보 글꼴마다 머리 크기가 달라서(Leipzig는 1.2il × 0.85il, 표준은 1.2 × 0.82) 정해 두면 새 머리가 작거나 커 보인다.
    검출 상자(h.w, h.h)는 기둥 밑동·안티에일리어싱이 붙어 더 크다. 칸에 든 머리는 위아래 줄이 가장자리에 붙어 줄 빼기로 안
    빠지고(위아래가 머리 잉크다) 높이를 부풀리므로 줄 위 머리만 잰다 (줄이 머리 한가운데를 지나 잉크에 묻힌다); 덧줄 음은 덧줄이
    머리 밖으로 나와 폭을 부풀리므로 뺀다.
    흐린 캡처의 머리는 번져서 1.9 × 1.6il쯤 되어 위 창(±0.8il)과 한도(1.3il)에 다 걸린다 (13곡 중 11곡이 기본값 1.2 × 0.82il로
    떨어져 새 머리가 원본의 2/3 크기로 그려졌다). 그러면 넓은 창에서 머리보다 가는 줄·기둥을 열림 연산으로 지우고 가운데를 품은
    덩어리를 잰다. 그래도 잴 머리가 없으면 None (부르는 쪽이 쪽의 다른 오선에서 잰 크기를 쓴다)."""
    H, W = ink.shape
    ws, hs, bws, bhs = [], [], [], []
    k = max(3, int(round(0.5 * il)) | 1)
    disk = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
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
        if not (rows.size < 0.5 * il or cols.size < 0.6 * il or rows.size > 1.3 * il or cols.size > 1.5 * il):
            ws.append(float(cols[-1] - cols[0] + 1))
            hs.append(float(rows[-1] - rows[0] + 1))
        # 흐린 머리: 넓은 창, 열림 뒤 가운데 덩어리. 머리를 지나는 줄은 양옆에 남으므로 폭은 몸통 높이의 35%가 넘는 열만 센다
        # (타원은 그 기준에서 93.7%가 남는다). 머리에 닿은 위아래 줄은 가운데 열 위아래에 혹으로 남으므로(이 세상: 높이 2.0il)
        # 높이는 가운데에서 폭의 30%만큼 비킨 두 열에서 재서 되돌린다 (타원은 거기서 높이의 80%)
        x0, x1 = int(round(h.x - 1.4 * il)), int(round(h.x + 1.4 * il)) + 1
        y0, y1 = int(round(h.y - 1.2 * il)), int(round(h.y + 1.2 * il)) + 1
        if x0 < 0 or y0 < 0 or x1 > W or y1 > H:
            continue
        body = cv2.morphologyEx(ink[y0:y1, x0:x1].astype(np.uint8), cv2.MORPH_OPEN, disk)
        _, lab, st_, _ = cv2.connectedComponentsWithStats(body, 8)
        c = lab[int(round(h.y)) - y0, int(round(h.x)) - x0]
        if not c:
            continue
        mask = lab == c
        cols = np.flatnonzero(mask.sum(axis=0) >= 0.35 * st_[c][3])
        if not cols.size:
            continue
        bw = float(cols.size) / 0.937
        mid = (cols[0] + cols[-1]) / 2
        bh = float(np.mean([mask[:, int(round(mid + d * bw))].sum() for d in (-0.3, 0.3)])) / 0.8
        if 0.5 * il <= bh <= 2.0 * il and 0.6 * il <= bw <= 2.4 * il:
            bws.append(bw)
            bhs.append(float(bh))
    if len(ws) >= 2:
        return float(np.median(ws)), float(np.median(hs))
    if len(bws) >= 2:
        return float(np.median(bws)), float(np.median(bhs))
    return None


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


def classify_accidental(mask: np.ndarray) -> str | None:
    """잉크 덩어리(오선 줄 뺀 것)가 ♯·♭·♮ 중 무엇인가, 세로획 배치로 가린다 (글꼴에 안 매인다):
    ♯은 높이의 3/4 넘게 뻗는 세로획이 둘, ♭은 하나이고 왼쪽 끝에, ♮은 그런 획은 없지만 절반 넘게 뻗는 획이 둘(왼쪽 위·오른쪽 아래)."""
    h, w = mask.shape
    if h < 4 or w < 3:
        return None
    col_h = mask.sum(axis=0)

    def groups(th: float) -> list[tuple[int, int]]:
        on = col_h >= th * h
        out, start = [], None
        for x, v in enumerate(list(on) + [False]):
            if v and start is None:
                start = x
            elif not v and start is not None:
                out.append((start, x - 1))
                start = None
        return out

    tall = groups(0.75)
    if len(tall) >= 2:
        return "sharp"
    if len(tall) == 1:                                     # ♭: 세로획이 왼쪽 끝에, 그 오른쪽에 고리 (획만 있는 기둥 토막은 아니다)
        s, e = tall[0]
        return "flat" if s <= 0.3 * w and e - s + 1 <= 0.5 * w else None
    return "natural" if len(groups(0.5)) == 2 else None


def match_accidental(ink: np.ndarray, x0: int, x1: int, cy: float, il: float, staff: Staff, t_line: int
                     ) -> tuple[str, tuple[int, int, int, int], float] | None:
    """머리 왼쪽 틈(x0..x1)의 임시표: 오선 줄을 뺀 잉크 덩어리 중 임시표 크기(높이 1.6–3.4il, 폭 0.3–1.2il)이고 머리 높이에
    있는(♯·♮은 머리 가운데, ♭은 고리가 머리라 중심이 0.45il 위) 것을 모양으로 가린다. 머리에 가장 가까운 것 하나.
    (종류, 상자, 1.0) 또는 None. 합성 템플릿 상관은 실제 글꼴과 0.1–0.3밖에 안 맞아 못 쓴다."""
    H, W = ink.shape
    y0, y1 = max(0, int(cy - 2.2 * il)), min(H, int(cy + 2.2 * il))
    x0, x1 = max(0, x0), min(W, x1)
    if x1 - x0 < 0.3 * il or y1 - y0 < 1.6 * il:
        return None
    region = ink[y0:y1, x0:x1].astype(np.uint8)
    cut = strip_staff_lines(region, [y - y0 for y in staff.lines if y0 <= y < y1], t_line)
    n, labels, stats, cents = cv2.connectedComponentsWithStats(cut, connectivity=8)
    best = None
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if not (1.6 * il <= h <= 3.4 * il and 0.3 * il <= w <= 1.2 * il):
            continue
        ccy = y0 + cents[i][1]
        kind = classify_accidental(labels[y:y + h, x:x + w] == i)
        if kind is None:
            continue
        expect = cy - (0.45 * il if kind == "flat" else 0)
        if abs(ccy - expect) > 0.6 * il:
            continue
        if best is None or x > best[1][0] - x0:
            best = (kind, (x0 + int(x), y0 + int(y), x0 + int(x + w), y0 + int(y + h)), 1.0)
    if best is None:
        return None
    # 흐린 가장자리 획(♭ 세로획의 꼭대기)은 덩어리에서 떨어지기도 한다: 틈 안에서 상자를 조금 넓혀 지우고 거둔다
    kind, (bx0, by0, bx1, by1), _ = best
    top = int(0.5 * il if kind == "flat" else 0.3 * il)
    return kind, (max(x0, bx0 - int(0.12 * il)), max(y0, by0 - top), min(x1, bx1 + int(0.12 * il)), min(y1, by1 + int(0.15 * il))), 1.0


def erase_key_glyphs(img: np.ndarray, ink: np.ndarray, staff: Staff, t_line: int, il: float, x0: int, x1: int, kind: str) -> int:
    """x0..x1의 조표 자리에서 kind(♯/♭) 모양의 잉크 덩어리를 지운다. 붙어 버린 ♯♯은 세로획이 넷이라 그대로 ♯으로 가려지고,
    붙은 ♭♭(폭 0.9il 넘음)은 키 2.4il 이상이면 받는다 (박자표 숫자는 2il쯤이고, 8은 왼쪽 곡선 때문에 ♭처럼 보인다). 지운 수."""
    H, W = ink.shape
    y0, y1 = max(0, int(staff.top - 2.2 * il)), min(H, int(staff.bottom + 2.2 * il))
    x0, x1 = max(0, x0), min(W, x1)
    if x1 - x0 < 0.3 * il:
        return 0
    cut = strip_staff_lines(ink[y0:y1, x0:x1].astype(np.uint8), [y - y0 for y in staff.lines if y0 <= y < y1], t_line)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(cut, connectivity=8)
    erased = 0
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if not (1.4 * il <= h <= 3.6 * il and 0.25 * il <= w <= 2.5 * il):
            continue
        c = classify_accidental(labels[y:y + h, x:x + w] == i)
        ok = c == kind and (kind == "sharp" or w <= 0.9 * il)
        if not ok and kind == "flat" and c is not None and w > 0.9 * il and h >= 2.4 * il:
            ok = True
        if ok:
            erase(img, (x0 + int(x) - 1, y0 + int(y) - 1, x0 + int(x + w) + 1, y0 + int(y + h) + 1), staff, t_line)
            erased += 1
    return erased


def ledger_count(ink: np.ndarray, staff: Staff, x: float, il: float, t_line: int, below: bool) -> int:
    """x에서 오선 아래(또는 위)로 이어진 덧줄 수. 덧줄 = 그 줄 자리의 행 띠에서 x를 지나는 잉크 런이 1.45–3il (머리만이면 1.3il
    아래, 빔·이음줄은 더 길다). 첫 덧줄이 없으면 0."""
    H, W = ink.shape
    xi, n = int(round(x)), 0
    for k in range(1, 5):
        y = int(round(staff.bottom + k * il)) if below else int(round(staff.top - k * il))
        if not (t_line + 1 <= y < H - t_line - 1):
            break
        row = ink[y - t_line // 2 - 1:y + t_line // 2 + 2, :].any(axis=0)
        if not row[xi]:
            break
        l = r = xi
        while l - 1 >= 0 and row[l - 1]:
            l -= 1
        while r + 1 < W and row[r + 1]:
            r += 1
        if not 1.45 * il <= r - l + 1 <= 3.0 * il:
            break
        # 덧줄은 줄 두께다; 빔(0.4il 두께)이 같은 자리를 지나도 길이는 맞는다. 머리 바깥쪽(±0.9il) 열에서 그 행을 지나는 세로 잉크
        # 런이 한쪽이라도 줄 두께의 1.5배 안이어야 한다
        thin = False
        for xo in (xi - int(0.9 * il), xi + int(0.9 * il)):
            if not 0 <= xo < W or not ink[y, xo]:
                continue
            a = b = y
            while a - 1 >= 0 and ink[a - 1, xo]:
                a -= 1
            while b + 1 < H and ink[b + 1, xo]:
                b += 1
            if b - a + 1 <= 1.5 * t_line + 2:
                thin = True
        if not thin:
            break
        n += 1
    return n


def ledger_snap(ink: np.ndarray, staff: Staff, heads: list[Notehead], il: float, t_line: int, binary: np.ndarray | None = None) -> int:
    """작은 캡처에서 덧줄 둘 이상 아래(위)의 머리는 덧줄과 번져 한 칸 위로 읽힌다(주님은 산 같아서 6.3px: A3가 B3로). 덧줄이
    n개면 머리는 n번째 덧줄 위(−2n)나 그 아래 칸(−2n−1)뿐이니 거기로 옮긴다. n이 2 이상일 때만: 첫 덧줄(C4/D4 경계)은
    머리 밑이 줄 자리에 닿아 세기가 틀리고, 정답 대조에서 거기 손대면 7곡 중 셋이 나빠졌다 (n≥2는 바뀌는 게 그 악보뿐)."""
    moved = 0
    for h in heads:
        step = int(round((staff.bottom - h.y) / (il / 2)))
        if -2 <= step <= 10:                                   # 첫 덧줄 언저리는 손대지 않는다
            continue
        below = step < 0
        n = ledger_count(ink, staff, h.x, il, t_line, below)
        if n < 2:
            continue
        allowed = (-2 * n, -2 * n - 1) if below else (8 + 2 * n, 9 + 2 * n)
        if step in allowed:
            continue
        best = min(allowed, key=lambda a: abs(a - step))
        if abs(best - step) <= 1.5:
            h.y = staff.bottom - best * il / 2
            if binary is not None:                             # 칸에 있다고 본 머리는 가운데가 희어 속 빈 머리로 읽혔다: 자리를 옮겼으니 다시 본다
                h.evidence = notes.duration_evidence(binary, h, il)
            moved += 1
    return moved


def clef_keep_x(cut: np.ndarray, lab: np.ndarray, stt, clef_i: int, bx0: int, y0: int, staff: Staff, il: float) -> int | None:
    """흐린 캡처에서 조표가 음자리표에 붙어 한 덩어리가 되면, 음자리표를 어디까지 남길지 구멍으로 정한다: 음자리표의 큰 고리 구멍
    (높이 1.4il 넘는 것)의 오른쪽 끝 너머에 있는 첫 작은 구멍(♯의 가운데, 폭·높이 0.9il 아래)의 0.45il 왼쪽부터가 조표다.
    작은 구멍이 없으면(♭은 번지면 구멍이 사라진다) 고리 구멍 오른쪽 끝 + 0.45il(획 두께). 고리 구멍이 없으면(깨끗한 그림은 줄을 뺀
    자리에서 고리가 열린다) None = 덩어리를 통째로 남긴다."""
    comp = (lab == clef_i).astype(np.uint8)
    hn, hlab, hst, _ = cv2.connectedComponentsWithStats(1 - comp, connectivity=4)
    Hc, Wc = comp.shape
    loops, small = [], []
    for j in range(1, hn):
        hx, hy, hw, hh, _ = hst[j]
        if hx == 0 or hy == 0 or hx + hw == Wc or hy + hh == Hc:
            continue                                                   # 바깥 배경
        if hh >= 1.4 * il:
            loops.append(hx + hw)
        elif hw <= 0.9 * il and hh <= 0.9 * il and staff.top - 1.8 * il <= y0 + hy <= staff.top + 3 * il:
            small.append(hx)
    if not loops:
        return None
    loop_right = max(loops)
    glyph = [hx for hx in small if hx >= loop_right - 0.1 * il]
    # 첫 작은 구멍이 고리에서 1.2il 넘게 떨어져 있으면 첫 글자가 번져 구멍이 없는 것이다 (주 이름 찬양 6째 줄): 고리 끝 + 획 두께
    if glyph and min(glyph) - loop_right <= 1.2 * il:
        return bx0 + int(min(glyph) - 0.45 * il)
    return bx0 + int(loop_right + 0.45 * il)


def find_clef_end(ink: np.ndarray, staff: Staff, t_line: int, il: float) -> int | None:
    """흐린 캡처에서 조표가 붙은 음자리표의 오른쪽 끝(clef_keep_x). 못 구하면 None."""
    H, W = ink.shape
    y0, y1 = max(0, int(staff.top - 1.7 * il)), min(H, int(staff.bottom + 1.7 * il))
    bx0, bx1 = max(0, staff.left), min(W, staff.left + int(14 * il))
    cut = strip_staff_lines(ink[y0:y1, bx0:bx1].astype(np.uint8), [y - y0 for y in staff.lines], t_line)
    n, lab, stt, _ = cv2.connectedComponentsWithStats(cv2.erode(cut, np.ones((3, 3), np.uint8), iterations=2), connectivity=8)
    for i in range(1, n):
        bx, by, bw, bh, _ = stt[i]
        if bh >= 4 * il and bx0 + bx < staff.left + 3 * il:
            return clef_keep_x(cut, lab, stt, i, bx0, y0, staff, il)
    return None


def cross_shaped(ink: np.ndarray, box: tuple[int, int, int, int]) -> bool:
    """Coda 기호 𝄌처럼 고리를 세로획이 위아래로 길게 꿰뚫는가: 상자 안 가장 큰 덩어리의 구멍 가운데 열에서, 구멍 위와 아래의 잉크가
    각각 덩어리 높이의 20% 넘게(둘 합쳐 45% 넘게) 이어진다. 글자 D·B·A·a·e·9는 구멍 위아래가 획 두께뿐이다."""
    x0, y0, x1, y1 = box
    reg = ink[y0:y1, x0:x1].astype(np.uint8)
    if reg.size == 0 or not reg.any():
        return False
    n, lab, stt, _ = cv2.connectedComponentsWithStats(reg, connectivity=8)
    if n < 2:
        return False
    i = 1 + int(np.argmax(stt[1:, 4]))
    comp = (lab == i).astype(np.uint8)
    bx, by, bw, bh, _ = stt[i]
    hn, hlab, hst, _ = cv2.connectedComponentsWithStats(1 - comp, connectivity=4)
    for j in range(1, hn):
        hx, hy, hw, hh, _ = hst[j]
        if hx == 0 or hy == 0 or hx + hw == comp.shape[1] or hy + hh == comp.shape[0]:
            continue                                                   # 바깥 배경
        if hw < 3 or hh < 3:                                           # 번진 고리의 구멍은 몇 px밖에 안 남는다
            continue
        cx = hx + hw // 2
        col = comp[:, cx]
        above = int(col[by:hy].sum())
        below = int(col[hy + hh:by + bh].sum())
        if above >= 0.2 * bh and below >= 0.2 * bh and above + below >= 0.5 * bh:
            return True
    return False


def erase_key_area(img: np.ndarray, ink: np.ndarray, staff: Staff, t_line: int, il: float, x0: int, x1: int) -> None:
    """음자리표 끝(x0)부터 x1까지의 옛 조표(와 박자표)를 지운다: 남길 것을 빼고 전부. 남기는 것은 음자리표(4il 넘게 키 큰 덩어리;
    조표가 거기 붙어 있으면 clef_keep_x 왼쪽만), 오선 위 코드 글자(바닥이 윗줄 위 0.3il보다 높은 덩어리: 찬양의 열기의 D),
    x1 너머에 중심이 있는 쉼표·첫 음. 덩어리는 오선 줄을 뺀 띠를 2px 깎아 가르고(붙은 글자들의 다리가 끊긴다) 도로 불린다;
    깎여 사라지는 가는 획(4/4의 사선)은 어느 덩어리도 아니라 지워진다. 오선 줄 행은 이웃 열의 값으로 잇는다."""
    H, W = ink.shape
    y0, y1 = max(0, int(staff.top - 1.7 * il)), min(H, int(staff.bottom + 1.7 * il))
    bx0, bx1 = max(0, min(staff.left, int(x0 - 3 * il))), min(W, int(x1 + 3 * il))   # 음자리표가 통째로 들게 (구멍이 띠 가장자리에 닿으면 못 센다)
    band = ink[y0:y1, bx0:bx1].astype(np.uint8)
    cut = strip_staff_lines(band, [y - y0 for y in staff.lines], t_line)
    k = np.ones((3, 3), np.uint8)
    n, lab, stt, _ = cv2.connectedComponentsWithStats(cv2.erode(cut, k, iterations=2), connectivity=8)
    # 지울 자리 안의 마디줄(도돌이표 포함: 키 3.5il 넘고 폭 0.35il 아래)에서 멈춘다 — 주 이름 찬양 6째 줄은 조표 뒤가 바로 도돌이표다
    for i in range(1, n):
        bx, by, bw, bh, _ = stt[i]
        if bh >= 3.5 * il and bw <= 0.35 * il and x0 <= bx0 + bx + bw / 2 < x1:
            x1 = min(x1, bx0 + bx - int(0.4 * il))
    keep_px = np.zeros_like(cut)
    ex0 = x0
    # 오선 줄을 지우기 전의 덩어리: 줄에 닿은 것은 줄과 함께 한 덩어리(키 큼)가 된다 — 윗줄 위로 솟은 ♯의 윗부분을 코드 글자와 가른다
    _, lab_full, stt_full, _ = cv2.connectedComponentsWithStats(band, connectivity=8)
    for i in range(1, n):
        bx, by, bw, bh, _ = stt[i]
        cx = bx0 + bx + bw / 2
        if bx0 + bx < x0 - 0.5 * il and bx0 + bx + bw > x0 + 0.3 * il:   # 왼쪽(음자리표)에서 지울 자리로 뻗어 든 덩어리: 음자리표에 붙은 조표
            # 띠가 위아래 1.7il에서 잘려 음자리표는 키로 못 알아본다; 구멍으로 끝을 재고(없으면 x0) 그 왼쪽만 남긴다
            kx = clef_keep_x(cut, lab, stt, i, bx0, y0, staff, il)
            kx = x0 if kx is None else max(kx, x0 - int(0.5 * il))
            keep_px[(lab == i) & (np.arange(cut.shape[1])[None, :] + bx0 < kx)] = 1
            ex0 = min(ex0, kx)
        elif bx0 + bx < x0 and x0 <= cx < x1:
            # 음자리표 끝 어림(x0)보다 조금(0.5il 안) 왼쪽에서 시작하는 조표 덩어리: 음자리표와 따로 떨어져 있으니 통째로 지운다.
            # 위 규칙으로 가면 ♯의 구멍을 음자리표 끝으로 알고 그 왼쪽 ♯을 남겼다 (하나님은 너를 첫 줄: ♯♯♯♯와 4가 한 덩어리)
            ex0 = min(ex0, bx0 + bx)
        elif not (x0 <= cx < x1):                                     # 지울 자리 밖에 중심이 있는 것
            keep_px[lab == i] = 1
        elif y0 + by + bh <= staff.top - 0.3 * il:                     # 코드 글자: 줄에 안 닿은 것만 (♯의 윗부분은 줄에 닿는다)
            full = lab_full[lab == i]
            full = full[full > 0]
            if full.size and stt_full[int(np.bincount(full).argmax())][3] < 2.5 * il:
                keep_px[lab == i] = 1
    protected = cv2.dilate(keep_px, k, iterations=4) > 0
    cols = np.zeros(cut.shape[1], bool)
    cols[max(0, ex0 - bx0):max(0, x1 - bx0)] = True
    # 잉크로 안 잡힌 회색 테두리(번진 글자 가장자리)도 함께: 보호 대상 둘레 4px 밖은 전부
    mask = ~protected & cols[None, :]
    keep = np.zeros(y1 - y0, bool)
    for y in staff.lines:
        a, b = max(y0, y - t_line // 2 - 1), min(y1, y + (t_line + 1) // 2 + 1)
        if b > a:
            keep[a - y0:b - y0] = True
    region = img[y0:y1, bx0:bx1]
    region[mask & ~keep[:, None]] = 255
    # 줄 행: 지우는 열을 상자 좌우 바깥(열 6개씩)의 중앙값으로 — erase()와 같은 방식
    for r in np.flatnonzero(keep):
        cs = np.flatnonzero(mask[r])
        if cs.size:
            around = np.concatenate([img[y0 + r, max(0, ex0 - 6):ex0], img[y0 + r, x1:min(W, x1 + 6)]])
            region[r, cs] = int(np.median(around)) if around.size else 255


def draw_time_signature(img: np.ndarray, staff: Staff, beats: int, beat_type: int, x: int, il: float, scale: float = 1.0) -> int:
    """박자표 숫자 둘을 x부터 쓴다 (위 숫자 바닥 = 가운데 줄, 아래 숫자 바닥 = 맨 아래 줄, 높이 2il×scale). 오른쪽 끝 x."""
    from .chordoverlay import FONTS, _fit_font, _render_mask
    face = FONTS.get("Times Bold", next(iter(FONTS.values())))
    hgt = max(6, int(round(2 * il * scale)) - 2)
    right = x
    for text, base in ((str(beats), staff.lines[2]), (str(beat_type), staff.lines[4])):
        font = _fit_font(face, text, hgt)
        mask = _render_mask(font, text)
        h, w = mask.shape
        stamp(img, ((mask > 128) * 255).astype(np.uint8), x + w / 2, base + 1 - h / 2)
        right = max(right, x + w)
    return right


def after_time_signature(ink: np.ndarray, staff: Staff, il: float, kend: int) -> int:
    """조표 다음 잉크 무리(박자표)의 오른쪽 끝 x. 첫 음의 임시표를 찾을 때 박자표 숫자(6 위에 8)를 ♭으로 보지 않게 건너뛴다.
    박자표가 없으면 첫 음표를 건너뛰게 되는데, 그러면 첫 음은 임시표를 못 찾을 뿐이다."""
    prof = column_profile(ink, staff, il)
    empty = line_thickness(ink, staff) + 3
    x = min(len(prof) - 1, int(kend))
    while x < len(prof):                                   # 잉크 무리들을 0.4il 미만의 틈은 건너뛰며 잇는다 (숫자 안의 빈 열)
        while x < len(prof) and prof[x] > empty:
            x += 1
        gap = x
        while x < len(prof) and prof[x] <= empty and x - gap < 0.4 * il:
            x += 1
        if x >= len(prof) or prof[x] <= empty:
            return gap + 2
    return x


def find_key_signature(ink: np.ndarray, staff: Staff) -> tuple[int, list[tuple[int, int, int, int]], int, int, int]:
    """오선 시작의 조표: (fifths, 글자 상자들, 조표 시작 x, 조표 다음 잉크 x, 음자리표 오른쪽 끝 x). 조표의 k번째 ♯/♭은 자리가
    정해져 있어 그 y에 맞는 덩어리를 세어 종류와 수를 안다. 조표가 없으면 (0, [], 음자리표 오른쪽, 그다음 잉크, 음자리표 오른쪽)."""
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
    # 흐린 캡처에서는 조표·박자표·첫 음까지 음자리표와 한 덩어리로 붙는다(주 이름 찬양: 9il 폭). 높음표의 폭은 높이의 0.37배쯤이라
    # 덩어리 폭 대신 높이로 끝을 어림한다 (덩어리가 그보다 좁으면 그 끝)
    clef_right = staff.left + int(2.5 * il)
    for x, y, w, h, cx, cy in comps:
        if h >= 4 * il and x < staff.left + 3 * il:
            clef_right = max(clef_right, min(x + w, x + int(0.38 * min(h, 7.5 * il))))
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
    return fifths, boxes, start_x, nxt, clef_right


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
        spacing = max(0.5 * il, avail / n)
    # 글자가 더 많아져(3♯→5♯) 0.62il 간격으로도 안 들어가면 글자를 간격에 맞춰 줄인다 (박자표 위에 겹쳐 그리지 않게)
    shrink = min(1.0, spacing / (0.62 * il))
    mask = sample[0] if sample is not None else None
    if mask is not None and shrink < 1:
        mask = cv2.resize(mask, (max(2, int(mask.shape[1] * shrink)), max(2, int(mask.shape[0] * shrink))), interpolation=cv2.INTER_AREA)
        mask = ((mask > 96) * 255).astype(np.uint8)
    for k in range(n):
        cx = start_x + 0.45 * il * shrink + k * spacing
        if mask is not None:
            stamp(img, mask, cx, staff.bottom - steps[k] * il / 2 + sample[1] * shrink)
        else:
            draw_glyph(img, kind, cx, staff.bottom - steps[k] * il / 2 - (0.45 * il if kind == "flat" else 0), il * shrink)


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
        if any(flag_hook(hx, hy, o, il) for o in heads) or rest_tail(ink, hx - staff.left, hy - y0, il):
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


def column_fill(ink: np.ndarray, h: Notehead) -> float:
    """머리 상자 가운데 절반 열에서 가장 긴 세로 잉크 길이의 중앙값 / 머리 높이. 꽉 찬 머리는 1에 가깝고(13곡 1500개 중 가장
    낮은 것 0.85), 머리로 잘못 잡은 빔 토막·꼬리 끝처럼 기울어진 띠는 띠 두께만큼만 이어진다 (0.56–0.72)."""
    y0, y1 = max(0, int(h.y - h.h / 2)), int(h.y + h.h / 2)
    runs = []
    for c in range(max(0, int(h.x - h.w / 4)), min(ink.shape[1], int(h.x + h.w / 4))):
        d = np.diff(np.r_[0, ink[y0:y1, c].astype(np.int8), 0])
        starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        runs.append(int((ends - starts).max()) if starts.size else 0)
    return float(np.median(runs)) / max(1, h.h) if runs else 1.0


FILTER_LOG: list = []          # Vision 상자 거르기 뒤에 남은 (오선, 글자, x0, y1) (디버깅용)
KEY_LOG: list[tuple] = []      # 조표 통째 지우기가 오선마다 (오선 번호, 음자리표 끝, 틈, 지운 끝, 첫 머리, 박자표 다시 씀, 상자)를 남긴다 (디버깅용)
HOLE_LOG: list[tuple] = []     # find_hole_heads가 후보마다 (x, y, 떨어진 검사 또는 "ok", 값)을 남긴다 (디버깅용)


def find_hole_heads(binary: np.ndarray, sys_: System, il: float, heads: list[Notehead], min_x: float, t_line: int) -> list[Notehead]:
    """속 빈 머리를 바깥 모양이 아니라 **안의 흰 구멍**으로 찾는다: 붙임줄·임시표·기둥이 붙어 덩어리 모양이 망가져도(주님은 산
    같아서 13·16마디: ♮이 붙어 폭 2.6il, 붙임줄이 붙어 높이 1.6il) 구멍은 그대로다. 오선 줄을 뺀 잉크의 배경 성분 중 가장자리에
    안 닿는 것이 구멍; 머리 구멍은 폭 0.4–1.05il, 높이 0.28–0.8il, 가로로 긴 타원(상자 채움 0.55–0.92: 빔·기둥·줄이 둘러싼
    직사각형은 0.95쯤, 붙임줄·마디줄·줄의 세모는 0.5 아래). 기둥이 있어야 하고 마디줄 위면 아니다. 머리 상자 = 구멍 + 고리 0.35il."""
    staff = sys_.staff
    H, W = binary.shape
    y0, y1 = max(0, int(staff.top - 1.6 * il)), min(H, int(staff.bottom + 1.6 * il))
    region = (binary[y0:y1, staff.left:staff.right + 1] < 128).astype(np.uint8)
    cut = strip_staff_lines(region, [y - y0 for y in staff.lines], t_line)
    # 구멍은 3×3 열림(깎았다 되돌림)을 거쳐 잰다: 줄을 뺀 자리에 남는 1–2px 틈이 구멍에 붙어 상자를 넓히고 채움을 0.34까지
    # 떨어뜨리는데, 열림은 그 틈만 없애고 구멍 크기는 둔다 (깎기만 하면 작은 구멍이 사라진다)
    holes = cv2.morphologyEx((cut == 0).astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, labels, stats, cents = cv2.connectedComponentsWithStats(holes, connectivity=4)
    border = set(labels[0]) | set(labels[-1]) | set(labels[:, 0]) | set(labels[:, -1])
    # 코드 띠의 글자 덩어리(A의 세모 구멍, D·O의 구멍)는 뺀다. 띠로 올라온 2분음표는 기둥까지 한 덩어리라 글자 덩어리에서 걸러진다
    cb0, cb1 = sys_.chord_band
    text_boxes = [(bx0, cb0 + by0, bx1, cb0 + by1) for bx0, by0, bx1, by1 in text.find_text_blobs(binary[cb0:cb1], il)]
    # 줄 위의 머리는 구멍이 줄로 위아래 둘로 갈라지고, 기울어진 타원이라 두 반쪽이 가로로 어긋나 줄 빼기로도 안 이어진다
    # (주님은 산 같아서 16마디). 줄 띠(두께+3)를 사이에 두고 마주 보며 가로로 겹치는 작은 구멍 둘은 하나로 합친다
    cands = [[int(v) for v in stats[i]] + [float(cents[i][0]), float(cents[i][1])] for i in range(1, n) if i not in border]
    # 오선 줄에 매달린 머리(D4·G5처럼 고리 한쪽이 줄 자체)는 줄을 빼면 구멍이 밖으로 터진다 (주를 위한 이곳에 32마디): 줄을 둔
    # 잉크에서도 구멍을 찾아, 줄에 닿는 것 중 위에서 못 찾은 것만 더한다
    holes2 = cv2.morphologyEx((region == 0).astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n2, lab2, st2, ce2 = cv2.connectedComponentsWithStats(holes2, connectivity=4)
    border2 = set(lab2[0]) | set(lab2[-1]) | set(lab2[:, 0]) | set(lab2[:, -1])
    for i in range(1, n2):
        if i in border2:
            continue
        x_, y_, w_, h_, a_ = (int(v) for v in st2[i])
        # 오선 밖에 매달린 것만: 아랫줄 바로 밑, 윗줄 바로 위 (오선 안의 줄 사이 칸은 기둥·빔과 함께 구멍처럼 닫힌다)
        bot, top = staff.lines[-1] - y0, staff.lines[0] - y0
        touches = abs(y_ - (bot + t_line // 2 + 1)) <= 2 or abs(y_ + h_ - (top - t_line // 2)) <= 2
        gx, gy = ce2[i][0] + staff.left, ce2[i][1] + y0
        near_head = any(abs(gx - h.x) < 0.9 * il and abs(gy - h.y) < 0.9 * il for h in heads)   # 줄 밑에 매달린 꽉 찬 머리와 줄 사이의 쐐기
        if touches and not near_head and not any(abs(ce2[i][0] - c[5]) < 0.5 * il and abs(ce2[i][1] - c[6]) < 0.5 * il for c in cands):
            cands.append([x_, y_, w_, h_, a_, float(ce2[i][0]), float(ce2[i][1])])
    line_rows = {y - y0 for y in staff.lines}
    merged = []
    used: set[int] = set()
    for a in range(len(cands)):
        if a in used:
            continue
        ax, ay, aw, ah, aa, acx, acy = cands[a]
        hit = None
        for b in range(len(cands)):
            if b == a or b in used:
                continue
            bx, by, bw, bh, ba, bcx, bcy = cands[b]
            gap = by - (ay + ah)
            if not (0 <= gap <= t_line + 3) or ah > 0.5 * il or bh > 0.5 * il:
                continue
            overlap = min(ax + aw, bx + bw) - max(ax, bx)
            mid = ay + ah + gap / 2
            # 기울어진 홈은 두 반쪽이 가로로 거의 안 겹친다(1–2px): 0.25il까지 떨어져도 받는다, 합친 폭이 머리 안이면
            if overlap >= -0.25 * il and max(ax + aw, bx + bw) - min(ax, bx) <= 1.05 * il \
                    and any(abs(mid - ly) <= t_line for ly in line_rows):
                hit = b
                break
        if hit is None:
            merged.append((ax, ay, aw, ah, aa, acx, acy))
            continue
        bx, by, bw, bh, ba, bcx, bcy = cands[hit]
        used.add(hit)
        x0_, y0_ = min(ax, bx), ay
        x1_, y1_ = max(ax + aw, bx + bw), by + bh
        area = aa + ba + (aw + bw) / 2 * (by - (ay + ah))                 # 줄이 가리던 가운데도 구멍이다
        merged.append((x0_, y0_, x1_ - x0_, y1_ - y0_, area, (acx * aa + bcx * ba) / (aa + ba), (y0_ + y1_) / 2))
    out = []
    for x, y, w, h, area, cxm, cym in merged:
        cx, cy = cxm, cym
        hx, hy = cx + staff.left, cy + y0
        if w < 0.3 * il or h < 0.2 * il:
            continue
        if any(bx0 <= x + w / 2 + staff.left <= bx1 and by0 <= y + h / 2 + y0 <= by1 for bx0, by0, bx1, by1 in text_boxes):
            HOLE_LOG.append((hx, hy, "text", 0))
            continue
        # 흐린 그림은 고리가 두꺼워 구멍이 세로로 길어지기도 한다(0.48×0.64il): 가로세로비 0.65까지. 깨끗한 악보 글꼴(Bravura)의
        # 2분음표 속은 기울어진 가는 홈이라 상자 채움이 0.48: 채움은 0.4부터
        # 오선 줄이 가로지르는 구멍(줄 위의 기울어진 2분음표)은 줄을 지운 자리만큼 초승달처럼 가늘다: 채움 0.3까지 (주를 위한 이곳에 4마디)
        crossed = any(hy0 - 1 <= ly - y0 <= hy0 + h + 1 for ly in staff.lines for hy0 in [y])
        fill_min = 0.3 if crossed else 0.4
        w_min, h_min = (0.3, 0.2) if crossed else (0.4, 0.28)           # 고리가 두껍게 번지면 틈만 남는다 (주를 위한 이곳에 12마디)
        # 둥근 큰 구멍: 2분음표를 칸보다 큰 동그라미로 그린 악보 (하나님은 너를: 구멍 1.3–1.4 × 1.2–1.3il, 채움 0.63–0.8).
        # 원에 가까워야 한다 (가로세로비 0.8–1.35, 채움은 타원 π/4쯤): 빔·기둥이 둘러싼 네모(채움 0.95)와 붙임줄 틈(얇음)은 안 든다
        fill = area / (w * h)
        big_round = 1.05 * il < w <= 1.6 * il and 0.8 * il < h <= 1.45 * il and 0.8 <= w / h <= 1.35 and 0.6 <= fill <= 0.86
        if not big_round and not (w_min * il <= w <= 1.05 * il and h_min * il <= h <= 0.8 * il and 0.65 <= w / h <= 2.8 and fill_min <= fill <= 0.92):
            HOLE_LOG.append((hx, hy, "size", (round(w / il, 2), round(h / il, 2), round(area / (w * h), 2))))
            continue
        if hx < min_x or not staff.top - 1.2 * il <= hy <= staff.bottom + 1.2 * il:
            HOLE_LOG.append((hx, hy, "range", 0))
            continue
        if big_round:
            # 큰 동그라미는 가는 고리로 둘러싸인다: 꽉 찬 머리 둘과 빔 사이의 틈(모든 능력 4째 줄)은 양옆이 머리라 두껍다.
            # 줄을 뺀 그림에서 가운데 높이 세 줄로 재고 가장 얇은 것을 쓴다 (기둥이 옆에 붙어도 한 줄은 고리만이다)
            def side(r: int, step: int) -> int:
                n_, xx = 0, (x - 1 if step < 0 else x + w)
                while 0 <= xx < cut.shape[1] and cut[r, xx] and n_ <= il:
                    n_, xx = n_ + 1, xx + step
                return n_
            rows_ = [min(cut.shape[0] - 1, int(y + h * f)) for f in (0.35, 0.5, 0.65)]
            ring = max(min(side(r, -1) for r in rows_), min(side(r, 1) for r in rows_))
            # 글자 D·P의 구멍은 한쪽이 곧은 세로선이다; 동그라미는 위아래로 갈수록 가장자리가 안으로 든다 (주 이름 찬양: D)
            hm = holes[y:y + h, x:x + w] > 0
            rows_in = [r for r in range(int(0.1 * h), int(0.9 * h) + 1) if r < hm.shape[0] and hm[r].any()]
            lefts = [int(np.argmax(hm[r])) for r in rows_in]
            rights = [w - 1 - int(np.argmax(hm[r][::-1])) for r in rows_in]
            flat = not rows_in or min(max(lefts) - min(lefts), max(rights) - min(rights)) < 0.12 * w
            if ring > 0.5 * il or flat:
                HOLE_LOG.append((hx, hy, "big-shape", (ring / il, flat)))
                continue
        # 타원 고리는 머리 상자(구멍 + 고리)의 네 모서리 바깥이 희다; 빔·기둥·줄이 둘러싼 네모 구멍, 글자 A·4의 세모 구멍은
        # 모서리가 잉크다. 기둥이 붙는 모서리 하나는 봐준다. 고리 두께는 흐린 그림에서 0.35il까지라 머리 상자로 재고, 고리에
        # 닿은 오선 줄이 모서리에 걸리지 않게 줄을 뺀 그림(cut)에서 본다
        hw0, hh0 = max(w + 0.4 * il, 1.2 * il), max(h + 0.4 * il, 0.9 * il)
        rx, ry = int((hw0 - w) / 2) + 2, int((hh0 - h) / 2) + 2
        corners = [(x - rx, y - ry), (x + w + rx, y - ry), (x - rx, y + h + ry), (x + w + rx, y + h + ry)]
        inked = sum(1 for px, py in corners if 0 <= py < cut.shape[0] and 0 <= px < cut.shape[1] and cut[py, px])
        if inked >= 2:
            HOLE_LOG.append((hx, hy, "corners", inked))
            continue
        # 구멍 바로 위·아래의 잉크는 고리 두께(0.35il 아래)여야 한다: 비스듬한 빔(0.4–0.5il)이 기둥·줄과 둘러싼 구멍은 모서리가
        # 희어도 위가 두껍다. 가운데 열에서 잰다; 아래는 줄을 뺀 뒤라 희기도 하다
        colx = int(round(cxm))
        if 0 <= colx < region.shape[1]:
            up = 0
            yy = y - 1
            while yy >= 0 and region[yy, colx]:
                if not any(abs(yy - ly) <= t_line // 2 + 1 for ly in line_rows):   # 고리에 닿은 오선 줄은 두께에 안 넣는다
                    up += 1
                yy -= 1
            if up > (0.45 if big_round else 0.35) * il:                # 큰 동그라미는 고리도 굵다 (하나님은 너를: 0.4il)
                HOLE_LOG.append((hx, hy, "thick", up / il))
                continue
        # 오선 밖(위 0.75il 넘게, 아래도)이면 덧줄이 있어야 한다: 코드 글자(A의 세모 구멍)와 가사 글자를 거른다
        if (hy < staff.top - 0.75 * il and ledger_count(binary < 128, staff, hx, il, t_line, False) < 1) or \
           (hy > staff.bottom + 0.75 * il and ledger_count(binary < 128, staff, hx, il, t_line, True) < 1):
            HOLE_LOG.append((hx, hy, "ledger", 0))
            continue
        head_w, head_h = int(max(w + 0.4 * il, 1.2 * il)), int(max(h + 0.4 * il, 0.9 * il))   # 고리가 두꺼우면 구멍보다 머리가 훨씬 크다
        if not short_stem(region, cx, cy, head_w, il):
            HOLE_LOG.append((hx, hy, "stem", 0))
            continue
        if any(abs(hx - o.x) < 0.7 * il and abs(hy - o.y) < 0.7 * il for o in heads):
            HOLE_LOG.append((hx, hy, "dup", 0))
            continue
        # 마디줄은 구멍을 **가로지른다**(붙임줄×마디줄 고리): 구멍 폭 안에서만 본다. 작은 구멍 옆의 제 기둥(오선을 다 가로지르는
        # 낮은 음의 기둥)을 마디줄로 보면 안 된다 (주를 위한 이곳에 12마디)
        # 8분쉼표가 번져 닫힌 구멍은 작다: 칸보다 큰 동그라미에는 꼬리 검사를 안 한다 (하나님은 너를: 흐려서 기둥이 줄 사이마다 끊겨
        # 기둥 조각과 점이 비스듬한 꼬리로 보였다)
        bar_through = bool((binary[staff.top:staff.bottom + 1, int(x + staff.left):int(x + w + staff.left)] < 128).mean(axis=0).__ge__(0.9).any()) if w > 0 else False
        if any(flag_hook(hx, hy, o, il) for o in heads) or (not big_round and rest_tail(binary < 128, hx, hy, il)) or bar_through:
            HOLE_LOG.append((hx, hy, "hook/bar", 0))
            continue
        HOLE_LOG.append((hx, hy, "ok", int(big_round)))
        head = Notehead(hx, hy, head_w, head_h)
        head.evidence = notes.duration_evidence(binary, head, il)
        head.evidence.hollow = True
        head.big_ring = big_round                    # 칸보다 큰 동그라미: 옮겨 그릴 때도 그 크기로
        out.append(head)
    return out


def short_stem(ink: np.ndarray, cx: float, cy: float, w: int, il: float) -> bool:
    """머리 상자 폭 안의 어느 열에서든 머리 위나 아래(0.5–2.5il)로 0.9il 넘게 이어진 세로 잉크가 있으면 기둥. notes.has_stem은
    1.5il을 요구하는데 흐린 캡처의 2분음표 기둥이 1il밖에 안 남은 경우가 있다(주님은 산 같아서 13마디); 구멍 머리는 이미 모양을
    엄하게 봤으니 짧은 기둥도 받는다."""
    H, W = ink.shape
    icy = int(round(cy))
    for x in range(int(cx - w / 2) - 3, int(cx + w / 2) + 4):
        if not 0 <= x < W:
            continue
        for ya, yb in ((icy - int(2.5 * il), icy - int(0.5 * il)), (icy + int(0.5 * il), icy + int(2.5 * il))):
            seg = ink[max(0, ya):max(0, yb), x]
            if seg.size and seg.sum() >= 0.9 * il:
                return True
    return False


def rest_tail(ink: np.ndarray, cx: float, cy: float, il: float) -> bool:
    """(cx, cy)의 속 빈 후보 아래(또는 위) 0.5–2.2il에 비스듬한 획이 있는가: 8분쉼표(𝄾)는 번지면 점과 고리 사이가 닫혀 속 빈 머리처럼
    보이고 꼬리가 기둥처럼 보인다(지금은 엘리야). 기둥은 수직이라 행마다 잉크의 x가 같고, 꼬리는 행마다 0.4px 넘게 옮겨 간다."""
    H, W = ink.shape
    # 머리에 붙은 기둥(한쪽 끝이 머리 높이 ±0.7il 안, 다른 끝이 1.5il 넘게 떨어진 곧은 세로 잉크)이 있으면 음표다: 2분음표 밑의
    # 붙임줄도 비스듬해서 꼬리처럼 보인다. 마디줄은 머리를 지나쳐 양쪽으로 뻗으니 기둥이 아니다
    for x in range(max(0, int(cx - 0.8 * il)), min(W, int(cx + 0.8 * il) + 1)):
        ya, yb = max(0, int(cy - 3.5 * il)), min(H, int(cy + 3.5 * il))
        col = ink[ya:yb, x]
        start = None
        for i, v in enumerate(list(col) + [False]):
            if v and start is None:
                start = i
            elif not v and start is not None:
                a, b = ya + start, ya + i - 1
                if b - a >= 1.5 * il and (abs(a - cy) <= 0.7 * il or abs(b - cy) <= 0.7 * il):
                    return False
                start = None
    x0, x1 = max(0, int(cx - 1.3 * il)), min(W, int(cx + 1.3 * il) + 1)
    for ya, yb in ((int(cy + 0.5 * il), int(cy + 2.2 * il)), (int(cy - 2.2 * il), int(cy - 0.5 * il))):
        ys, xs = [], []
        for y in range(max(0, ya), min(H, yb)):
            cols = np.flatnonzero(ink[y, x0:x1])
            if cols.size and cols.size <= 0.6 * il:                  # 가로로 긴 잉크(빔·줄)는 뺀다
                ys.append(y); xs.append(float(cols.mean()) + x0)
        if len(ys) >= 0.6 * il:                                     # 번진 꼬리는 반쯤만 남기도 한다 (지금은 엘리야 4째 줄)
            slope = abs(np.polyfit(ys, xs, 1)[0])
            if slope > 0.4:
                return True
    return False


AI_HOLLOW_RUN = 0                 # 양옆 짝까지 맞아야 하는 수: 2분음표는 흔히 악절 끝(오선 끝·붙임줄 뒤)이라 0
AI_HOLLOW_LOG: list[tuple] = []   # ai_hollow_fix가 바꾼 머리마다 (x, y, 'hollow') (디버깅용)


def ai_hollow_fix(heads: list[Notehead], bars: list[float], measures: list, il: float, staff: Staff | None = None,
                  run: int = 1) -> int:
    """흐린 캡처에서 구멍이 번져 막힌 2분음표(가운데까지 검어 모양으로는 못 알아본다)를 AI 전사의 길이로 되살린다.
    오선의 머리 열과 AI 음표 열(쉼표 뺌)을 음높이로 정렬하고, 음높이가 똑같이 맞은 짝 중 양옆 run개 짝도 똑같이 맞은 것만 믿어
    AI가 2분음표 이상이라 한 머리를 속 빈 것으로 바꾼다. 꽉 찬 쪽으로는 안 바꾼다 (검출기의 거짓 속 빈 머리는 거의 0이다).
    마디 단위 수 맞추기는 안 된다: 놓친 2분음표가 있는 마디는 대개 AI 음표 수도 어긋난다."""
    from . import hybrid
    ai_notes = [x for m in measures for x in m.notes if x.step]
    if not ai_notes or not heads or staff is None:
        return 0
    hp = []
    for h in heads:
        step = int(round((staff.bottom - h.y) / (il / 2)))
        hp.append(step_pitch(step))
    cost = lambda a, b: 0.0 if a == (b.step, b.octave) else (0.6 if a[0] == b.step else 1.0)
    pairs = hybrid.align(hp, ai_notes, cost, 0.6, 0.6)
    exact = [i is not None and j is not None and hp[i] == (ai_notes[j].step, ai_notes[j].octave) for i, j in pairs]
    if sum(exact) < 0.7 * len(heads):                 # AI가 이 오선을 엉뚱하게 읽었으면(음높이 70% 미만 일치) 안 믿는다
        return 0
    n = 0
    for k, (i, j) in enumerate(pairs):
        if not exact[k]:
            continue
        if not all(0 <= k + d < len(pairs) and exact[k + d] for d in range(-run, run + 1) if d):
            continue
        h, a = heads[i], ai_notes[j]
        if a.duration >= 2 and not (h.evidence and h.evidence.hollow):
            if h.evidence is None:
                h.evidence = notes.DurationEvidence(False, False, 0, False)
            h.evidence.hollow = True
            AI_HOLLOW_LOG.append((h.x, h.y, "hollow", str(a.duration), round(sum(exact) / len(heads), 2), len(heads), len(ai_notes)))
            n += 1
    return n


ARC_LOG: list[tuple] = []     # find_arcs가 후보마다 (x0, y0, x1, y1, 판정) 을 남긴다 (디버깅용)


def find_arcs(binary: np.ndarray, staff: Staff, heads: list[Notehead], bars: list[float], il: float, t_line: int
              ) -> list[tuple[int, int, np.ndarray]]:
    """이음줄·붙임줄: 오선 줄·머리·기둥·빔을 뺀 잉크 중 가늘고 옆으로 긴 활 모양 덩어리로, 양 끝이 머리(또는 마디줄) 가까이에 있는 것.
    (y0, x0, 마스크) 목록. 제자리 이조에서는 한 오선의 음이 모두 같은 칸 수로 움직이므로 곡선도 그만큼 평행이동하면 된다."""
    H, W = binary.shape
    y0, y1 = max(0, int(staff.top - 3 * il)), min(H, int(staff.bottom + 3 * il))
    x0, x1 = max(0, staff.left - 2), min(W, staff.right + 2)
    ink = (binary[y0:y1, x0:x1] < 128).astype(np.uint8)
    cut = strip_staff_lines(ink, [y - y0 for y in staff.lines], t_line)
    cut0 = cut.copy()
    # 굵은 것(머리·빔·음자리표·쉼표: 0.45il 타원 열림을 견디는 것)과 기둥·마디줄(세로 1.4il 열림)을 뺀다. 가는 곡선만 남는다.
    # 머리는 상자로 지우지 않는다: 이웃 머리 사이의 짧은 붙임줄이 상자에 잘려 조각만 남았다
    # (흐린 캡처의 붙임줄은 가운데가 0.45il까지 굵어 0.45il 타원이 삼켰다 → 0.62il; 빔은 따로 1.0il×0.3il 열림으로)
    ek = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (max(3, int(0.62 * il)) | 1, max(3, int(0.62 * il)) | 1))
    cut[cv2.dilate(cv2.morphologyEx(cut, cv2.MORPH_OPEN, ek), np.ones((3, 3), np.uint8)) > 0] = 0
    bk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, int(1.0 * il)), max(2, int(0.3 * il))))
    cut[cv2.dilate(cv2.morphologyEx(cut, cv2.MORPH_OPEN, bk), np.ones((3, 3), np.uint8)) > 0] = 0
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, int(1.4 * il))))
    cut[cv2.dilate(cv2.morphologyEx(cut, cv2.MORPH_OPEN, vk), np.ones((3, 3), np.uint8)) > 0] = 0
    # 속 빈 머리의 고리는 가늘어 남는다: 찾은 머리 상자는 따로 지운다 (고리에 붙은 붙임줄은 키 큰 덩어리가 되어 버려지니)
    for h in heads:
        if h.evidence and h.evidence.hollow:
            hx0, hy0 = int(h.x - h.w / 2) - x0 - 2, int(h.y - h.h / 2) - y0 - 2
            cut[max(0, hy0):hy0 + h.h + 5, max(0, hx0):hx0 + h.w + 5] = 0
    n, lab, stt, _ = cv2.connectedComponentsWithStats(cut, connectivity=8)
    anchors = [(h.x, h.y) for h in heads] + [(b, (staff.top + staff.bottom) / 2) for b in bars]
    # 열림이 곡선의 끝(머리·기둥에 닿는 곳)을 깎아 내므로, 받아들인 곡선은 열기 전 그림의 가는 잉크(세로 런 0.4il 이하)를 따라 되살린다
    vrun = np.zeros_like(cut0, np.int32)
    for x in range(cut0.shape[1]):
        col = cut0[:, x]
        run = 0
        for y in range(cut0.shape[0]):
            run = run + 1 if col[y] else 0
            if run:
                vrun[y - run + 1:y + 1, x] = run
    thin = ((cut0 > 0) & (vrun <= 0.4 * il)).astype(np.uint8)
    _, tlab = cv2.connectedComponents(thin, connectivity=8)
    out = []
    for i in range(1, n):
        bx, by, bw, bh, area = stt[i]
        gx0, gy0 = bx + x0, by + y0
        tag = None
        if bw < 0.5 * il:                                  # 이웃한 8분음표 사이의 붙임줄은 머리 상자를 빼면 0.5il쯤만 남는다
            tag = "narrow"
        elif bh > 2.5 * il or bh > 0.8 * bw:
            tag = "tall"
        elif gy0 > staff.bottom + 1.3 * il and not any(abs(h.x - (gx0 + bw / 2)) <= bw / 2 + il and h.y > staff.bottom + 0.3 * il for h in heads):
            tag = "lyrics"                                   # 오선 아래 멀리, 근처에 낮은 머리도 없다: 가사
        elif gy0 + bh < staff.top - 0.3 * il and bw <= 1.6 * il and cross_shaped(
                binary < 128, (max(0, gx0 - int(0.3 * il)), max(0, gy0 - int(1.2 * il)), gx0 + bw + int(0.3 * il), gy0 + bh + int(0.5 * il))):
            tag = "symbol"                                   # 오선 위 Coda 기호 𝄌의 고리 토막 (주 이름 찬양 끝줄: 옮겨져 기호가 반쯤 지워졌다)
        if tag:
            ARC_LOG.append((gx0, gy0, gx0 + bw, gy0 + bh, tag)); continue
        comp = (lab[by:by + bh, bx:bx + bw] == i)
        cols = np.flatnonzero(comp.any(axis=0))
        # 오선 줄에 붙은 덩어리: 줄을 따라 뻗던 곡선의 양 끝은 줄과 함께 지워졌으니(흐린 캡처의 붙임줄은 줄 위를 탄다) 가운데
        # 토막만 남는다 — 납작하고 끝이 머리에서 멀다. 그런 것은 기준을 늦춘다
        on_line = any(abs(gy0 - (ly + t_line // 2 + 1)) <= 2 or abs(gy0 + bh - (ly - t_line // 2 - 1)) <= 2 for ly in staff.lines)
        # 열마다 세로 두께의 중앙값 = 획 두께; 가로줄 하나(0.3il 넘게 굵은 열 없이)
        thick = np.median(comp.sum(axis=0)[cols])
        # 겹친 이음줄 둘(한 음 위로 짧은 것과 긴 것)은 0.5il까지 굵다 (모든 능력 4째 줄); 빔은 앞에서 이미 뺐으니 0.6il까지 받는다
        if thick > 0.6 * il:
            tag = "thick"
        else:
            ys = np.array([np.flatnonzero(comp[:, c]).mean() for c in cols], float)
            xs = cols.astype(float)
            a, b_, c_ = np.polyfit(xs, ys, 2)
            fit = a * xs * xs + b_ * xs + c_
            resid = float(np.abs(ys - fit).mean())
            sag = abs(a) * (bw / 2) ** 2                       # 가운데와 끝의 높이 차
            if resid > max(1.5, 0.08 * il):
                tag = "wiggly"
            elif sag < (0.02 if on_line else 0.05) * il:        # 작은 캡처의 붙임줄은 아주 납작하다 (주님은 산 같아서: 0.06il)
                tag = "straight"
            else:
                # 양 끝이 머리(가로 1.0il·세로 1.5il 안; 줄에 붙은 토막은 가로 2.0il)나 마디줄(가로 1.3il·세로 2.2il) 가까이
                ends = [(gx0 + cols[0], gy0 + ys[0]), (gx0 + cols[-1], gy0 + ys[-1])]
                hx_tol = (2.0 if on_line else 1.0) * il
                near_heads = [[h for h in heads if abs(ex - h.x) <= hx_tol and abs(ey - h.y) <= 1.5 * il] for ex, ey in ends]
                near_bars = [any(abs(ex - b) <= 1.3 * il for b in bars) and staff.top - 2.2 * il <= ey <= staff.bottom + 2.2 * il
                             for ex, ey in ends]
                if not all(nh or nb for nh, nb in zip(near_heads, near_bars)):
                    tag = "loose"
                else:
                    # 머리 아래의 곡선은 아래로(가운데가 끝보다 낮게), 위의 곡선은 위로 불러야 한다
                    ref = [h.y for nh in near_heads for h in nh]
                    if ref:
                        below = np.mean([ey for _, ey in ends]) > float(np.mean(ref))
                        mid_lower = a < 0                        # y는 아래로 커진다: a<0 이면 가운데가 더 낮다
                        if below != mid_lower:
                            tag = "bulge"
        if tag:
            ARC_LOG.append((gx0, gy0, gx0 + bw, gy0 + bh, tag)); continue
        ARC_LOG.append((gx0, gy0, gx0 + bw, gy0 + bh, "ok"))
        ids = set(np.unique(tlab[by:by + bh, bx:bx + bw][comp])) - {0}
        full = np.isin(tlab, list(ids)) if ids else np.zeros_like(thin, bool)
        full[by:by + bh, bx:bx + bw] |= comp
        ys_, xs_ = np.nonzero(full)
        fy0, fy1, fx0, fx1 = ys_.min(), ys_.max() + 1, xs_.min(), xs_.max() + 1
        if fx1 - fx0 > 2.5 * bw or fy1 - fy0 > 2 * il:            # 가는 잉크를 따라 엉뚱한 데까지 번졌다: 원래 덩어리만
            out.append((gy0, gx0, comp.astype(np.uint8)))
        else:
            out.append((y0 + fy0, x0 + fx0, full[fy0:fy1, fx0:fx1].astype(np.uint8)))
    return out


def find_line_arcs(binary: np.ndarray, staff: Staff, heads: list[Notehead], il: float, t_line: int,
                   avoid: list[tuple[int, int, int, int]] = ()) -> list[tuple[int, int, np.ndarray]]:
    """오선 줄을 타고 가는 붙임줄(줄 위 음의 붙임줄은 그 줄에 붙어 활처럼 부푼다): 줄의 위·아래 가장자리에서 줄과 이어진 잉크의
    두께를 열마다 재어, 머리·기둥 자리가 아닌 곳에서 0.7il 넘게 이어지며 가운데가 0.12–0.9il로 부푼 구간을 찾는다.
    (줄 지우기(strip_staff_lines)가 그런 곡선을 줄과 함께 지워 find_arcs에는 토막만 남는다.) (y0, x0, 마스크) 목록."""
    H, W = binary.shape
    ink = binary < 128
    out = []
    x_lo, x_hi = max(0, staff.left), min(W, staff.right + 1)
    for ly in staff.lines:
        for side in (-1, 1):
            edge = ly - t_line // 2 - 2 if side < 0 else ly + (t_line + 1) // 2 + 2
            if not 0 <= edge < H:
                continue
            prof = np.zeros(W, int)
            for x in range(x_lo, x_hi):
                y, n = edge, 0
                while 0 <= y < H and ink[y, x] and n < 2 * il:
                    n += 1
                    y += side
                prof[x] = n
            # 머리 자리(±0.3il)와 기둥·마디줄(1.2il 넘게 이어진 열)은 뺀다
            for h in heads:
                prof[max(0, int(h.x - h.w / 2 - 0.3 * il)):int(h.x + h.w / 2 + 0.3 * il) + 1] = 0
            prof[prof > 1.2 * il] = 0
            x = x_lo
            while x < x_hi:
                if prof[x] < 2:
                    x += 1
                    continue
                s_ = x
                while x < x_hi and prof[x] >= 2:
                    x += 1
                run = prof[s_:x]
                w = x - s_
                peak = int(run.max())
                mid = s_ + int(run.argmax())
                tag = None
                if w < 0.7 * il:
                    tag = "l-short"
                elif not 0.12 * il <= peak <= 0.9 * il:
                    tag = "l-peak"
                elif not (s_ + 0.2 * w <= mid <= x - 0.2 * w):
                    tag = "l-skew"                                # 가운데가 아니라 끝에서 두껍다: 기둥·글자 뿌리
                elif run[: max(1, w // 6)].mean() > 0.6 * peak or run[-max(1, w // 6):].mean() > 0.6 * peak:
                    tag = "l-flat"                                # 끝이 얇아지지 않는다: 빔·굵은 줄
                elif not any(abs(h.x - s_) <= 1.5 * il or abs(h.x - x) <= 1.5 * il for h in heads):
                    tag = "l-loose"
                elif any(min(x, bx1) - max(s_, bx0) > 0.5 * w and min(ly, by1) + 0.6 * il >= max(ly, by0) for bx0, by0, bx1, by1 in avoid):
                    tag = "l-avoid"                               # find_arcs가 거른 덩어리의 일부: 혼자 옮기면 곡선이 끊긴다
                ARC_LOG.append((s_, edge if side < 0 else ly, x, ly if side < 0 else edge, tag or "l-ok"))
                if tag:
                    continue
                y0 = edge - peak + 1 if side < 0 else edge
                m = np.zeros((peak, w), np.uint8)
                for k in range(w):
                    n = prof[s_ + k]
                    if side < 0:
                        m[peak - n:peak, k] = 1
                    else:
                        m[:n, k] = 1
                out.append((y0, s_, m))
    return out


def move_arcs(img: np.ndarray, arcs: list[tuple[int, int, np.ndarray]], dy: int, staff: Staff, t_line: int,
              avoid: np.ndarray | None = None) -> None:
    """곡선 픽셀을 지우고(오선 줄은 잇고) dy만큼 옮겨 다시 찍는다. avoid(새 코드 글자 둘레)는 건드리지 않는다."""
    H, W = img.shape[:2]
    if avoid is None:
        avoid = np.zeros((H, W), bool)
    for y0, x0, m in arcs:
        h, w = m.shape
        md = (cv2.dilate(m, np.ones((3, 3), np.uint8)) > 0) & ~avoid[y0:y0 + h, x0:x0 + w]
        region = img[y0:y0 + h, x0:x0 + w]
        keep = np.zeros(h, bool)
        for ly in staff.lines:
            a, b = max(y0, ly - t_line // 2 - 1), min(y0 + h, ly + (t_line + 1) // 2 + 1)
            if b > a:
                keep[a - y0:b - y0] = True
        region[md & ~keep[:, None]] = 255
        for r in np.flatnonzero(keep):
            cs = np.flatnonzero(md[r])
            if cs.size:
                around = np.concatenate([img[y0 + r, max(0, x0 - 6):x0], img[y0 + r, x0 + w:min(W, x0 + w + 6)]])
                region[r, cs] = int(np.median(around)) if around.size else 255
    for y0, x0, m in arcs:
        h, w = m.shape
        ny = y0 + dy
        sy0, sy1 = max(0, -ny), min(h, H - ny)
        if sy1 <= sy0:
            continue
        dst = img[ny + sy0:ny + sy1, x0:x0 + w]
        dst[(m[sy0:sy1] > 0) & ~avoid[ny + sy0:ny + sy1, x0:x0 + w]] = 0


def on_barline(ink: np.ndarray, staff: Staff, x: float, il: float) -> bool:
    """x 둘레 ±0.35il에 오선 첫 줄부터 다섯째 줄까지 거의 끊기지 않은 세로선이 있나 (마디줄). 붙임줄이 마디줄·오선 줄과 만든
    고리가 속 빈 머리로 잡히는 것을 거른다; 진짜 머리의 기둥은 머리 중심에서 0.6il 떨어져 있다."""
    x0, x1 = max(0, int(x - 0.35 * il)), min(ink.shape[1], int(x + 0.35 * il) + 1)
    if x1 <= x0:
        return False
    return bool((ink[staff.top:staff.bottom + 1, x0:x1].mean(axis=0) >= 0.9).any())


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
        if rest_tail(binary < 128, hx, hy, il):                       # 8분쉼표의 고리 (지금은 엘리야)
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


def harvest_glyph(ink: np.ndarray, box: tuple[int, int, int, int], staff: Staff | None = None, t_line: int = 1) -> np.ndarray:
    """조표·임시표 글자 하나의 잉크 마스크 (0/255). 새 조표나 임시표를 그릴 때 그림 자체의 글꼴을 쓴다.
    staff를 주면 상자를 지나는 오선 줄은 뺀다 (다른 높이에 찍을 때 줄 토막이 따라가지 않게)."""
    x0, y0, x1, y1 = box
    region = ink[y0:y1, x0:x1].astype(np.uint8)
    if staff is not None:
        region = strip_staff_lines(region, [y - y0 for y in staff.lines if y0 <= y < y1], t_line)
    return (region * 255).astype(np.uint8)


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
                       work: Path | None = None, fixes: dict | None = None) -> Report:
    """src 그림을 semitones만큼 제자리에서 이조해 out에 쓴다 (out 확장자대로 PNG/JPEG). 작업은 ingest가 키운 쪽 크기로 한다.
    work: AI 답을 캐시하는 폴더 (같은 그림을 다시 옮길 때 다시 묻지 않는다).
    fixes: 검수(audit)나 사용자가 정한 수정 — {"add_heads": [(오선, x, y, 속빔|None)], "drop_heads": [(오선, x, y)],
           "shift_heads": [(오선, x, y, 칸)], "drop_box": [(오선, x0, y0, x1, y1)], "shift_box": [(오선, x0, y0, x1, y1, 칸)], "chords": [(오선, x, y, 새 코드)], "add_chords": [(오선, x0, y0, x1, y1,옛 코드[, 새 코드])]}
           (작업 크기 좌표). 첫 판단을 그 자리에서만 바꾼다."""
    fixes = fixes or {}
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
    # 너무 넓은 상자: 글자 하나가 (높이 × 글자 수+1)보다 넓을 수 없다 ("Last time"을 E로). 조각: 다른 상자와 폭의 절반 넘게 겹치는데
    # 그보다 30% 넘게 낮은 것 (Aadd9의 밑동을 D로). 낮기만 한 상자는 남긴다 — Vision이 진짜 코드의 위아래를 자르기도 한다
    def fragment(b, row):
        return any(o is not b and min(o.x1, b.x1) - max(o.x0, b.x0) > 0.5 * min(o.x1 - o.x0, b.x1 - b.x0)
                   and b.y1 - b.y0 < 0.7 * (o.y1 - o.y0) for o in row)
    hs = sorted(b.y1 - b.y0 for row in chord_boxes for b in row)
    med_h = hs[len(hs) // 2] if hs else il
    # 바닥이 줄의 바닥선(상자 셋 이상의 중앙값)에서 0.4il 넘게 뜬 상자도 코드가 아니다: Coda 기호 𝄌는 "Coda" 글자 옆 위쪽에 있어
    # Vision이 "E"로 읽는데(주 이름 찬양), 코드로 두면 AI 코드가 거기 붙어 기호를 지운다. 기호는 절대 지우면 안 된다 (사용자 지시)
    # Vision 상자는 글자 일부만 잡기도 하므로(B♭²의 윗부분만, A의 가로획만) 상자 바닥이 아니라 상자 열의 잉크가 바닥선 쪽으로
    # 어디까지 이어지는지로 잰다 (덩어리는 빔에 붙은 글자가 빔과 함께 빠져 못 쓴다)
    ink_all = page.binary < 128

    # 기둥 위에 높이 쓰인 진짜 코드도 뜨므로(지금은 엘리야의 D), 뜬 것 중 십자 모양(가운데 열과 가운데 행의 잉크가 상자를 거의 관통)만
    # 기호로 본다 — 글자 A·B·D·E는 가운데 열이나 행이 구멍·빈 곳을 지난다
    def floating(b, row, si):
        bots = sorted(o.y1 for o in row)
        if len(bots) < 3:
            return False
        base = bots[len(bots) // 2]
        band = ink_all[int(b.y0):min(ink_all.shape[0], int(base + 0.15 * il)), int(b.x0):int(b.x1)]
        rows = np.flatnonzero(band.any(axis=1))
        lowest = int(b.y0) + int(rows[-1]) if rows.size else int(b.y0)
        return base - lowest > 0.4 * il and cross_shaped(ink_all, (int(b.x0), int(b.y0), int(b.x1), int(b.y1)))
    # 작은 글씨(D.C.·지시문)도 코드가 아니다: 상자 열의 잉크가 위아래로 이어지는 높이(Vision이 자른 위아래까지)가 글자 높이다.
    # 그 높이로 쳐서 글자 수+1보다 넓으면 버린다 (주 이름 찬양: 띠 꼭대기에 잘린 "D.C."를 "A"로 읽어 그 밑 Coda 기호까지 지웠다)
    def small_text(b):
        x0, x1 = int(b.x0), int(b.x1)
        top, bot = max(0, int(b.y0 - 2 * il)), min(ink_all.shape[0], int(b.y1 + 2 * il))
        rows = ink_all[top:bot, x0:x1].any(axis=1)
        mid = int((b.y0 + b.y1) / 2) - top
        if x1 <= x0 or not rows[mid]:
            return False
        a, z = mid, mid
        while a > 0 and rows[a - 1]:
            a -= 1
        while z + 1 < len(rows) and rows[z + 1]:
            z += 1
        return x1 - x0 > (len(b.text) + 1) * (z - a + 1)
    # 1·2번 괄호의 "|1."을 품은 Vision 상자는 괄호를 잘라 근음부터로 (안 자르면 넓어서 아래에서 버려지거나 괄호까지 지운다 — 지금은 엘리야)
    for si, row in enumerate(chord_boxes):
        band_top = systems[si].chord_band[0] if si < len(systems) else 0
        for b in row:
            if b.y0 <= band_top + 1:
                nv = _after_volta(ink_all, (int(b.x0), int(b.y0), int(b.x1), int(b.y1)), il)
                if nv is not None and nv[0] < b.x1 - 0.3 * il:
                    b.x0, b.y0 = nv
    # (글자 수 + 2: Vision이 Dm/F를 "D"와 "F"로 가르면 뒤 상자는 "/F"를 품어 글자보다 두 배 넓다 — 주님은 산 같아서)
    chord_boxes = [[b for b in row if b.x1 - b.x0 <= (len(b.text) + 2) * max(1, b.y1 - b.y0, med_h) and not fragment(b, row)
                    and not floating(b, row, si) and not small_text(b)]
                   for si, row in enumerate(chord_boxes)]
    FILTER_LOG.append([(si, b.text, int(b.x0), int(b.y1)) for si, row in enumerate(chord_boxes) for b in row])
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
                chord_boxes, rep.chord_source = snap_to_blobs(page, systems, merged, il, chord_boxes), label
                _fix_vision_accidentals(chord_boxes, ai_t.fifths)
            else:
                rep.notes_.append(f"{label}'s chords did not match the picture ({int(exact * 100)}% agree): kept Apple Vision's")
                ai_t = None
    # Vision 상자 중 글자 조각(높이가 쪽 중앙값의 65% 미만: 윗첨자·가사 토막·"Last time" 같은 작은 글씨)과 글자 수에 비해 너무 넓은
    # 상자(지시문을 한 글자로 읽은 것)는 코드가 아니다 — 남기면 AI 코드가 거기 짝지어져 엉뚱한 자리에 쓰인다 (찬양의 열기·주 이름 찬양)
    chord_names = [b.text for row in chord_boxes for b in row]
    from . import key as key_mod
    inferred = key_mod.infer_key(chord_names) if len(chord_names) >= 3 else None

    # 1. 조표: 오선마다 글자로 읽는다(깨끗한 그림). 흐린 그림(키운 웹 캡처)에서는 글자 검출이 일부만 잡히거나(4♭ 중 1개를 ♯으로)
    #    열 투영 어림도 틀려서(4♭을 2♭으로), 읽는 데는 코드로 추론한 조(코드 3개 이상, 다이어토닉 비율 0.7 이상)를 먼저 믿고
    #    (네 곡 모두 맞았다; AI는 셋), 코드가 없으면 AI에게 첫 오선 앞부분을 한 번 묻는다(몇 초). 지우는 데는 음자리표와 박자표
    #    사이를 통째로 쓴다 (글자 상자는 일부만이라 옛 ♭이 남는다).
    blurry = page.scale > 1.5
    read: list[tuple[int, list, int, int, tuple | None, int]] = []
    for s in systems:
        kf, kboxes, kstart, kend, clef_right = find_key_signature(ink, s.staff)
        est = key_from_profile(ink, s.staff, il) if blurry or not kboxes else None
        read.append((kf, kboxes, kstart, kend, est, clef_right))
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
    for s, (kf, kboxes, kstart, kend, est, clef_right) in zip(systems, read):
        # 그 오선의 글자 상자는 글자 수가 정한 조와 맞을 때만 믿는다 (주님은 산 같아서: 다섯 오선 중 둘은 3♯ 중 1개만 글자로
        # 잡혀, 다수결로 조는 맞았지만 그 둘에서 ♯ 하나만 지워 둘이 남았다). 안 맞으면 투영 상자로 통째 지운다; 투영도 없으면 글자 상자
        if kboxes and (kf == rep.old_fifths or est is None):
            keysigs.append((kf, kboxes, kstart, kend, True))
        elif est or rep.old_fifths:
            # 투영 상자 그대로 (음자리표 끝부터 박자표 직전까지). 글자 수로 넓히면 안 된다: 조표가 좁게 쌓인 악보(3♯이 2il)에서
            # 박자표의 4를 지워 버렸다. 상자의 왼쪽은 음자리표 덩어리의 오른쪽 끝 뒤: 투영의 음자리표 끝은 고리가 짧은 곳에서 일찍
            # 끝나(보혈을 지나) 음자리표 오른쪽 1.6il이 지워졌다. 투영이 없어도(♯이 박자표에 붙어 무리가 안 갈림) 조를 알면
            # 음자리표 끝부터 통째 지우기로 간다 — 지울 끝은 거기서 틈·첫 머리로 다시 정한다
            x0 = clef_right + 1
            box = (x0, int(s.staff.top - 1.6 * il), max(x0 + int(0.5 * il), est[1][2] if est else x0 + int(2 * il)), int(s.staff.bottom + 1.6 * il))
            keysigs.append((est[0] if est else rep.old_fifths, [box], x0 + int(0.3 * il), est[3] if est else kend, False))
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
            samples[kind] = (harvest_glyph(ink, b, sys_.staff, line_thickness(ink, sys_.staff)),
                             (b[1] + b[3]) / 2 - (sys_.staff.bottom - steps[0] * il / 2))
            if len(kboxes) >= 2:
                old_spacing = (kboxes[1][0] + kboxes[1][2] - kboxes[0][0] - kboxes[0][2]) / 2

    plans_all: list[tuple[System, list[HeadPlan]]] = []
    # 머리 크기를 못 잰 오선(잴 머리가 모자람)은 쪽의 다른 오선에서 잰 크기를 쓴다: 한 쪽 안에서 머리 크기가 들쭉날쭉하지 않게
    shapes = [x for s_, syms_ in zip(systems, symbols)
              if (x := head_shape(ink, syms_.noteheads, s_.staff, line_thickness(ink, s_.staff), il))]
    page_shape = (float(np.median([x[0] for x in shapes])), float(np.median([x[1] for x in shapes]))) if shapes else (1.2 * il, 0.82 * il)
    arc_jobs: list[tuple[list, int, Staff, int]] = []
    drawn_heads: list[tuple[float, float, float, float, bool]] = []   # 새로 그린 머리 (x, y, 폭, 높이, 속 빔): 코드 상자가 지우면 다시 그린다
    for si, (sys_, syms, (kf, kboxes, kstart, kend, real), (_, _, _, _, _, clef_right)) in enumerate(zip(systems, symbols, keysigs, read)):
        st = sys_.staff
        t_line = line_thickness(ink, st)
        # 음자리표·조표 자리에서 잡힌 가짜 머리(보혈을 지나: 음자리표 고리, 주를 위한 이곳에: 줄 위 ♯의 굵은 획)는 뺀다 — 옮겨 그려지고,
        # 첫 머리 자리가 조표 안이 되면 조표를 못 지운다. 글자를 믿는 오선은 조표 다음 잉크(kend)부터, 아니면 글자 수만큼 뒤부터
        zone = max(clef_right, kend - 0.3 * il if real else clef_right + (0.3 + 1.1 * abs(rep.old_fifths)) * il)
        heads = [h for h in syms.noteheads if h.x >= zone]
        min_x = max(min(kend + 0.2 * il, st.left + 9 * il), st.left + 3 * il)
        # 마디줄 위의 속 빈 "머리"(붙임줄이 마디줄·오선 줄과 만든 고리)는 뺀다. 그런 가짜가 있으면 마디줄 검출이 그 선을
        # 기둥으로 보고 버리므로, 뺀 뒤 마디줄을 다시 찾는다
        # 8분음표 꼬리의 고리를 속 빈 머리로 읽은 것(보혈을 지나: 기둥 끝의 큰 고리)도 뺀다 — 보강 검출기들은 거르지만 기본 검출은 안 걸렀다
        heads = [h for h in heads if not (h.evidence and h.evidence.hollow
                                          and (on_barline(ink, st, h.x, il) or rest_tail(ink, h.x, h.y, il)
                                               or any(flag_hook(h.x, h.y, o, il) for o in heads if o is not h)))]
        heads += find_hollow_heads(page.binary, sys_, il, heads, min_x)
        # 구멍 머리는 조표·박자표 다음(첫 오선), 첫 꽉 찬 머리 앞 2.5il부터: ♭의 고리와 4의 세모도 머리 크기의 구멍이다
        hole_x = max(min_x, min((h.x for h in heads if not (h.evidence and h.evidence.hollow)), default=min_x) - 2.5 * il,
                     after_time_signature(ink, st, il, kend) if si == 0 else 0)
        heads += find_hole_heads(page.binary, sys_, il, heads, hole_x, t_line)
        heads += find_whole_notes(page.binary, sys_, il, heads, min_x)
        heads = sorted((h for h in heads if h.x >= zone), key=lambda h: h.x)
        # 오선 위 코드 글자의 구멍(A의 세모)을 기둥 없는 속 빈 머리로 읽은 것 (모든 능력 첫 줄: 덧줄 셋과 함께 옮겨 그려져 새 B 위에 겹쳤다).
        # 읽은 코드 상자 안, 오선 위쪽, 기둥 없는 머리는 뺀다
        cboxes = chord_boxes[si] if si < len(chord_boxes) else []
        # (상자 밑 0.3il까지: C의 아래 굽은 곳이 상자 바닥 4px 밑에 머리로 잡혔다 — 주님 다시 오실때까지)
        heads = [h for h in heads if not (h.y < st.top and not (h.evidence and h.evidence.stem)
                                          and any(b.x0 <= h.x <= b.x1 and b.y0 <= h.y <= b.y1 + 0.3 * il for b in cboxes))]
        # 오선 밖(1il 넘게 위·아래)의 Coda 기호 𝄌: 고리를 세로획이 꿰뚫어 기둥 달린 속 빈 머리로 읽힌다 (주 이름 찬양 끝줄: 옮겨
        # 그려져 기호가 반쯤 지워졌다). 기호는 절대 지우면 안 된다. 머리 둘레 상자가 십자 모양이면 뺀다
        heads = [h for h in heads if not ((h.y < st.top - il or h.y > st.bottom + il)
                                          and cross_shaped(ink, (max(0, int(h.x - 1.2 * il)), max(0, int(h.y - 1.5 * il)),
                                                                 int(h.x + 1.2 * il), int(h.y + 1.5 * il))))]
        # 쉼표(4분쉼표의 굽은 곳, 8분쉼표의 점)를 꽉 찬 머리로 읽은 것: 기둥이 없고 이 오선의 기둥 달린 머리보다 작다 (폭 0.9배·높이
        # 0.8배 미만). 진짜 머리는 흐려서 기둥을 못 찾아도 크기는 같다 (주를 위한 이곳에: 쉼표 20개가 음표로 옮겨졌다)
        stemmed = [h for h in heads if h.evidence and h.evidence.stem and not h.evidence.hollow]
        if len(stemmed) >= 3:
            mw, mh = float(np.median([h.w for h in stemmed])), float(np.median([h.h for h in stemmed]))
            heads = [h for h in heads if (h.evidence and (h.evidence.stem or h.evidence.hollow)) or not (h.w < 0.9 * mw and h.h < 0.8 * mh)]
        # 기둥 없는 속 빈 머리가 오선 밖(0.75il 넘게)인데 덧줄이 없으면 머리가 아니다: 오선 끝에서 다음 줄로 넘어가는 붙임줄 끝이
        # 오선 위에 만든 고리 (주 이름 찬양: 덧줄 달린 2분음표로 옮겨 그려졌다). 구멍 머리 검출기와 같은 기준이다
        heads = [h for h in heads if not (h.evidence and h.evidence.hollow and not h.evidence.stem
                                          and ((h.y < st.top - 0.75 * il and ledger_count(ink, st, h.x, il, t_line, False) < 1)
                                               or (h.y > st.bottom + 0.75 * il and ledger_count(ink, st, h.x, il, t_line, True) < 1)))]
        # 빔 토막·8분음표 꼬리 끝을 꽉 찬 머리로 읽은 것 (주님 한 분만으로: 오선 위 빔 토막이 덧줄 달린 머리로 옮겨 그려지고 빔이
        # 사라졌다; 주님 다시 오실때까지·주 이름 찬양: 꼬리 끝). 가운데 열의 세로 잉크가 머리 높이에 못 미친다
        heads = [h for h in heads if (h.evidence and h.evidence.hollow) or column_fill(ink, h) >= 0.78]
        # 검수가 정한 수정: 못 찾은 머리 더하기, 음표가 아닌 것 빼기
        for fsi, fx, fy in fixes.get("drop_heads", []):
            if fsi == si:
                heads = [h for h in heads if not (abs(h.x - fx) < 0.5 * il and abs(h.y - fy) < 0.5 * il)]
        for fsi, fx, fy, fhollow in fixes.get("add_heads", []):
            if fsi == si and not any(abs(h.x - fx) < 0.6 * il and abs(h.y - fy) < 0.6 * il for h in heads):
                nh = Notehead(fx, fy, int(1.15 * il), int(0.95 * il))
                nh.evidence = notes.duration_evidence(page.binary, nh, il)
                if fhollow is not None:                  # None: 그림에서 잰 속 빔을 그대로
                    nh.evidence.hollow = bool(fhollow)
                heads.append(nh)
        for fsi, fx, fy, fsteps in fixes.get("shift_heads", []):
            if fsi == si:
                near = [h for h in heads if abs(h.x - fx) < 0.8 * il and abs(h.y - fy) < 1.0 * il]
                if near:
                    h = min(near, key=lambda h: (h.x - fx) ** 2 + (h.y - fy) ** 2)
                    h.step_bias = getattr(h, "step_bias", 0) + int(fsteps)
        # 사용자가 네모로 고른 곳: 안의 머리를 모두 빼거나 모두 옮긴다 (가운데가 상자 안이면)
        pad = 0.3 * il
        for fsi, x0, y0, x1, y1 in fixes.get("drop_box", []):
            if fsi == si:
                heads = [h for h in heads if not (x0 - pad <= h.x <= x1 + pad and y0 - pad <= h.y <= y1 + pad)]
        for fsi, x0, y0, x1, y1, fsteps in fixes.get("shift_box", []):
            if fsi == si:
                for h in heads:
                    if x0 - pad <= h.x <= x1 + pad and y0 - pad <= h.y <= y1 + pad:
                        h.step_bias = getattr(h, "step_bias", 0) + int(fsteps)
        heads.sort(key=lambda h: h.x)
        bars = sorted(notes.find_barlines(page.binary, st, il, heads))
        snapped = ledger_snap(ink, st, heads, il, t_line, page.binary)
        if snapped:
            rep.notes_.append(f"staff {si + 1}: {snapped} noteheads moved to the ledger line they sit on")
        if ai_t is not None and blurry:
            n_fix = ai_hollow_fix(heads, bars, [m for m in ai_t.measures if m.system == si], il, st, AI_HOLLOW_RUN)
            if n_fix:
                rep.notes_.append(f"staff {si + 1}: {n_fix} half notes taken from the {label} reading")
        # 2. 머리마다: 자리, 왼쪽 틈의 임시표, 옛 음 → 새 음 → 새 임시표
        plans: list[HeadPlan] = []
        state_old = dict(old_alters)             # (글자, 옥타브)별 현재 변화 — 마디마다 초기화. 글자만으로 둔다 (옥타브 무시)
        state_new = dict(new_alters)
        bar_i = 0
        prev_right = after_time_signature(ink, st, il, kend)   # 조표·박자표 다음부터 (그 글자들을 첫 음의 임시표로 보지 않게)
        for h in heads:
            while bar_i < len(bars) and bars[bar_i] < h.x:          # 마디줄을 지나면 임시표 상태 초기화
                state_old, state_new = dict(old_alters), dict(new_alters)
                bar_i += 1
                prev_right = int(bars[bar_i - 1]) + 2
            # 사용자가 "한 칸 올려/내려"라고 표시한 머리: 검출이 칸을 잘못 읽은 것이니 옛 음 자체를 고친다 (지울 자리는 그대로)
            step_old = int(round((st.bottom - h.y) / (il / 2))) + getattr(h, "step_bias", 0)
            hollow = bool(h.evidence and h.evidence.hollow)
            plan = HeadPlan(h, step_old, step_old + rep.steps, hollow)
            if h.evidence and h.evidence.dot:
                plan.dot = find_dot(ink, h, il)
            gap_x0, gap_x1 = max(prev_right, int(h.x - 2.6 * il)), int(h.x - h.w / 2 - 0.12 * il)
            if gap_x1 - gap_x0 >= 0.5 * il:
                m = match_accidental(ink, gap_x0, gap_x1, h.y, il, st, t_line)
                # 임시표는 머리 바로 앞에 붙는다(0.6il 안). 더 떨어진 것은 앞 음 뒤의 8분쉼표 (주를 위한 이곳에: 1.2–1.3il에서 ♭으로 읽혔다)
                if m and gap_x1 - m[1][2] > 0.6 * il:
                    m = None
                # 앞 음의 아래로 뻗은 기둥(왼쪽에 붙고 꼬리가 오른쪽으로 말린다)은 ♭처럼 보인다: 어느 머리의 기둥 열이 상자 안이면 아니다
                if m and any(o.evidence and o.evidence.stem and m[1][0] - 2 <= o.evidence.stem_x <= m[1][2] + 2
                             and abs(o.y - (m[1][1] + m[1][3]) / 2) < 3 * il for o in heads if o is not h):
                    m = None
                if m:
                    plan.acc_old = (m[0], m[1])
                    rep.accidentals_found += 1
                    if m[0] not in samples:                           # 그림의 임시표 글자를 새 임시표의 본으로 (같은 글꼴)
                        b = m[1]
                        samples[m[0]] = (harvest_glyph(ink, b, st, t_line), (b[1] + b[3]) / 2 - h.y)
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
        rep.state.setdefault("bars", []).append(bars)
        rep.state.setdefault("zone", []).append(max(zone, after_time_signature(ink, st, il, kend) if si == 0 else zone))
        rep.heads += len(plans)

        # 2b. 이음줄·붙임줄: 머리를 지우기 전의 잉크에서 찾아 두고, 다 그린 뒤 같은 칸 수만큼 옮긴다
        if rep.steps:
            n_log = len(ARC_LOG)
            arcs = find_arcs(page.binary, st, heads, bars, il, t_line)
            rejected = [e[:4] for e in ARC_LOG[n_log:] if e[4] in ("thick", "wiggly", "tall", "bulge", "loose")]
            arcs += find_line_arcs(page.binary, st, heads, il, t_line, rejected)
        else:
            arcs = []
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
                erase(img, p.acc_old[1], st, t_line)
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
        new_kind = "sharp" if rep.new_fifths > 0 else "flat"
        # 흐린 그림(글자를 못 읽은 조표): 조표·박자표·첫 음이 한 덩어리로 번져 글자별로는 못 지운다(모든 능력: 아래 ♯이 4 밑에).
        # 음자리표 끝부터 첫 음 직전까지(단, 옛 조표 폭 + 첫 오선이면 박자표 폭까지만 — 도돌이표는 남게) 통째로 지우고,
        # 새 조표를 촘촘히 쓴 뒤 첫 오선에는 AI가 읽은 박자표 숫자를 다시 쓴다. 자리가 모자라면 전체를 60%까지 줄인다
        redraw = (not real) and kboxes and heads and (si > 0 or ai_t is not None)
        if redraw:
            clef_end = int(kboxes[0][0])
            # 조표가 음자리표에 붙은 흐린 캡처는 상자의 왼쪽(높이로 어림한 음자리표 끝)이 실제 끝보다 1–2il 왼쪽이라, 글자 수로 어림한
            # 지울 끝이 박자표 앞에서 끝나 옛 4/4가 남고 새 4/4가 그 뒤에 또 그려졌다 (하나님은 너를). 구멍으로 잰 끝이 있으면 그것부터
            kx = find_clef_end(ink, st, t_line, il)
            if kx is not None and kx > clef_end:
                clef_end = kx
            n_new, with_ts = abs(rep.new_fifths), si == 0 and ai_t is not None
            first = int(heads[0].x - 0.35 * il)                   # 첫 머리는 어차피 지우고 다시 그리니 그 왼쪽 반까지
            # 옛 조표의 끝 = 조표 폭쯤 뒤의 첫 빈 열 틈(0.25il); 그다음 잉크 무리가 박자표(첫 오선). 새 조표가 옛 자리에 들어가면
            # 거기까지만 지우고 박자표는 둔다(C 같은 기호도 그대로); 안 들어가거나 틈이 없으면(박자표가 첫 음에 붙은 모든 능력)
            # 첫 머리 직전까지 지우고 박자표를 AI가 읽은 숫자로 다시 쓴다
            prof = column_profile(ink, st, il)
            empty = max(2, t_line // 2 + 1)        # 줄 두께+3이면 ♯의 두 빗금 사이 열(빗금 둘 = 8px)이 빈 열로 보여 셋째 ♯이 남았다

            def gap_after(x: int) -> int | None:
                # 틈은 0.45il 넘게: 줄 위에 앉은 ♯은 빗금이 줄 행과 겹쳐 투영에서 사라지므로 세로획 사이(0.34il)가 빈 열로 보인다
                while x < first:
                    if all(prof[x:min(first, x + int(0.45 * il))] <= empty):
                        return x
                    x += 1
                return None

            # 쌓아 쓴 조표(♯♯ 위에 ♯ 아래)는 글자당 0.85il쯤 차지한다; 그 안에서 틈을 찾으면 마지막 글자가 남는다
            key_end = gap_after(clef_end + int((0.3 + 1.1 * abs(rep.old_fifths)) * il))
            need_key = (0.25 + 0.7 * n_new + 0.2) * il
            # 틈이 없으면(박자표에 쉼표·첫 음이 붙은 찬양의 열기) 글자 수로 어림한 끝까지만: 첫 머리까지 지우면 쉼표가 사라진다.
            # 덩어리 단위로 지우므로 어림 끝에 걸친 4는 통째로 지워지고 그 너머 중심의 쉼표는 남는다
            est_end = clef_end + int((0.3 + 1.1 * abs(rep.old_fifths) + (1.5 if si == 0 else 0)) * il)   # 번진 ♯은 글자당 1.1il
            x_end, with_ts = min(first, est_end), si == 0 and ai_t is not None
            if key_end is not None and key_end - clef_end >= need_key:
                x_end, with_ts = key_end, False
            x_end = max(clef_end + int(0.5 * il), x_end)
            KEY_LOG.append((si, clef_end, key_end, x_end, first, with_ts, kboxes[0]))
            erase_key_area(img, ink, st, t_line, il, clef_end, x_end)
            # 지운 뒤 지운 끝과 첫 머리 사이에 박자표 크기의 잉크(오선 안, 높이 1.5il 넘게)가 남았으면 원래 박자표가 살아 있다:
            # 새로 쓰면 둘이 겹친다 (주를 위한 이곳에: C 앞에 4/4가 끼었다)
            if with_ts:
                band = (img[int(st.top):int(st.bottom) + 1, x_end:max(x_end + 1, first)] < 128).astype(np.uint8)
                band = strip_staff_lines(band, [y - int(st.top) for y in st.lines], t_line)
                n_, _, stt_, _ = cv2.connectedComponentsWithStats(band, connectivity=8)
                if any(stt_[i][3] >= 1.5 * il for i in range(1, n_)):
                    with_ts = False
            need = (0.25 + 0.7 * n_new + (1.4 if with_ts else 0)) * il
            scale = max(0.6, min(1.0, (x_end - clef_end) / need)) if need else 1.0
            x = clef_end + 0.25 * il * scale
            if n_new:
                draw_key_signature(img, st, rep.new_fifths, int(x), int(x + (0.7 * n_new * scale + 0.5) * il), il,
                                   samples.get(new_kind), 0.7 * il * scale)
                x += 0.7 * n_new * scale * il + 0.3 * il * scale
            if with_ts:
                draw_time_signature(img, st, ai_t.beats, ai_t.beat_type, int(x), il, scale)
        else:
            for b in kboxes:                                               # 글자 상자는 흐린 가장자리 획(♯의 왼쪽 세로획)을 놓치기도 해 여유를 둔다
                erase(img, (b[0] - int(0.15 * il), b[1] - int(0.1 * il), b[2] + int(0.15 * il), b[3] + int(0.1 * il)), st, t_line)
            draw_key_signature(img, st, rep.new_fifths, kstart, kend, il, samples.get(new_kind), old_spacing)

        # 4. 그리기: 머리, 덧줄, 기둥 연장, 임시표
        filled_w, filled_h = head_shape(ink, heads, st, t_line, il) or page_shape   # 지우기 전 잉크에서 잰다 (ink는 page.binary라 그대로다)
        for p in plans:
            h = p.head
            y_new = st.bottom - p.step_new * il / 2
            w, hh = filled_w, filled_h
            if getattr(h, "big_ring", False) and p.hollow:           # 큰 동그라미 2분음표는 원래 크기 (구멍 + 고리)
                w, hh = max(w, 0.95 * h.w), max(hh, 0.95 * h.h)
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
            drawn_heads.append((h.x, y_new, w, hh, p.hollow))
            if p.dot and p.step_new != p.step_old:                     # 점은 칸에 둔다: 줄 위 음이면 바로 위 칸
                bx0, by0, bx1, by1 = p.dot
                dot_step = p.step_new if p.step_new % 2 else p.step_new + 1
                stamp(img, harvest_glyph(ink, p.dot), (bx0 + bx1) / 2, st.bottom - dot_step * il / 2)
            if p.acc_new:
                if (smp := samples.get(p.acc_new)):
                    stamp(img, smp[0], h.x - w / 2 - 0.75 * il, y_new + smp[1])
                else:
                    draw_glyph(img, p.acc_new, h.x - w / 2 - 0.75 * il, y_new, il)

        if arcs:
            arc_jobs.append((arcs, int(round(-rep.steps * il / 2)), st, t_line))
            rep.arcs += len(arcs)

    # 5. 코드 글자: Vision이 읽은 코드 상자를 Swift 이조기로 옮겨 같은 글꼴로 다시 쓴다
    forced: dict = {}
    # 검수가 찾은, 상자가 없어 안 바뀐 옛 코드: 그 자리를 코드 상자로 더한다 (옛 글자는 Vision이 원본에서 읽은 것)
    for fsi, x0, y0, x1, y1, ftext, *new_text in fixes.get("add_chords", []):
        if fsi < len(chord_boxes) and not any(min(b.x1, x1) - max(b.x0, x0) > 0 for b in chord_boxes[fsi]):
            if new_text and new_text[0] and ftext == new_text[0]:     # 사용자가 새 이름만 알려 줬다: 옛 이름은 거꾸로 옮겨 얻는다
                from . import chordoverlay
                try:
                    ftext = chordoverlay._transpose_tokens([new_text[0]], -semitones)[0][0] or ftext
                except Exception:
                    ftext = "?"
            nb = text.TextBox(ftext, 1.0, x0, y0, x1, y1, "vision")
            chord_boxes[fsi].append(nb)
            chord_boxes[fsi].sort(key=lambda b: b.x0)
            if new_text and new_text[0]:
                fixes.setdefault("chords", []).append((fsi, (x0 + x1) / 2, (y0 + y1) / 2, new_text[0]))
    for fsi, fx, fy, ftext in fixes.get("chords", []):
        if fsi < len(chord_boxes):
            hit = next((b for b in chord_boxes[fsi] if b.x0 - 2 <= fx <= b.x1 + 2 and b.y0 - 0.5 * il <= fy <= b.y1 + 0.5 * il), None)
            if hit is not None:
                forced[id(hit)] = ftext
    rep.state.update(page=page, systems=systems, il=il, plans_all=plans_all, chord_boxes=chord_boxes, keysigs=keysigs, read=read)
    letters_new = np.zeros(img.shape[:2], bool)
    try:
        before_chords = img.copy()
        rep.chords = _rewrite_chords(page, semitones, img, chord_boxes, forced)
        letters_new = img.astype(np.int16) - before_chords.astype(np.int16) < -60    # 새 코드 글자 잉크
        _restore_after_chords(img, before_chords, systems, ink, drawn_heads, il)
    except Exception as e:                                            # 코드가 안 되어도 음표는 낸다
        rep.notes_.append(f"chords not rewritten: {str(e).splitlines()[0][:120]}")
    # 6. 이음줄·붙임줄은 맨 나중에 옮긴다: 위로 올라간 이음줄이 코드 상자에 들면 코드 지우기가 잘라 먹는다 (모든 능력 4째 줄).
    #    새 코드 글자 둘레(0.15il)는 지우지도 찍지도 않는다: 곡선이 글자 뒤로 지나가게 (이 세상: 올라간 붙임줄이 새 E♭·C를 덮었다)
    avoid = cv2.dilate(letters_new.astype(np.uint8), np.ones((max(3, int(0.3 * il)) | 1,) * 2, np.uint8)) > 0
    for arcs, dy, st_, t_ in arc_jobs:
        move_arcs(img, arcs, dy, st_, t_, avoid)

    moved_arcs = [(x0, y0, x0 + m.shape[1], y0 + m.shape[0], dy) for arcs, dy, _, _ in arc_jobs for y0, x0, m in arcs]
    rep.state.update(result=img.copy(), drawn_chords=getattr(_rewrite_chords, "drawn", []), semitones=semitones, src=src, ai=ai_t,
                     moved_arcs=moved_arcs)
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


# 찬양 악보에 실제로 나오는 코드 이름 (Vision만 읽은 코드를 거를 때)
PLAUSIBLE_CHORD = re.compile(r"^[A-G][#b]?(m|m7|m9|maj7|M7|7|9|6|2|4|sus|sus2|sus4|7sus4|add9|add2|dim|dim7|m7b5|aug|\+|m6|69)?(/[A-G][#b]?)?$")


def _fix_vision_accidentals(rows, fifths: int) -> None:
    """AI가 못 읽어 Vision 글자만 있는 코드의 근음 ♯·♭: Vision은 올린 ♯을 ♭으로 읽거나 아예 놓친다. ♯ 조에서 조표에 ♯이 붙은 글자를
    ♭으로 읽었으면 ♯이다 (♭ 조도 거꾸로). 하나님은 너를 지키시는자: E장조의 G♯을 "Gb"로 — 그대로 두면 +2가 A♯이 아니라 A♭이 된다."""
    sharps, flats = "FCGDAEB"[:max(0, fifths)], "BEADGCF"[:max(0, -fifths)]
    for row in rows:
        for b in row:
            if b.src == "vision" and re.match(r"[A-G](?![#b])", b.text) and (b.text[0] in sharps or b.text[0] in flats):
                # 임시표가 없는데 조표가 바꾸는 글자면 조표대로 (이 세상: A♭장조의 B♭m⁷을 Vision이 ♭ 없이 "Bm7"로)
                b.text = b.text[0] + ("#" if b.text[0] in sharps else "b") + b.text[1:]
                continue
            m = re.match(r"([A-G])([#b])", b.text)
            if b.src != "vision" or not m:
                continue
            if m[2] == "b" and m[1] in sharps:
                b.text = m[1] + "#" + b.text[2:]
            elif m[2] == "#" and m[1] in flats:
                b.text = m[1] + "b" + b.text[2:]


def _volta_bar(ink: np.ndarray, box: tuple[int, int, int, int], il: float) -> tuple[int, int] | None:
    """상자 꼭대기가 1·2번 괄호의 세로줄에 걸렸는가: 상자 꼭대기 안쪽 0.4il을 가는 세로줄(0.3il 폭 이하)이 다 지나고, 그 줄의
    꼭대기에서 오른쪽으로만 괄호의 선이 1.8il 넘게 뻗는다 (괄호선은 점선이기도 해서 처음~끝 길이로). (세로줄 오른쪽 끝 열, 괄호선 바로 밑 행).
    띠에 잘린 글자(D·E의 세로획)는 윗획이 1il 남짓이고, Coda 기호 𝄌는 팔이 왼쪽에도 뻗는다 (보혈을 지나·주님의 임재·지금은 엘리야:
    "|1."이 코드 글자 덩어리에 붙는다; 괄호선이 띠 꼭대기에 딱 걸리기도 한다)."""
    x0, y0, x1, _ = box
    full = np.flatnonzero(ink[y0:y0 + max(2, int(0.4 * il)), x0:x1].all(axis=0))
    if not full.size:
        return None
    run = full[: 1 + int(np.argmax(np.diff(np.append(full, 10 ** 6)) > 1))]
    if run[-1] - run[0] + 1 > 0.3 * il:
        return None
    c = x0 + int(run[0])
    r = y0
    while r > 0 and ink[r - 1, c:c + len(run)].any():
        r -= 1
    rows = slice(max(0, r - 2), r + int(0.3 * il) + 3)                 # 줄 끝이 괄호선 위로 조금 솟기도 한다
    line = np.flatnonzero(ink[rows, c:c + int(3 * il)].any(axis=0))
    left = ink[rows, max(0, c - int(0.6 * il)):c].any(axis=0).sum()
    if not (line.size and line[-1] - line[0] >= 1.8 * il and left <= 0.15 * il):
        return None
    # 괄호선 바로 밑(0.25il)은 비어 있다: 번호·코드는 그 아래에서 시작한다. 글자 꼭대기(D(add9)의 D 세로획과 윗첨자들이 이어진 것)는
    # 바로 밑이 글자 몸이다 (주님은 산 같아서: 코드 6개의 근음이 잘렸다)
    band = ink[rows, c + len(run):c + int(3 * il)]
    filled = np.flatnonzero(band.sum(axis=1) >= 1.0 * il)
    if not filled.size:
        return None
    lb = rows.start + int(filled[-1]) + 2
    below = ink[lb:lb + max(3, int(0.25 * il)), c + int(1.0 * il):c + int(3.5 * il)]
    return (c + len(run), lb) if below.size and below.any(axis=0).mean() < 0.3 else None


def _after_volta(ink: np.ndarray, box: tuple[int, int, int, int], il: float) -> tuple[int, int] | None:
    """상자 왼쪽 끝이 괄호 세로줄이면 (번호 "1."을 건너뛴 근음 글자의 왼쪽 끝, 괄호선 바로 밑 행). 근음은 남은 조각 중 가장 큰 것의
    키 80% 넘는 조각들의 맨 왼쪽이다 (번호 글씨는 코드보다 작다). 상자 꼭대기가 괄호선에 걸려 있으면 같이 지워지니(지금은 엘리야
    "|2. A") 꼭대기도 그 밑으로 내린다. 괄호가 아니면 None."""
    x0, y0, x1, y1 = box
    v = _volta_bar(ink, (x0, y0, min(x1, x0 + int(1.5 * il)), y1), il)
    if v is None:
        return None
    c, lb = v
    y0 = max(y0, lb)
    reg = ink[y0:y1, c + 1:x1].astype(np.uint8)
    n, _, st, _ = cv2.connectedComponentsWithStats(reg, connectivity=8)
    if n < 2:
        return None
    hmax = st[1:, cv2.CC_STAT_HEIGHT].max()
    xs = [st[i, cv2.CC_STAT_LEFT] for i in range(1, n) if st[i, cv2.CC_STAT_HEIGHT] >= 0.8 * hmax]
    return c + 1 + int(min(xs)) - 1, y0


def snap_to_blobs(page, systems: list[System], rows, il: float, vision_rows=None):
    """hybrid.merge_chords가 준 상자는 Vision 중심 둘레의 작은 상자(x−0.3il…x+0.9il)라 옛 글자를 다 못 지운다(A♭의 윗첨자, Fm의 F).
    코드 띠의 잉크 덩어리(find_text_blobs: 윗첨자까지 뭉친 상자) 중 그 자리에 있는 것으로 바꾼다. Vision 상자가 있던 것(both)은
    그 Vision 상자에 걸치는 덩어리를 전부 합친다(Amaj7/E처럼 덩어리가 둘로 갈라진 코드도 통째로 지우게); AI만 읽은 코드는
    남은 덩어리 중 가장 가까운 것 하나에 붙인다 (앞 오선 끝의 코드가 다음 오선 앞에 놓이는 일이 있어, 남의 덩어리를 뺏지 않게).
    덩어리가 없으면 그대로 두고, 그리기는 잉크가 없어 건너뛴다."""
    ink_all = page.binary < 128
    for si, (sys_, row) in enumerate(zip(systems, rows)):
        y0, y1 = sys_.chord_band
        blobs = [(bx0, y0 + by0, bx1, y0 + by1) for bx0, by0, bx1, by1 in text.find_text_blobs(page.binary[y0:y1], il)]
        vrow = vision_rows[si] if vision_rows else []
        vboxes = [(v.x0, v.y0, v.x1, v.y1) for v in vrow]
        taken: set[int] = set()
        reserved: set[int] = set()               # 한 칸 민 AI 전용 코드 몫으로 남겨 둔 덩어리 (Vision 상자 쪽이 가져가지 않게)
        drop: list = []
        # Vision만 읽은 코드와 AI만 읽은 코드가 사이에 짝지어진 코드 없이 이웃하면 같은 코드다 (찬양의 열기: Vision의 "Cm"과 AI의
        # Em7 — 근음이 달라 짝이 안 됐다). AI 이름을 쓰고 Vision 자리를 쓴다; 흐린 캡처에서 AI는 91–100%, Vision은 48–62%
        # 같은 앵커 틈 안에서는 왼쪽부터 차례로 짝짓는다 (가장 가까운 것을 고르면 Cm이 Em7 대신 Asus4를 받았다 — 찬양의 열기)
        both_x = sorted(b.x0 for b in row if b.src == "both")
        # 다만 AI 코드 어림 자리 가까이에 Vision이 못 읽은 글자 덩어리가 Vision 상자보다 가까이 있으면 그게 그 코드의 자리다
        # (하나님은 너를 지키시는자: AI는 줄 끝 G♯을 빼먹었는데, AI의 E가 10il 떨어진 Vision "G♭" 자리에 써지고 진짜 E는 그대로였다)
        free = [((bb[0] + bb[2]) / 2, bb) for bb in blobs if bb[3] - bb[1] >= 0.8 * il
                and not any(v[0] <= (bb[0] + bb[2]) / 2 <= v[2] for v in vboxes)]

        def own_blob(c, v):
            cx = c.x0 + 0.3 * il
            dv = abs((v.x0 + v.x1) / 2 - cx)
            return any(abs(mx - cx) < dv and bb[2] - bb[0] <= (len(c.text) + 1) * il for mx, bb in free)
        # AI가 "X…, X"로 읽은 두 코드를 Vision이 앞의 것만 "X"로 읽으면 정렬은 AI의 뒤 X를 그 상자에 짝짓는다 (모든 능력: C♯sus⁴/G♯
        # 자리에 D♯이 써지고 바로 옆 진짜 C♯은 그대로). 짝 바로 왼쪽의 AI 전용 코드가 같은 근음으로 시작하고 더 길며, Vision 상자
        # 바로 오른쪽(1il 안)에 Vision이 못 읽은 덩어리가 있으면 한 칸씩 민다: 긴 이름이 Vision 자리를, 짝의 이름이 그 덩어리를
        ordered = sorted(row, key=lambda b: b.x0)
        for c, n in zip(ordered, ordered[1:]):
            rn = re.match(r"[A-G][#b]?", n.text)
            if c.src != "claude" or n.src != "both" or not rn or not c.text.startswith(rn.group(0)) or len(c.text) <= len(n.text):
                continue
            vb = next((v for v in vboxes if v[0] <= n.x0 + 0.3 * il <= v[2]), None)
            nxt = [bb for mx, bb in free if vb is not None and vb[2] - 0.2 * il <= bb[0] <= vb[2] + il]
            if nxt:
                bb = min(nxt)
                reserved.add(blobs.index(bb))
                c.text, n.text = n.text, c.text                 # n(짝, Vision 자리)은 긴 이름, c(AI 전용)는 짧은 이름을
                c.x0, c.x1 = bb[0] - 0.3 * il, bb[2]           # 덩어리 자리로 (아래 AI 전용 자리 찾기가 cx = x0 + 0.3il로 쓴다)
        for v in sorted((b for b in row if b.src == "vision"), key=lambda b: b.x0):
            cands = sorted((c for c in row if c.src == "claude" and not any(min(v.x0, c.x0) < bx < max(v.x0, c.x0) for bx in both_x)
                            and not own_blob(c, v)),
                           key=lambda c: c.x0)
            if cands:
                c = cands[0]
                v.text, v.src = c.text, "both"
                row.remove(c)
        # Vision만 읽은 코드도 제 Vision 상자(와 거기 걸친 덩어리)로 넓힌다: 작은 상자로 두면 앞 글자가 남고(Fm의 F), 이웃의 한계도 틀린다
        for src in ("both", "vision", "claude"):
            for b in row:
                if b.src != src or not blobs:
                    continue
                cx = b.x0 + 0.3 * il
                vb = next((v for v in vboxes if v[0] <= cx <= v[2]), None) if src != "claude" else None
                if vb is not None:
                    # Vision이 한 코드를 둘로 읽어(Dm/F를 "D"와 "F") AI 이름이 앞 상자에만 붙으면, 바로 오른쪽(0.3il 안)의
                    # Vision 전용 상자가 AI 이름의 뒷부분이면 그 상자까지 한 코드로 삼고 그 전용 코드는 버린다
                    vtext = next((v.text for v in vrow if (v.x0, v.y0, v.x1, v.y1) == vb), "")
                    for v2 in row:
                        if v2.src != "vision" or v2 is b or not (len(b.text) > len(vtext) and b.text.endswith(v2.text)):
                            continue
                        c2 = v2.x0 + 0.3 * il                     # 합친 상자는 중심 둘레의 작은 상자: 원래 Vision 상자를 찾는다
                        vb2 = next((v for v in vboxes if v[0] <= c2 <= v[2]), None)
                        if vb2 is not None and vb[2] - 0.5 * il <= vb2[0] <= vb[2] + 0.3 * il:   # 흐리면 상자가 0.5il쯤 겹친다 (하나님은: E+D♯)
                            vb = (vb[0], min(vb[1], vb2[1]), vb2[2], max(vb[3], vb2[3]))
                            drop.append(v2)
                    # 상자에 걸치는 덩어리 중 다른 Vision 상자가 더 잘 품는 것(중심이 그 안)은 그 코드의 것이다
                    # (C♯sus4/G♯의 상자가 이웃 C♯까지 3px 걸쳐 C♯ 글자를 삼키고, C♯은 조각 하나에 붙었다)
                    # (중심이 이 상자 안에도 들면 같은 코드로 본다: Vision이 B⁷m을 위아래 두 상자로 읽은 경우)
                    def foreign(bb):
                        mid = (bb[0] + bb[2]) / 2
                        return not (vb[0] <= mid <= vb[2]) and any(v is not vb and v[0] <= mid <= v[2] for v in vboxes)
                    # 왼쪽으로 삐져나온 덩어리는 제 폭의 30% 넘게 상자 안이어야 한다 (이 세상: 1·2번 괄호의 "2."가 A♭ 상자에 8px
                    # 걸쳐 같이 지워졌다). 오른쪽은 조금만 걸쳐도 받는다: Vision이 맨 글자만 잡으면 윗첨자(F²의 ², F♯의 ♯)가 그렇다
                    ks = [k for k, bb in enumerate(blobs) if k not in taken | reserved and bb[2] > vb[0] + 2 and bb[0] < vb[2] - 2
                          and (bb[0] >= vb[0] or min(bb[2], vb[2]) - vb[0] > 0.3 * (bb[2] - bb[0]))
                          # 상자 바닥 밑에서 시작해 상자 열 밖으로 나가는 것은 늘임표·이음줄·빔이다 (이 세상 끝줄 A♭ 밑의 늘임표).
                          # 열 안에 든 것은 빔 거르기에 잘린 글자 다리다 (지금은 엘리야·찬양의 열기 A). 덩어리 상자는 0.3il씩 불어 있다
                          and (bb[1] < vb[3] - 0.1 * il or (bb[0] >= vb[0] - 0.4 * il and bb[2] <= vb[2] + 0.4 * il))
                          and not foreign(bb)]
                    # Vision 상자가 근음 글자를 빼고 잡기도 한다(F♯m을 "♯m"만): 바로 왼쪽 0.2il 안에 붙은, 다른 Vision 상자에
                    # 들지 않은 덩어리는 같은 코드다
                    # (근음 글자라면 바닥이 코드 바닥과 같다: 1·2번 괄호의 "2."는 바닥이 0.5il 위라 받지 않는다 — 주 이름 찬양)
                    # Vision이 근음 글자부터 읽었으면(A♭을 "Ab") 왼쪽 덩어리는 근음이 아니다 (이 세상: 왼쪽에 붙은 Coda 기호를 지웠다)
                    left_ok = not (re.match(r"[A-G]", vtext) and vtext[0] == b.text[:1])
                    while ks and left_ok:
                        left = min(blobs[k][0] for k in ks)
                        bottom = max(blobs[k][3] for k in ks)
                        more = [k for k, bb in enumerate(blobs) if k not in taken and k not in ks and 0 <= left - bb[2] <= 0.2 * il
                                and abs(bb[3] - bottom) <= 0.25 * il
                                and not any(v[0] <= (bb[0] + bb[2]) / 2 <= v[2] for v in vboxes if v is not vb)]
                        if not more:
                            break
                        ks += more
                    # AI 이름이 Vision 글자보다 길면(A → Asus4) 바로 오른쪽에 붙은 덩어리("sus4")도 같은 코드다: 안 지우면 새 글자가 그 위에 겹친다
                    if ks and len(b.text) > len(vtext):
                        right_edge = max(blobs[k][2] for k in ks)
                        ks += [k for k, bb in enumerate(blobs) if k not in taken | reserved and k not in ks and -0.15 * il <= bb[0] - right_edge <= 0.3 * il
                               and bb[1] < vb[3] and bb[3] > vb[1] and not any(v[0] <= (bb[0] + bb[2]) / 2 <= v[2] for v in vboxes if v is not vb)]
                    if ks:
                        taken.update(ks)
                        b.x0, b.y0 = min(blobs[k][0] for k in ks), min(blobs[k][1] for k in ks)
                        b.x1, b.y1 = max(blobs[k][2] for k in ks), max(blobs[k][3] for k in ks)
                        # AI 이름이 Vision 글자보다 길지 않으면 오른쪽에 더 쓸 글자가 없다: 번진 덩어리가 옆 기호까지 품어도
                        # (이 세상: D♭과 Coda 기호가 한 덩어리) Vision 상자 끝 너머로 넓히지 않는다
                        if len(b.text) <= len(vtext):
                            b.x1 = min(b.x1, vb[2] + 0.3 * il)
                        # 띠보다 높이 쓰인 코드(이 세상 끝줄의 A♭)는 덩어리가 띠에 잘려 있다: 띠 위는 Vision 상자대로.
                        # (띠를 넓혀 덩어리를 다시 찾으면 위의 "al Coda"가 Fm에 붙어 지워졌다)
                        if vb[1] < y0:
                            b.y0, b.x1 = min(b.y0, vb[1]), max(b.x1, vb[2])
                        # 1·2번 괄호의 "|1."이 근음 글자와 한 덩어리(Vision 상자도 같이)면 괄호는 빼고 근음부터 (주님의 임재 "|1.C²")
                        if b.y0 <= y0 + 1:
                            nv = _after_volta(ink_all, (int(b.x0), int(b.y0), int(b.x1), int(b.y1)), il)
                            if nv is not None:
                                b.x0, b.y0 = nv
                        # Vision은 맨 글자(F)로 읽었는데 AI가 접미를 붙였고(Fmaj7) 잉크 덩어리가 대문자 하나 폭(높이의 0.95배 아래)이면
                        # AI가 지어낸 것이다 (주님 다시 오실때까지). 접미가 있으면 덩어리가 글자보다 넓다
                        if re.fullmatch(r"[A-G]", vtext) and b.text.startswith(vtext) and len(b.text) > 1:
                            from .chordoverlay import _root_letter
                            ink8 = (page.binary < 128).astype(np.uint8)
                            bx = (int(b.x0), int(b.y0), int(b.x1), int(b.y1))
                            rl = _root_letter(ink8, bx)
                            cols = np.flatnonzero(ink8[bx[1]:bx[3], bx[0]:bx[2]].any(axis=0))    # 덩어리 상자는 0.3il씩 불어 있다: 잉크 폭으로
                            # 잉크 폭이 근음 대문자 폭보다 대문자 높이의 0.2배도 안 넓으면 접미가 없다 (윗첨자 ²는 덩어리를 키도 키우므로
                            # 덩어리의 가로세로비로는 못 가른다: 나 주님의 기쁨 F²)
                            # 바로 오른쪽(0.5il 안)에 다른 덩어리(따로 떨어진 "sus4")가 있으면 접미가 있는 것이다 (주를 위한 이곳에 A sus4)
                            tail = any(-0.1 * il <= bb[0] - b.x1 <= 0.5 * il and bb[1] < b.y1 and bb[3] > b.y0 for bb in blobs if bb is not None)
                            if rl and cols.size and not tail \
                               and (cols[-1] - cols[0] + 1) - (rl[0][2] - rl[0][0]) <= 0.2 * (rl[0][3] - rl[0][1]):
                                b.text = vtext
                        continue
                # AI만 읽은 코드의 x는 이웃 코드 사이를 고르게 나눈 어림이라 크게 빗나간다 (찬양의 열기: Vision이 놓친 D가 3il 왼쪽).
                # 양옆의 자리 잡힌 코드 사이(앵커 틈) 안에서, 안 쓴 덩어리 중 가장 가까운 것 (틈 폭의 절반 또는 1.5il 안)
                if src == "claude":
                    # 기호 덩어리는 자리 후보가 아니다: 못 찾으면 옛 코드가 남지만 기호를 지우는 것보다 낫다
                    # (주 이름 찬양: AI만 읽은 B가 Coda 기호에, 보혈을 지나: D가 1번 괄호의 "|1."에 써졌다)
                    def symbol(bb):
                        bx = (int(bb[0]), int(bb[1]), int(bb[2]), int(bb[3]))
                        if cross_shaped(ink_all, bx):
                            return True
                        return bx[1] <= y0 + 1 and _volta_bar(ink_all, bx, il) is not None
                    left = max((o.x1 for o in row if o is not b and o.src != "claude" and o.x0 < cx), default=sys_.staff.left)
                    right = min((o.x0 for o in row if o is not b and o.src != "claude" and o.x0 > cx), default=sys_.staff.right)
                    tol = max(1.5 * il, 0.5 * (right - left))
                    near = [k for k, bb in enumerate(blobs) if k not in taken and left - 2 < (bb[0] + bb[2]) / 2 < right + 2
                            and bb[0] - tol <= cx <= bb[2] + tol and bb[3] - bb[1] >= 0.8 * il        # 윗첨자 조각(⁴)은 안 됨
                            and bb[2] - bb[0] <= (len(b.text) + 1.6) * il                             # 지시문("Last time")은 안 됨
                            and not symbol(bb)]
                    # (+0.6il: 덩어리 상자는 양옆 0.3il씩 불어 있다 — 보혈을 지나 D가 52px > 2il이라 빠지고 C 자리로 밀렸다)
                else:
                    near = [k for k, bb in enumerate(blobs) if k not in taken and bb[0] - 0.5 * il <= cx <= bb[2] + 0.5 * il]
                if near:
                    k = min(near, key=lambda k: abs((blobs[k][0] + blobs[k][2]) / 2 - cx))
                    ks = [k]
                    if src == "claude":                           # 한 코드가 덩어리 둘로 갈라진 것(D/F♯의 올린 ♯)은 함께
                        for k2, bb in enumerate(blobs):
                            # 위아래 틈은 0.6il까지, 가로로 안에 든 조각은 0.9il까지: 빔 거르기에 잘린 같은 글자다
                            # (찬양의 열기 끝줄 A의 꼭짓점이 0.7il 위에 따로 남았다)
                            inside = bb[0] >= blobs[k][0] - 0.35 * il and bb[2] <= blobs[k][2] + 0.35 * il
                            if k2 not in taken and k2 != k and min(bb[2], blobs[k][2]) - max(bb[0], blobs[k][0]) > -0.35 * il \
                               and min(bb[3], blobs[k][3]) - max(bb[1], blobs[k][1]) > -(0.9 if inside else 0.6) * il \
                               and bb[2] - bb[0] < max(2.5, len(b.text) + 1.6) * il:   # 위아래로 갈라진 A도, 떨어진 "sus4"(Asus4)도
                               # (한 덩어리에서 고르는 한도와 같게: 보혈을 지나 D⁷이 조각 하나에만 붙어 D가 남았다)
                               # (2.5il: 덩어리 상자는 0.3il씩 불어 있어 A의 다리 조각이 2.04il — 지금은 엘리야; 괄호선 "2. ----"은 6il)
                                ks.append(k2)
                    taken.update(ks)
                    b.x0, b.y0 = min(blobs[k][0] for k in ks), min(blobs[k][1] for k in ks)
                    b.x1, b.y1 = max(blobs[k][2] for k in ks), max(blobs[k][3] for k in ks)
                elif src == "claude":
                    # 맞는 덩어리가 없으면, 너무 넓은 덩어리(글자에 빔 끝·기둥이 붙었다: 주님 한 분만으로의 G) 안의 글자만 한 잉크
                    # 조각(키 0.8il 넘게, 폭 0.4il~글자 수+1 il) 중 가장 가까운 것을 쓴다
                    pieces = []
                    for k, bb in enumerate(blobs):
                        if k in taken or not (left - 2 < (bb[0] + bb[2]) / 2 < right + 2) or not (bb[0] - tol <= cx <= bb[2] + tol) \
                           or bb[2] - bb[0] <= (len(b.text) + 1.6) * il or symbol(bb):
                            continue
                        bx0, by0, bx1, by1 = (int(v) for v in bb)
                        n_, _, st_, _ = cv2.connectedComponentsWithStats(ink_all[by0:by1, bx0:bx1].astype(np.uint8), 8)
                        cs = [(x, y, w_, h_) for x, y, w_, h_, _ in st_[1:] if w_ >= 0.25 * il]
                        for x, y, w_, h_ in cs:
                            if not (h_ >= 0.8 * il and h_ <= 2.5 * il and 0.4 * il <= w_ <= (len(b.text) + 1) * il):
                                continue
                            # 낱말 속 글자는 아니다: 옆 1.5il 안에 바닥이 같은(0.2il 안) 비슷한 키의 조각이 있으면 낱말이다
                            # (주 이름 찬양: "D.S. al Coda"의 a·S에 B를 써 넣었다; 점으로 떨어진 D.S.도 1.5il 안이다)
                            gx, gy = bx0 + x, by0 + y                   # 덩어리 상자가 낱말을 자르기도 해서 둘레 창에서 잰다
                            wx0, wy0 = max(0, int(gx - 2 * il)), max(0, int(gy - 0.3 * il))
                            win = ink_all[wy0:int(gy + h_ + 0.3 * il), wx0:int(gx + w_ + 2 * il)].astype(np.uint8)
                            m_, _, wst, _ = cv2.connectedComponentsWithStats(win, 8)
                            if any(w2 >= 0.1 * il and h2 >= 0.5 * h_ and w2 <= 1.4 * h2 and abs((wy0 + y2 + h2) - (gy + h_)) <= 0.2 * il   # l·i도; 납작한 늘임표는 글자가 아니다
                                   and (0 <= wx0 + x2 - (gx + w_) <= 1.5 * il or 0 <= gx - (wx0 + x2 + w2) <= 1.5 * il)
                                   for x2, y2, w2, h2, _ in wst[1:]):
                                continue
                            pieces.append((k, (bx0 + x, by0 + y, bx0 + x + w_, by0 + y + h_)))
                    if pieces:
                        k, pb = min(pieces, key=lambda p: abs((p[1][0] + p[1][2]) / 2 - cx))
                        taken.add(k)
                        b.x0, b.y0, b.x1, b.y1 = pb
                    else:
                        drop.append(b)                 # 자리를 모르는 AI 전용 코드: 임시 상자가 이웃 글자에 걸치면 그걸 덮어쓴다
        # Vision만 읽은 "코드"가 다른 코드의 상자 안에 있으면 그 코드의 윗첨자 조각이다 (쌓인 ⁷₄를 "A"로, 올린 ⁴를 "D"로):
        # 지우면 안 되고, 옮겨 쓰면 그 코드 위에 글자가 덧씌워진다
        # Vision만 읽은 코드는 AI가 못 본 것이라 믿음이 약하다. 버리는 것: 다른 코드의 상자 안(쌓인 ⁷₄를 "A"로), 다른 코드와 폭의
        # 절반 넘게 겹침(같은 Fm을 "Em"으로 한 번 더), 찬양 악보에 없는 이름(1·2번 괄호의 "E1"·"E13"), 글자 수에 비해 너무 넓은 상자
        # ("Coda"를 "C"로)
        for b in row:
            if b.src == "vision" and b not in drop:
                cx, cy = (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2
                if any(o is not b and o not in drop and o.x0 <= cx <= o.x1 and o.y0 - 2 <= cy <= o.y1 + 2 for o in row):
                    drop.append(b)
                elif any(o is not b and o not in drop and min(o.x1, b.x1) - max(o.x0, b.x0) > 0.5 * min(o.x1 - o.x0, b.x1 - b.x0)
                         for o in row):
                    drop.append(b)
                elif not PLAUSIBLE_CHORD.match(b.text):
                    drop.append(b)
                elif (b.x1 - b.x0) > (len(b.text) + 1) * max(1, b.y1 - b.y0):
                    drop.append(b)
        for b in drop:
            row.remove(b)
        _read_orphans(page, row, blobs, y0, il, ink_all)
    return rows


def _read_orphans(page, row, blobs, band_top: int, il: float, ink_all: np.ndarray) -> None:
    """AI도 Vision도 못 읽은 코드 (찬양의 열기 A, 나 주님의 기쁨 줄 끝 C): 어느 코드도 안 가져간 덩어리 중 글자 하나 모양
    (키 0.8~2.5il, 폭 0.4il~키의 1.4배, 바닥이 줄의 코드 바닥과 0.3il 안)이고 기호·낱말 속 글자가 아닌 것을 따로 잘라 읽는다.
    Vision은 글자 하나짜리를 무시하므로 같은 조각을 이어 붙여 읽는다 (read_tiled). 코드로 읽힌 것만 Vision 코드로 더한다.
    빔 거르기에 위아래로 잘린 글자(A의 꼭짓점)는 가로로 겹치고 0.9il 안에 붙은 조각을 합친다."""
    if len(row) < 2:
        return
    base = float(np.median([b.y1 for b in row]))
    def used(bb):
        return any(min(bb[2], b.x1) - max(bb[0], b.x0) > 0.3 * (bb[2] - bb[0]) for b in row)
    # 글자 하나가 될 수 없는 넓은 덩어리(괄호 줄·빔)는 묶지 않는다: 바로 밑 글자와 한 무리가 되어 같이 버려진다 (지금은 엘리야: "1." 괄호 밑 A)
    free = [bb for bb in blobs if not used(bb) and bb[2] - bb[0] <= 4 * il]
    groups: list[list] = []
    for bb in sorted(free, key=lambda bb: bb[1]):
        g = next((g for g in groups if min(bb[2], max(x[2] for x in g)) - max(bb[0], min(x[0] for x in g)) > 0.5 * (bb[2] - bb[0])
                  and bb[1] - max(x[3] for x in g) <= 0.9 * il), None)
        (g.append(bb) if g is not None else groups.append([bb]))
    for g in groups:
        bx = (int(min(x[0] for x in g)), int(min(x[1] for x in g)), int(max(x[2] for x in g)), int(max(x[3] for x in g)))
        # 덩어리 상자는 0.3il씩 불었지만 글자 끝을 자르기도 한다 (나 주님의 기쁨 C의 오른쪽): 상자에 걸친 잉크 조각 전체로
        wx0, wy0 = max(0, int(bx[0] - il)), max(0, int(bx[1] - il))
        win = ink_all[wy0:int(bx[3] + il), wx0:int(bx[2] + il)].astype(np.uint8)
        n_, lab_, wst, _ = cv2.connectedComponentsWithStats(win, 8)
        core = lab_[bx[1] - wy0:bx[3] - wy0, bx[0] - wx0:bx[2] - wx0]
        ids = [i for i in np.unique(core) if i and wst[i][4] >= 0.05 * il * il]
        # 빔·이음줄 토막이다 (이 세상: 빔 끝을 "Ab"로 읽었다). 창 가장자리에 닿은 조각은 창 밖으로 더 뻗는다
        if not ids or any(wst[i][2] > 2.5 * il or wst[i][0] == 0 or wst[i][0] + wst[i][2] >= win.shape[1] for i in ids):
            continue
        x0, y0 = wx0 + min(wst[i][0] for i in ids), wy0 + min(wst[i][1] for i in ids)
        x1, y1 = wx0 + max(wst[i][0] + wst[i][2] for i in ids), wy0 + max(wst[i][1] + wst[i][3] for i in ids)
        own = (wx0, wy0, np.isin(lab_, ids))               # 글자 조각 (창 좌표): 읽을 때 옆 잉크를 지운다
        if any(min(x1, b.x1) - max(x0, b.x0) > 0 and min(y1, b.y1) - max(y0, b.y0) > 0 for b in row):
            continue
        w, h = x1 - x0, y1 - y0
        if not (0.8 * il <= h <= 2.5 * il and 0.4 * il <= w <= 1.4 * h and abs(y1 - base) <= 0.3 * il):
            continue
        if cross_shaped(ink_all, (x0, y0, x1, y1)) or (y0 <= band_top + 1 and _volta_bar(ink_all, (x0, y0, x1, y1), il) is not None):
            continue
        # 낱말 속 글자는 아니다: 옆 1.5il 안에 바닥이 같은 비슷한 키의 글자 모양 조각이 있으면 낱말이다 (D.C.·Coda)
        wx0, wy0 = max(0, int(x0 - 2 * il)), max(0, int(y0 - 0.3 * il))
        n_, _, wst, _ = cv2.connectedComponentsWithStats(ink_all[wy0:int(y1 + 0.3 * il), wx0:int(x1 + 2 * il)].astype(np.uint8), 8)
        if any(w2 >= 0.1 * il and h2 >= 0.5 * h and w2 <= 1.4 * h2 and abs((wy0 + yy + h2) - y1) <= 0.2 * il
               and (0 <= wx0 + xx - x1 <= 1.5 * il or 0 <= x0 - (wx0 + xx + w2) <= 1.5 * il) for xx, yy, w2, h2, _ in wst[1:]):
            continue
        # 글자 조각만 남기고 읽는다: 여백에 든 옆 기호(괄호 번호 "1."의 점)를 "•AA"로 읽었다 (지금은 엘리야). 회색 테까지 2px 넓힌다
        keep = np.zeros(ink_all.shape, bool)
        keep[own[1]:own[1] + own[2].shape[0], own[0]:own[0] + own[2].shape[1]] = own[2]
        keep = cv2.dilate(keep.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        r = text.read_tiled(page, (x0, y0, x1, y1), il, text.CHORD_VOCAB, 3, keep) or text.read_tiled(page, (x0, y0, x1, y1), il, text.CHORD_VOCAB, 5, keep)
        name = text.normalize_chord(re.sub(r"^([A-G])[Il|]$", r"\1", r.text)) if r else ""
        if r and r.conf >= 0.9 and text.is_chord(name) and PLAUSIBLE_CHORD.match(name):
            row.append(text.TextBox(name, r.conf, x0, y0, x1, y1, "vision"))
    row.sort(key=lambda b: b.x0)


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


def _restore_after_chords(img: np.ndarray, before: np.ndarray, systems, ink: np.ndarray, drawn_heads, il: float) -> None:
    """코드는 상자째 지우고 다시 쓴다 (흐린 그림의 회색 테 때문). 상자는 글자에 붙은 잉크까지 자라서 오선 줄·머리·기둥·이음줄도
    같이 지워졌다 (이 세상: 맨 윗줄에 붙은 코드 11곳에서 줄이 끊기고, C7 밑 머리가 반만 남았다). 코드 쓰기가 밝게 만든 점 중
    옛 글자가 아닌 것만 되돌린다. 새 글자는 더 어두워서 np.minimum에 그대로 남는다."""
    from . import chordoverlay
    region = np.zeros(img.shape, bool)
    for x0, y0, x1, y1 in getattr(chordoverlay.draw, "erased", []):
        region[y0:y1, x0:x1] = True
    lit = region & (img.astype(np.int16) - before.astype(np.int16) > 40)
    if not lit.any():
        return
    # 1. 오선 줄 행 (폭의 절반 넘게 검은 행): 옛 글자가 줄에 걸친 점도 줄 자리라 되돌려도 된다
    rows = np.zeros(img.shape[0], bool)
    for sys_ in systems:
        st = sys_.staff
        for y in st.lines:
            for yy in range(max(0, y - 6), min(img.shape[0], y + 7)):
                if ink[yy, st.left:st.right].mean() >= 0.5:
                    rows[yy] = True
                    seg = slice(st.left, st.right)
                    m = lit[yy, seg]
                    img[yy, seg][m] = np.minimum(img[yy, seg][m], before[yy, seg][m])
    # 2. 줄 행을 뺀 옛 잉크 덩어리 중 지운 상자 밖으로 4분의 1 넘게 나가는 것 (머리·기둥·이음줄·빔). 옛 글자는 상자 안에 다 들어
    # 있어 밖으로 안 나간다 (새 글자에 덮여 안 밝아진 점도 상자 안이다). 글자에 붙은 머리처럼 거의 다 안에 든 것은 그대로 둔다
    bink = (before < 128)
    bink[rows] = False
    ys, xs = np.nonzero(lit)
    pad = int(2 * il)
    Y0, Y1, X0, X1 = max(0, ys.min() - pad), ys.max() + pad + 1, max(0, xs.min() - pad), xs.max() + pad + 1
    n, labels, stats, _ = cv2.connectedComponentsWithStats(bink[Y0:Y1, X0:X1].astype(np.uint8), connectivity=8)
    sub_lit = lit[Y0:Y1, X0:X1]
    hit = np.bincount(labels[region[Y0:Y1, X0:X1]], minlength=n)
    tot = np.bincount(labels.ravel(), minlength=n)
    keep = np.zeros(n, bool)
    keep[1:] = (np.bincount(labels[sub_lit], minlength=n)[1:] > 0) & (tot[1:] - hit[1:] > 0.25 * tot[1:])
    # 옛 글자가 기둥·빔에 붙어 있으면 덩어리째 밖으로 나가 글자까지 살아난다 (지금은 엘리야: B 밑에 A, F♯ 밑에 E).
    # Vision이 글자를 읽은 상자 안은 되살리지 않는다 (그 안의 옮긴 머리는 3에서 다시 그린다)
    letters = np.zeros(img.shape, bool)
    lboxes = []
    for _, x0, y0, x1, y1, old, new_ in getattr(_rewrite_chords, "drawn", []):
        if new_ and new_ != old:
            lboxes.append((max(0, int(y0) - 1), int(y1) + 2, max(0, int(x0) - 1), int(x1) + 2))
            letters[lboxes[-1][0]:lboxes[-1][1], lboxes[-1][2]:lboxes[-1][3]] = True
    # Vision 상자가 글자 일부만 잡으면(지금은 엘리야 E: 아래 절반) 글자가 지운 상자 밖으로 나가 기둥처럼 보인다.
    # 글자만 한 덩어리가 글자 상자에 걸리면 옛 글자다 (빔·긴 이음줄처럼 큰 것은 글자에 걸려도 되살린다)
    on_letter = np.bincount(labels[letters[Y0:Y1, X0:X1]], minlength=n) > 0
    small = (stats[:, cv2.CC_STAT_WIDTH] <= 3 * il) & (stats[:, cv2.CC_STAT_HEIGHT] <= 2.5 * il)
    keep &= ~(on_letter & small)
    # 거의 다 지운 상자 안에 든 긴 곡선(3il 넘게 넓다: 이 세상의 이음줄은 E♭·Fm 상자 둘에 93%가 들었다)도 후보로 둔다. 글자가 붙은
    # 상자 안은 아래 세로획 검사가 막는다
    wide = (stats[:, cv2.CC_STAT_WIDTH] > 3 * il) & (np.bincount(labels[sub_lit], minlength=n) > 0)
    wide[0] = False
    keep |= wide
    # 글자 상자를 그냥 지나가는 곡선·빔(그 상자 안 부분에 글자 높이의 세로획이 없다: 세로 런 0.6il 이하)은 상자 안까지 되살린다
    # (이 세상: Vision 상자가 글자에 붙은 이음줄까지 품어 이음줄 가운데가 끊겼다). 상자마다 따로 잰다: 같은 이음줄이 이웃 코드의
    # F를 지나면 그 상자에서만 글자가 붙은 것이다
    sub_letters = letters[Y0:Y1, X0:X1]
    m = keep[labels] & sub_lit & ~sub_letters
    for ya, yb, xa, xb in lboxes:
        ya, yb, xa, xb = max(0, ya - Y0), max(0, yb - Y0), max(0, xa - X0), max(0, xb - X0)
        if yb <= ya or xb <= xa:
            continue
        lab_b, lit_b = labels[ya:yb, xa:xb], sub_lit[ya:yb, xa:xb]
        for i in np.unique(lab_b[lit_b]):
            if i == 0 or not keep[i]:
                continue
            part = lab_b == i
            runs = 0
            for col in part.T:
                if col.any():
                    d = np.diff(np.concatenate([[0], col.astype(np.int8), [0]]))
                    runs = max(runs, int((np.flatnonzero(d == -1) - np.flatnonzero(d == 1)).max()))
            if runs <= 0.6 * il:
                m[ya:yb, xa:xb] |= part & lit_b
            else:
                # 글자가 붙은 덩어리(이 세상: 이음줄이 Fm의 F에 닿는다)는 상자 밖에서 들어오는 가는 곡선을 따라 상자 안의 가는 잉크
                # (세로 런 0.4il 이하)만 잇는다. 글자 기둥은 굵어서 거기서 멈춘다
                g0, g1, h0, h1 = max(0, ya - 2), min(labels.shape[0], yb + 2), max(0, xa - 2), min(labels.shape[1], xb + 2)
                comp = labels[g0:g1, h0:h1] == i
                cvr = np.zeros(comp.shape, np.int32)
                for c in range(comp.shape[1]):
                    col = comp[:, c]
                    if col.any():
                        d = np.diff(np.concatenate([[0], col.astype(np.int8), [0]]))
                        for a_, b_ in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):
                            cvr[a_:b_, c] = b_ - a_
                thin_ = comp & (cvr <= 0.4 * il)
                inner = np.zeros_like(thin_)
                inner[ya - g0:yb - g0, xa - h0:xb - h0] = True
                seed = thin_ & ~inner                              # 상자 바로 밖(2px 테)의 가는 곡선
                nl, tl, tst, _ = cv2.connectedComponentsWithStats(thin_.astype(np.uint8), connectivity=8)
                # 상자 안의 길고 납작한 가는 조각도 곡선이다 (폭 2il 넘게, 키의 2배 넘게): 글자를 지나며 굵어진 곳에서 끊겨 밖과
                # 안 이어져도 (이 세상: Fm의 m 위). 글자의 가로획·m의 아치는 그보다 짧다
                longs = [j for j in range(1, nl) if tst[j][2] > 2 * il and tst[j][2] > 2 * tst[j][3]]
                ids_ = set(np.unique(tl[seed]).tolist()) | set(longs)
                ids_.discard(0)
                if not ids_:
                    continue
                grow = np.isin(tl, list(ids_)) & inner
                m[g0:g1, h0:h1] |= grow & sub_lit[g0:g1, h0:h1]
    # 새 코드 글자 하나하나의 상자(+0.15il)는 되살리지 않는다: 새 글자가 옛 것보다 넓으면 되살린 이음줄이 글자를 긋는다
    # (모든 능력 C♯m⁷: 옮기지 못한 붙임줄이 C의 몸통을 지났다). 곡선은 글자 뒤로 지나가게 끊긴다. 글자마다 따로 잰다: 코드 전체
    # 상자로 하면 키 작은 m 위를 지나는 이음줄까지 비었다 (이 세상 Gm)
    sub, sb = img[Y0:Y1, X0:X1], before[Y0:Y1, X0:X1]
    new_ink = (sub.astype(np.int16) - sb.astype(np.int16) < -60).astype(np.uint8)
    nn_, _, nst, _ = cv2.connectedComponentsWithStats(new_ink, connectivity=8)
    nst = np.array([st for st in nst[1:] if st[4] >= 0.03 * il * il] or np.zeros((0, 5), int)).reshape(-1, 5)
    nst = np.vstack([np.zeros((1, 5), int), nst])
    pad_ = max(1, int(0.15 * il))
    m_all = m.copy()
    for x, y, w_, h_, _ in nst[1:]:
        m[max(0, y - pad_):y + h_ + pad_, max(0, x - pad_):x + w_ + pad_] = False
    # 다만 그 안에서도 빔처럼 굵은 것(세로 런 0.35il 넘게; 붙임줄은 더 가늘다)은 새 글자 획 둘레(0.15il) 밖이면 되살린다
    # (주님은 산 같아서: B/D# 밑을 지나는 빔이 비었다)
    vr = np.zeros(sb.shape, np.int32)
    dark = sb < 128
    for c in range(dark.shape[1]):
        col = dark[:, c]
        if col.any():
            d = np.diff(np.concatenate([[0], col.astype(np.int8), [0]]))
            for a, b in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):
                vr[a:b, c] = b - a
    near_new = cv2.dilate(new_ink, np.ones((max(3, int(0.3 * il)) | 1,) * 2, np.uint8)) > 0
    m |= m_all & (vr > 0.35 * il) & ~near_new
    sub[m] = np.minimum(sub[m], sb[m])
    # 3. 새로 그린 머리 중 지운 자리에 걸린 것은 다시 그린다 (옮긴 머리는 옛 잉크가 아니라 1·2에서 안 돌아온다)
    for hx_, hy_, hw_, hh_, hollow_ in drawn_heads:
        y0, y1 = max(0, int(hy_ - hh_ / 2)), int(hy_ + hh_ / 2) + 1
        x0, x1 = max(0, int(hx_ - hw_ / 2)), int(hx_ + hw_ / 2) + 1
        if lit[y0:y1, x0:x1].any():
            draw_head(img, hx_, hy_, hw_, hh_, hollow_, il)


def _rewrite_chords(page, semitones: int, img: np.ndarray, boxes, forced: dict | None = None) -> int:
    from . import chordoverlay
    from .chordoverlay import Chord
    forced = forced or {}
    src_boxes = [b for row in boxes for b in row]
    chords = [Chord(b.x0, b.y0, b.x1, b.y1, b.text, "vision", col=k) for k, row in enumerate(boxes) for b in row]
    if not chords:
        return 0
    pieces = [p for c in chords for p in c.text.split("-")]
    moved, _ = chordoverlay._transpose_tokens(pieces, semitones)
    k = 0
    for c, b in zip(chords, src_boxes):
        n_ = len(c.text.split("-"))
        c.new = "-".join(moved[k:k + n_])
        k += n_
        if id(b) in forced:                              # 검수가 정한 새 이름
            c.new = forced[id(b)]
    ink = (page.binary < 128).astype(np.uint8)
    rgb = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
    font = chordoverlay.pick_font(ink, chords)
    rgb = chordoverlay.draw(rgb, ink, chords, font, uniform=True, erase_box=page.scale > 1.5)
    img[:] = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    _rewrite_chords.drawn = [(c.col, c.x0, c.y0, c.x1, c.y1, c.text, c.new) for c in chords]
    return sum(1 for c in chords if c.new and c.new != c.text)
