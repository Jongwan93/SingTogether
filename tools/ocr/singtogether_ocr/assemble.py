"""5단계 조립: 엔진 마디를 오선에 배정하고, 음표에 x좌표를 붙이고, 코드 기호를 가장 가까운 음표에 단다."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from . import debug
from .ingest import Page
from .layout import System
from .model import Transcription, key_alter
from .notes import SystemSymbols
from .text import TextBox


def place_notes(t: Transcription, systems: list[System], symbols: list[SystemSymbols]) -> None:
    """오선별 마디줄 수만큼 마디를 순서대로 배정하고, 오선 안 음표 머리와 음표를 순서대로 짝짓는다."""
    if not systems:
        return
    heads_per_system = [len(s.noteheads) for s in symbols]
    if sum(heads_per_system) >= 0.5 * sum(1 for n in t.notes if not n.is_rest):
        # 오선별 음표 머리 수에 맞춰 마디를 자른다 (마디줄보다 믿을 만하다)
        counts = _split_by_heads([sum(1 for n in m.notes if not n.is_rest) for m in t.measures], heads_per_system)
    else:
        counts = [max(1, len(s.barlines)) for s in symbols]
        if sum(counts) != len(t.measures):
            counts = _allocate(len(t.measures), counts)
    idx = 0
    for si, (sys_, syms, c) in enumerate(zip(systems, symbols, counts)):
        measures = t.measures[idx:idx + c]
        idx += c
        left, right = float(sys_.staff.left), float(sys_.staff.right)
        bars = sorted(syms.barlines)
        bars_ok = len(bars) in (c, c - 1)
        if not bars_ok:                                       # 마디줄 수가 안 맞으면 폭을 균등하게
            bars = list(np.linspace(left, right, c + 1))[1:-1]
        bars = [left] + bars + ([right] if len(bars) < c else [])
        for k, m in enumerate(measures):
            m.system = si
            m.x0, m.x1 = bars[min(k, len(bars) - 2)], bars[min(k + 1, len(bars) - 1)]
        st = sys_.staff
        heads = syms.noteheads
        # 머리 수가 엔진 음표 수와 같으면(오선 전체 → 안 되면 마디별) 음높이를 오선 위치에서 다시 읽는다. 엔진보다 정확하다.
        # 엔진이 같은 음이름·옥타브를 냈으면 엔진 것을 둔다 (임시표 보존). 수가 다르면 x만 비율로 배정.
        all_pitched = [n for m in measures for n in m.notes if not n.is_rest]
        groups = [(all_pitched, heads)] if len(all_pitched) == len(heads) else [
            ([n for n in m.notes if not n.is_rest], [h for h in heads if m.x0 <= h.x < m.x1] if bars_ok else heads)
            for m in measures]
        for pitched, hs in groups:
            if not pitched or not hs:
                continue
            if len(hs) == len(pitched):
                for n, h in zip(pitched, hs):
                    n.x, n.y = h.x, h.y
                    step = round((st.bottom - h.y) / (st.interline / 2))
                    letter, octave = "EFGABCD"[step % 7], 4 + (step + 2) // 7
                    if (n.step, n.octave) != (letter, octave):
                        n.step, n.octave, n.alter = letter, octave, key_alter(letter, t.fifths)
            else:
                for k, n in enumerate(pitched):
                    h = hs[round(k * (len(hs) - 1) / max(1, len(pitched) - 1))]
                    n.x, n.y = h.x, h.y
        for m in measures:                                   # 쉼표는 마디 안에서 시간 비율로
            if m.x0 is not None and m.length:
                pos = 0
                for n in m.notes:
                    if n.is_rest:
                        n.x = m.x0 + (m.x1 - m.x0) * float((pos + n.duration / 2) / m.length)
                    pos += n.duration
    for m in t.measures[idx:]:
        m.system = len(systems) - 1


def attach_chords(t: Transcription, chords: list[list[TextBox]], interline: float) -> None:
    """코드 글자의 왼쪽 끝이 음표 머리 근처에 온다. 가장 가까운 음표에 단다."""
    for si, boxes in enumerate(chords):
        notes = [n for m in t.measures if m.system == si for n in m.notes if n.x is not None]
        if not notes:
            continue
        for c in boxes:
            best = min(notes, key=lambda n: abs(n.x - (c.x0 + 0.3 * interline)))
            best.chord = c.text
    t.has_chords = True


def overlay(page: Page, t: Transcription, debug_dir: Path) -> None:
    img = debug.canvas(page.gray)
    il = page.interline
    for m in t.measures:
        if m.x0 is not None and m.system is not None:
            debug.text(img, [((m.x0 + 2, 14 + 20 * (m.system % 2)), str(m.number))], "staff", size=12)
        for n in m.notes:
            if n.x is None:
                continue
            y = n.y if n.y is not None else 0
            if not n.is_rest:
                debug.boxes(img, [(n.x - 0.6 * il, y - 0.6 * il, n.x + 0.6 * il, y + 0.6 * il)], "note")
                debug.text(img, [((n.x - 0.6 * il, y + 1.6 * il), n.name)], "note", size=11)
            if n.chord:
                debug.text(img, [((n.x - 0.6 * il, y - il), n.chord)], "chord", size=13)
    debug.caption(img, f"{page.name}  {t.engine}  measures {len(t.measures)}  chords {sum(1 for n in t.notes if n.chord)}")
    debug.save(debug_dir / f"{page.name}-5-assembled.png", img)


def _split_by_heads(notes_per_measure: list[int], heads_per_system: list[int]) -> list[int]:
    """마디를 순서대로 오선 수만큼의 구간으로 자르되, 구간의 음표 수가 오선의 머리 수와 가장 비슷하게 (DP)."""
    n, s = len(notes_per_measure), len(heads_per_system)
    prefix = [0]
    for c in notes_per_measure:
        prefix.append(prefix[-1] + c)
    INF = float("inf")
    best = [[INF] * (n + 1) for _ in range(s + 1)]     # best[k][i]: 앞 k개 오선에 앞 i개 마디
    choice = [[0] * (n + 1) for _ in range(s + 1)]
    best[0][0] = 0
    for k in range(1, s + 1):
        for i in range(n + 1):
            for j in range(i + 1):
                if best[k - 1][j] == INF:
                    continue
                cost = best[k - 1][j] + (prefix[i] - prefix[j] - heads_per_system[k - 1]) ** 2 + (0 if i > j else 50)
                if cost < best[k][i]:
                    best[k][i], choice[k][i] = cost, j
    counts, i = [], n
    for k in range(s, 0, -1):
        j = choice[k][i]
        counts.append(i - j)
        i = j
    return counts[::-1]


def _allocate(total: int, weights: list[int]) -> list[int]:
    """total을 weights 비율로 나눈다 (최대 나머지 방식)."""
    s = sum(weights) or 1
    raw = [total * w / s for w in weights]
    out = [int(r) for r in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - out[i], reverse=True)[:total - sum(out)]:
        out[i] += 1
    return out
