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
        # 코드 기호는 오선 위 0.3–3il 안에 있다. 가사는 그 아래부터 다음 오선의 코드 띠 직전까지 (절이 여럿이면 길다).
        chord_top = max(staves[k - 1].bottom + int(0.3 * il) if k else 0, int(s.top - 3 * il))
        chord_band = (chord_top, int(s.top - 0.3 * il))
        # 가사 띠는 다음 오선 직전까지. 다음 오선의 코드 줄이 섞이는데, 그건 lyrics.read_lyrics가 글줄 단위로 걸러낸다.
        lyric_bottom = min(int(staves[k + 1].top - 0.3 * il), int(s.bottom + 9 * il)) if k + 1 < len(staves) else min(H, int(s.bottom + 9 * il))
        lyric_band = (int(s.bottom + 0.3 * il), max(int(s.bottom + 0.3 * il) + int(il), lyric_bottom))
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


def find_staff_lines(binary: np.ndarray, max_thickness: int = 6) -> list[int]:
    """폭의 60% 이상을 가로지르는 얇은 검정 행 = 오선. 붙어 있는 행은 한 줄로 합친다.
    가사 글줄도 폭을 꽤 채우지만 두껍고(글자 높이) 성기므로, 두께 제한과 높은 비율로 걸러낸다."""
    ink = binary < 128
    proj = ink.sum(axis=1)
    thr = max(proj.max() * 0.5, ink.shape[1] * 0.6)
    groups: list[list[int]] = []
    for y in np.flatnonzero(proj >= thr):
        if groups and y - groups[-1][-1] <= 1:
            groups[-1].append(int(y))
        else:
            groups.append([int(y)])
    return [int(np.mean(g)) for g in groups if len(g) <= max_thickness]


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


def clean_staff_image(page: Page, systems: list[System], debug_dir: Path | None = None) -> Page:
    """코드·가사 글줄을 흰색으로 지운 쪽. OMR 엔진이 한글 글자를 음표로 오인하는 것을 줄인다.
    오선 위아래 1.5il 안(덧줄 음표·붙임줄)은 건드리지 않고, 코드 띠·가사 띠 안의 Vision 글줄 상자만 지운다."""
    import dataclasses

    from . import ingest
    from .lyrics import recognize_lines

    il = page.interline
    g = page.gray.copy()
    H, W = g.shape

    def in_text_zone(y: float) -> bool:
        for s in systems:
            if s.staff.top - 1.5 * il <= y <= s.staff.bottom + 1.5 * il:
                return False
            if s.chord_band[0] <= y <= s.chord_band[1] or s.lyric_band[0] <= y <= s.lyric_band[1]:
                return True
        return False

    n = 0
    for _, _, _, (x0, y0, x1, y1) in recognize_lines(page.gray, ("ko-KR", "en-US")):
        if in_text_zone((y0 + y1) / 2):
            g[max(0, int(y0) - 2):min(H, int(y1) + 2), max(0, int(x0) - 2):min(W, int(x1) + 2)] = 255
            n += 1
    clean = dataclasses.replace(page, gray=g, binary=ingest.binarize(g))
    if debug_dir:
        img = debug.canvas(clean.binary)
        debug.caption(img, f"{page.name}  text lines masked {n}")
        debug.save(debug_dir / f"{page.name}-2-clean.png", img)
    return clean
