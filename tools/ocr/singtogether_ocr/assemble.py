"""5단계 조립: 엔진 마디를 오선에 배정하고, 음표에 x좌표를 붙이고, 코드 기호를 가장 가까운 음표에 단다."""
from __future__ import annotations

import os
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
        # 첫 마디의 왼쪽은 음자리표·조표·박자표 자리(find_symbols가 머리를 건너뛰는 폭과 같다)를 뺀 곳부터.
        # 이걸 빼지 않으면 첫 마디 음표들의 기대 x가 왼쪽으로 쏠려 한 머리씩 어긋나게 짝지어진다 (렌더링 악보 첫 마디 F#→G).
        if measures:
            head_start = left + (5 if si == 0 else 3.5) * sys_.staff.interline
            if measures[0].x1 is not None and head_start < measures[0].x1 - sys_.staff.interline:
                measures[0].x0 = max(measures[0].x0, head_start)
        st = sys_.staff
        heads = syms.noteheads
        il = st.interline

        def position_pitch(h):
            step = round((st.bottom - h.y) / (il / 2))
            return "EFGABCD"[step % 7], 4 + (step + 2) // 7

        def read_pitch(n, h, why=""):
            """오선 위치에서 음높이를 다시 읽는다. 엔진이 같은 음이름·옥타브면 엔진 것을 둔다 (임시표 보존)."""
            letter, octave = position_pitch(h)
            if (n.step, n.octave) != (letter, octave):
                if os.environ.get("SINGTOGETHER_DEBUG_PITCH"):
                    print(f"    덮어씀[{why}] 오선{si} x={h.x:.0f}: {n.step}{n.octave} → {letter}{octave}")
                n.step, n.octave, n.alter = letter, octave, key_alter(letter, t.fifths)

        all_pitched = [n for m in measures for n in m.notes if not n.is_rest]
        if all_pitched and len(all_pitched) == len(heads):     # 오선 전체가 딱 맞으면 그대로 짝
            for n, h in zip(all_pitched, heads):
                n.x, n.y = h.x, h.y
                read_pitch(n, h, "오선수일치")
        else:
            # 음표의 기대 x = 그 마디 범위 안에서 시간 비율 (악보는 대체로 시간에 비례해 배치된다).
            # 오선 전체의 기대 x 열과 머리 열을 한 번에 순서대로 DP 짝짓기 → 머리를 두 음표가 나눠 갖지 않고 x가 단조롭다.
            # 짝이 된 음표는 머리 좌표를 쓰고(수가 비슷하거나 기대 자리와 가까우면 음높이도), 나머지는 기대 x.
            exp: dict[int, float] = {}
            for m in measures:
                if m.x0 is None or not m.length:
                    continue
                pos = 0
                for n in m.notes:
                    exp[id(n)] = m.x0 + (m.x1 - m.x0) * float((pos + n.duration / 2) / m.length)
                    pos += n.duration
            pitched = [n for n in all_pitched if id(n) in exp]
            for n in pitched:
                n.x = exp[id(n)]
            if pitched and heads:
                pairs = _align_x([exp[id(n)] for n in pitched], [h.x for h in heads], gap=1.5 * il)
                # 음높이 덮어쓰기는 짝이 확실할 때만. 마디 안의 머리 수와 음표 수가 같으면 그 마디의 짝은 믿을 만하고,
                # 아니면(머리를 놓쳤거나 가짜 머리) 기대 자리와 가까운 짝만 믿는다. 오선 전체 수가 비슷하다고 전부 덮어쓰면
                # 깨끗한 입력에서 엔진이 맞게 읽은 음을 어긋난 짝이 망친다 (렌더링 악보 100% → 88%).
                measure_of = {id(n): m for m in measures for n in m.notes}
                exact = {id(m): (m.x0 is not None and
                                 sum(1 for h in heads if m.x0 <= h.x < m.x1) == sum(1 for n in m.notes if not n.is_rest))
                         for m in measures}
                sure = [(a, b) for a, b in pairs
                        if exact[id(measure_of[id(pitched[a])])] and measure_of[id(pitched[a])].x0 <= heads[b].x < measure_of[id(pitched[a])].x1]
                # 확실한 짝에서 엔진과 오선 위치가 얼마나 일치하나 = 이 쪽에서 엔진의 음높이를 얼마나 믿을지.
                # 거의 다 맞으면(깨끗한 입력) 불확실한 짝은 엔진을 두고, 자주 틀리면(웹 캡처) 수가 비슷한 한 전부 덮어쓴다.
                agree = (sum(position_pitch(heads[b]) == (pitched[a].step, pitched[a].octave) for a, b in sure) / len(sure)) if sure else None
                trust_all = abs(len(heads) - len(pitched)) <= 3 and (agree is None or agree < 0.93)
                sure_ids = {(a, b) for a, b in sure}
                if os.environ.get("SINGTOGETHER_DEBUG_PITCH"):
                    print(f"  오선{si}: 음표 {len(pitched)} 머리 {len(heads)} 확실한 짝 {len(sure)} 일치율 {agree if agree is None else round(agree, 2)} trust_all {trust_all}")
                for a, b in pairs:
                    n, h = pitched[a], heads[b]
                    near = abs(exp[id(n)] - h.x) < 1.0 * il
                    n.x, n.y = h.x, h.y
                    if (a, b) in sure_ids or near or trust_all:
                        read_pitch(n, h, "확실" if (a, b) in sure_ids else ("근접" if near else "전체신뢰"))
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
    """코드 글자의 왼쪽 끝이 음표 머리 근처에 온다. 가장 가까운 음표에 달되, 한 음표에 코드 둘이 겹치면
    (앞 코드를 덮어써 잃지 않도록) 뒤 코드는 오른쪽의 비어 있는 다음 음표로 보낸다."""
    for si, boxes in enumerate(chords):
        notes = sorted((n for m in t.measures if m.system == si for n in m.notes if n.x is not None), key=lambda n: n.x)
        if not notes:
            continue
        for c in sorted(boxes, key=lambda b: b.x0):
            x = c.x0 + 0.3 * interline
            k = min(range(len(notes)), key=lambda i: abs(notes[i].x - x))
            while k < len(notes) and notes[k].chord:
                k += 1
            if k < len(notes):
                notes[k].chord = c.text
            else:                                  # 오른쪽에 자리가 없으면 왼쪽의 빈 음표
                free = [i for i in range(len(notes)) if not notes[i].chord]
                if free:
                    notes[free[-1]].chord = c.text
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


def _align_x(xs: list[float], hs: list[float], gap: float) -> list[tuple[int, int]]:
    """순서를 지키는 1:1 짝짓기 (음표 i → 머리 j). 비용 = |x 차이|, 어느 쪽이든 건너뛰면 gap."""
    I, J = len(xs), len(hs)
    INF = float("inf")
    cost = [[INF] * (J + 1) for _ in range(I + 1)]
    back = [[None] * (J + 1) for _ in range(I + 1)]
    cost[0][0] = 0.0
    for i in range(I + 1):
        for j in range(J + 1):
            cur = cost[i][j]
            if cur == INF:
                continue
            if i < I and j < J and cur + abs(xs[i] - hs[j]) < cost[i + 1][j + 1]:
                cost[i + 1][j + 1], back[i + 1][j + 1] = cur + abs(xs[i] - hs[j]), (i, j, True)
            if i < I and cur + gap < cost[i + 1][j]:
                cost[i + 1][j], back[i + 1][j] = cur + gap, (i, j, False)
            if j < J and cur + gap < cost[i][j + 1]:
                cost[i][j + 1], back[i][j + 1] = cur + gap, (i, j, False)
    pairs, i, j = [], I, J
    while (i, j) != (0, 0):
        pi, pj, matched = back[i][j]
        if matched:
            pairs.append((pi, pj))
        i, j = pi, pj
    return pairs[::-1]


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
