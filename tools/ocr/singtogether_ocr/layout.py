"""2단계 영역 분리: 오선(5줄 묶음) 검출, 각 오선의 위 띠(코드 기호)와 아래 띠(가사)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import debug
from .ingest import Page


@dataclass
class Staff:
    lines: list[int]      # 오선 5줄의 y
    left: int
    right: int

    @property
    def top(self) -> int: return self.lines[0]
    @property
    def bottom(self) -> int: return self.lines[-1]
    @property
    def interline(self) -> float: return (self.bottom - self.top) / 4


@dataclass
class System:
    staff: Staff
    chord_band: tuple[int, int]   # (y0, y1) 오선 위, 코드 기호가 있는 띠
    lyric_band: tuple[int, int]   # (y0, y1) 오선 아래, 가사가 있는 띠

    @property
    def box(self) -> tuple[int, int, int, int]:
        """엔진에 넘길 영역: 코드 띠 위부터 가사 띠 아래까지, 오선 좌우."""
        return (self.staff.left, self.chord_band[0], self.staff.right, self.lyric_band[1])


def find_systems(page: Page, debug_dir: Path | None = None) -> list[System]:
    il = page.interline
    staves = fit_staves(page.binary, find_staff_lines(page.binary), il)
    for s in staves:
        s.left, s.right = staff_extent(page.binary, s.lines)
    H = page.binary.shape[0]
    systems = []
    for k, s in enumerate(staves):
        above = (staves[k - 1].bottom + s.top) // 2 if k else 0
        below = (s.bottom + staves[k + 1].top) // 2 if k + 1 < len(staves) else H
        chord_band = (max(above, int(s.top - 5 * il)), int(s.top - 0.3 * il))
        lyric_band = (int(s.bottom + 0.3 * il), min(below, int(s.bottom + 5 * il)))
        systems.append(System(s, chord_band, lyric_band))
    if debug_dir:
        img = debug.canvas(page.gray)
        for sys_ in systems:
            debug.band(img, *sys_.chord_band, "chord")
            debug.band(img, *sys_.lyric_band, "lyric")
            debug.hlines(img, sys_.staff.lines, sys_.staff.left, sys_.staff.right, "staff")
        debug.caption(img, f"{page.name}  staves {len(systems)}  interline {il:.0f}px")
        debug.save(debug_dir / f"{page.name}-2-layout.png", img)
    return systems


def find_staff_lines(binary: np.ndarray) -> list[int]:
    """폭의 상당 부분을 가로지르는 검정 행 = 오선. 붙어 있는 행은 한 줄로 합친다."""
    ink = binary < 128
    proj = ink.sum(axis=1)
    thr = max(proj.max() * 0.3, ink.shape[1] * 0.2)
    groups: list[list[int]] = []
    for y in np.flatnonzero(proj >= thr):
        if groups and y - groups[-1][-1] <= 1:
            groups[-1].append(int(y))
        else:
            groups.append([int(y)])
    return [int(np.mean(g)) for g in groups]


def fit_staves(binary: np.ndarray, lines: list[int], interline: float) -> list[Staff]:
    """줄을 가까운 것끼리 묶고(간격 < 2il), 묶음마다 가로 투영에 가장 잘 맞는 5줄 빗(시작 y, 간격)을 찾는다.

    줄이 합쳐지거나 쪼개져 4줄·6줄로 보여도 복구된다.
    """
    if not lines:
        return []
    proj = (binary < 128).sum(axis=1).astype(float)
    clusters = [[lines[0]]]
    for y in lines[1:]:
        if y - clusters[-1][-1] < 2 * interline:
            clusters[-1].append(y)
        else:
            clusters.append([y])
    staves = []
    for c in clusters:
        if len(c) < 3:
            continue
        best = None
        for top in np.arange(c[0] - interline, c[0] + interline):
            for sp in np.arange(0.85 * interline, 1.15 * interline, 0.5):
                rows = [int(round(top + k * sp)) for k in range(5)]
                if rows[0] < 0 or rows[-1] >= len(proj):
                    continue
                s = sum(proj[max(0, r - 1):r + 2].max() for r in rows)
                if best is None or s > best[0]:
                    best = (s, rows)
        if best:
            staves.append(Staff(best[1], 0, 0))
    return staves


def staff_extent(binary: np.ndarray, lines: list[int]) -> tuple[int, int]:
    """5줄 중 3줄 이상에 검정이 있는 열의 범위 = 오선의 좌우 끝."""
    ink = binary < 128
    rows = np.clip(np.array([[y - 1, y, y + 1] for y in lines]).ravel(), 0, ink.shape[0] - 1)
    hits = ink[rows].reshape(len(lines), 3, -1).any(axis=1).sum(axis=0)
    xs = np.flatnonzero(hits >= 3)
    return (int(xs.min()), int(xs.max())) if xs.size else (0, ink.shape[1] - 1)
