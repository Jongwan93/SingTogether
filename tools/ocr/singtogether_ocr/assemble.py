"""5단계 조립: 엔진 마디를 오선에 배정하고, 음표에 x좌표를 붙이고, 코드 기호를 가장 가까운 음표에 단다."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from . import debug
from .ingest import Page
from .layout import System
from fractions import Fraction

from .model import Measure, Note, Transcription, key_alter
from .notes import SystemSymbols
from .text import TextBox


def staff_pitch(staff, y: float) -> tuple[str, int]:
    """오선 위치(y) → (음이름, 옥타브). 첫 줄 = E4, 반 칸마다 한 음."""
    step = round((staff.bottom - y) / (staff.interline / 2))
    return "EFGABCD"[step % 7], 4 + (step + 2) // 7


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
            return staff_pitch(st, h.y)

        def read_pitch(n, h, why=""):
            """오선 위치에서 음높이를 다시 읽는다. 엔진이 같은 음이름·옥타브면 엔진 것을 둔다 (임시표 보존)."""
            letter, octave = position_pitch(h)
            if (n.step, n.octave) != (letter, octave):
                if os.environ.get("SINGTOGETHER_DEBUG_PITCH"):
                    print(f"    override[{why}] staff{si} x={h.x:.0f}: {n.step}{n.octave} → {letter}{octave}")
                n.reasons.append(f"pitch fixed from staff position (engine read {n.name})")
                n.engine_read = n.name
                n.step, n.octave, n.alter = letter, octave, key_alter(letter, t.fifths)

        all_pitched = [n for m in measures for n in m.notes if not n.is_rest]
        if all_pitched and len(all_pitched) == len(heads):     # 오선 전체가 딱 맞으면 그대로 짝
            for n, h in zip(all_pitched, heads):
                n.x, n.y, n.head, n.evidence, h.taken = h.x, h.y, True, h.evidence, True
                read_pitch(n, h, "count match")
            for m in measures:
                m.head_match = True
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
                    print(f"  staff{si}: notes {len(pitched)} heads {len(heads)} sure pairs {len(sure)} agreement {agree if agree is None else round(agree, 2)} trust_all {trust_all}")
                for m in measures:
                    m.head_match = exact[id(m)]
                for a, b in pairs:
                    n, h = pitched[a], heads[b]
                    near = abs(exp[id(n)] - h.x) < 1.0 * il
                    n.x, n.y, n.head, n.evidence, h.taken = h.x, h.y, True, h.evidence, True
                    if (a, b) in sure_ids or near or trust_all:
                        read_pitch(n, h, "sure" if (a, b) in sure_ids else ("near" if near else "trust all"))
                for n in pitched:
                    if not n.head:
                        n.reasons.append("notehead not found")
        for m in measures:                                   # 쉼표는 마디 안에서 시간 비율로
            if m.x0 is not None and m.length:
                pos = 0
                for n in m.notes:
                    if n.is_rest:
                        n.x = m.x0 + (m.x1 - m.x0) * float((pos + n.duration / 2) / m.length)
                    pos += n.duration
    for m in t.measures[idx:]:
        m.system = len(systems) - 1


def resegment(t: Transcription, systems: list[System], symbols: list[SystemSymbols]) -> int:
    """마디를 다시 나눈다 (1-c): 이미지의 마디줄과 박자 합으로 엔진의 마디 경계를 바로잡는다.

    오선마다 음표 열(place_notes가 x를 붙인 순서)을 구간으로 자르는 DP. 자를 수 있는 자리 = 검출된 마디줄 자리,
    엔진의 마디 경계, 그리고 아무 자리(벌점). 비용 = 구간마다 박자 합이 맞으면 0, 아니면 1 + |합 − 박자표| / 박자표;
    경계마다 − 근거 보너스 (마디줄 1.0, 엔진 경계 0.3, 둘 다 1.3, 아무 자리 +0.5). 깨끗한 입력(마디줄·길이가 다 맞음)은
    그대로 나오고, 엔진이 마디를 쪼개거나 붙였을 때(이 세상의 부요함보다 35/34) 마디줄 쪽으로 돌아온다. 바뀐 경계 수를 돌려준다."""
    if not systems or not t.measures:
        return 0
    expected = Fraction(t.beats * 4, t.beat_type)
    old = t.measures
    new_measures: list[Measure] = []
    changed = 0
    for si, (sys_, syms) in enumerate(zip(systems, symbols)):
        ms = [m for m in old if m.system == si]
        seq = [(m, n) for m in ms for n in m.notes]
        if not seq:
            new_measures += ms                                     # 음표 없는 마디(온쉼표)는 그대로
            continue
        n = len(seq)
        xs = [nn.x if nn.x is not None else 0.0 for _, nn in seq]
        il = sys_.staff.interline
        g = 0
        engine_gaps = set()
        for m in ms:
            if 0 < g < n:
                engine_gaps.add(g)
            g += len(m.notes)
        # 후보 경계: 자리 g (= 음표 g 앞) → (보너스, 마디줄 x). 마디줄의 자리는 머리와 짝지어진 음표의 x로만 정한다
        # (짝이 없는 음표·쉼표의 x는 엔진 마디를 등분한 어림값이라 믿을 수 없다). 짝 없는 음표들 사이가 애매하면
        # 그 범위 안의 엔진 경계를 쓰고, 없으면 어림 x로 센다.
        bonus: dict[int, float] = {g: 0.3 for g in engine_gaps}
        bar_x: dict[int, float] = {}
        matched = [k for k, (_, nn) in enumerate(seq) if nn.head and nn.x is not None]
        for b in syms.barlines:
            lo = max((k for k in matched if xs[k] < b), default=-1) + 1       # 이 자리부터
            hi = min((k for k in matched if xs[k] > b), default=n)            # 이 자리까지 경계가 올 수 있다
            inside = [g for g in engine_gaps if lo <= g <= hi]
            g = inside[0] if inside else min(max(lo, sum(1 for x in xs if x < b)), hi)
            if 0 < g < n:
                bonus[g] = bonus.get(g, 0.0) + 1.0
                bar_x[g] = b
        durs = [nn.duration for _, nn in seq]
        prefix = [Fraction(0)]
        for d in durs:
            prefix.append(prefix[-1] + d)
        first_of_piece = bool(ms) and ms[0] is old[0]
        INF = float("inf")
        best = [INF] * (n + 1)
        back = [0] * (n + 1)
        best[0] = 0.0
        # 구간 비용: 박자 합이 딱 맞으면 0, 아니면 1 + 어긋난 비율 (맞는 합은 마디줄 하나보다 무겁다: 깨끗한 입력에서
        # 마디줄 자리를 한 음표 잘못 잡아도 엔진의 맞는 경계가 이긴다). 오선 첫·끝 구간은 짧아도 봐준다 (못갖춘마디).
        for j in range(1, n + 1):
            for i in range(j):
                if best[i] == INF:
                    continue
                length = prefix[j] - prefix[i]
                off = float(abs(length - expected) / expected)
                if length == expected:
                    seg = 0.0
                elif length < expected and (i == 0 or j == n):
                    seg = 0.0 if (i == 0 and first_of_piece) else off
                else:
                    seg = 1.0 + off
                cost = best[i] + seg + (0.0 if i == 0 else -bonus.get(i, -0.5))
                if cost < best[j] - 1e-9:
                    best[j], back[j] = cost, i
        cuts = []
        j = n
        while j > 0:
            cuts.append(j)
            j = back[j]
        cuts = sorted(set(cuts))                                   # 구간 끝 자리들 (마지막은 n)
        changed += len(set(cuts[:-1]) ^ engine_gaps)
        left = float(sys_.staff.left) + (5 if si == 0 else 3.5) * il
        start = 0
        for end in cuts:
            m = Measure(0)
            m.system = si
            m.notes = [nn for _, nn in seq[start:end]]
            first_m, last_m = seq[start][0], seq[end - 1][0]
            if first_m.notes and first_m.notes[0] is m.notes[0]:
                m.repeat_start, m.ending = first_m.repeat_start, first_m.ending
            if last_m.notes and last_m.notes[-1] is m.notes[-1]:
                m.repeat_end = last_m.repeat_end
            m.x0 = bar_x.get(start, left if start == 0 else None)
            if m.x0 is None:
                m.x0 = (xs[start - 1] + xs[start]) / 2
            m.x1 = bar_x.get(end, float(sys_.staff.right) if end == n else None)
            if m.x1 is None:
                m.x1 = (xs[end - 1] + xs[end]) / 2
            pitched = sum(1 for nn in m.notes if not nn.is_rest)
            heads = sum(1 for h in syms.noteheads if m.x0 <= h.x < m.x1)
            m.head_match = heads == pitched
            new_measures.append(m)
            start = end
    new_measures += [m for m in old if m.system is None or m.system >= len(systems)]
    for k, m in enumerate(new_measures, 1):
        m.number = k
    t.measures = new_measures
    return changed


_ALT = (Fraction(2), Fraction(1, 2), Fraction(3, 2), Fraction(2, 3))   # 길이 후보: ×2, ÷2, 점 붙이기, 점 떼기
FIX_MAX_COST = 0.7                                                     # 이보다 비싸면(근거 없는 고침) 안 고친다


def fill_missing(t: Transcription, systems: list[System], symbols: list[SystemSymbols]) -> int:
    """박자 합이 모자라는 마디를 엔진이 건너뛴 머리로 채운다 (1-b, 빠진 음표).

    작은 웹 이미지에서 엔진은 음표를 통째로 빼먹는데(주님 한 분만으로: 101개 중 46개만) 머리 검출은 대부분 찾는다.
    마디마다: ① 머리 없는 음표를 어림 자리 가까이(1.5il)의 짝 없는 머리와 다시 짝짓는다(정렬 DP가 놓친 짝).
    ② 그래도 짝 없는 머리가 남고 마디가 짧으면, 그 머리들의 이미지 길이 단서 합이 모자란 만큼과 똑같을 때만 그 자리에
    음표를 넣는다 — 음높이는 오선 위치. 넣은 음표는 "inserted" 신호로 확인 목록에 오른다. 넣은 수를 돌려준다.
    (머리 하나를 모자란 길이로 멋대로 넣거나 빈 자리에 쉼표를 넣는 것도 해 봤지만 가짜 머리가 음표가 되어 더 나빠졌다.)"""
    if not systems:
        return 0
    expected = Fraction(t.beats * 4, t.beat_type)
    added = 0
    by_system: dict[int | None, list[Measure]] = {}
    for m in t.measures:
        by_system.setdefault(m.system, []).append(m)
    for i, m in enumerate(t.measures):
        if m.system is None or m.system >= len(systems) or m.x0 is None or m.x1 is None:
            continue
        sys_, syms = systems[m.system], symbols[m.system]
        st, il = sys_.staff, sys_.staff.interline
        group = by_system[m.system]
        at_edge = i == 0 or i == len(t.measures) - 1 or m is group[0] or m is group[-1]
        free = sorted((h for h in syms.noteheads if not h.taken and m.x0 <= h.x < m.x1), key=lambda h: h.x)
        # ① 머리 없는 음표 ↔ 짝 없는 머리: 어림 x에서 1.5il 안에 있는 머리만 (먼 머리를 억지로 붙이면 코드 자리까지 밀린다)
        for n in (n for n in m.notes if not n.is_rest and not n.head and n.x is not None):
            near = [h for h in free if not h.taken and abs(h.x - n.x) < 1.5 * il]
            if not near:
                continue
            h = min(near, key=lambda h: abs(h.x - n.x))
            n.x, n.y, n.head, n.evidence, h.taken = h.x, h.y, True, h.evidence, True
            letter, octave = staff_pitch(st, h.y)
            if (n.step, n.octave) != (letter, octave):
                n.reasons.append(f"pitch fixed from staff position (engine read {n.name})")
                n.engine_read = n.name
                n.step, n.octave, n.alter = letter, octave, key_alter(letter, t.fifths)
        free = [h for h in free if not h.taken]
        missing = expected - m.length
        if missing <= 0 or not free or at_edge:
            continue
        # ② 남은 머리 전부의 이미지 길이 단서 합이 모자란 만큼과 똑같을 때만 넣는다 (머리 하나를 멋대로 늘이면 가짜 머리가 음표가 된다)
        ev_durs = [h.evidence.quarters()[0] if h.evidence else None for h in free]
        if any(d is None for d in ev_durs) or sum(ev_durs, Fraction(0)) != missing:
            continue
        inserted: list[Note] = []
        for h, d in zip(free, ev_durs):
            letter, octave = staff_pitch(st, h.y)
            if not (52 <= 12 * (octave + 1) + "C D EF G A B".index(letter) <= 84):     # E3–C6 밖이면 머리가 아니다
                inserted = []
                break
            n = Note(letter, key_alter(letter, t.fifths), octave, d, 1 if d.numerator == 3 else 0, x=h.x, y=h.y)
            n.head, n.evidence = True, h.evidence
            n.reasons.append("added from a notehead the engine skipped (beat count was short)")
            n.signals.append("inserted")
            inserted.append(n)
        if inserted:
            for h in free:
                h.taken = True
            m.notes = sorted(m.notes + inserted, key=lambda n: n.x if n.x is not None else 0.0)
            added += len(inserted)
    return added


def fix_rhythm(t: Transcription, ai: dict[int, "Note"], ai_trusted: bool) -> int:
    """박자 합이 박자표와 다른 마디에서 음표 길이를 바로잡는다 (1-b).

    한 마디 안에서 음표 한두 개의 길이를 ×2·÷2·점 붙이기·점 떼기로 바꿔 합을 맞추는 조합 중 가장 근거 있는 것을 고른다.
    근거 = AI가 그 길이로 읽었다(−0.6, 믿을 만할 때만), 이미지 단서(빈 머리·빔 수·점)가 그 길이를 가리킨다(−0.25),
    지금 길이를 단서가 지지한다(+0.15). 근거 없는 고침(비용 > FIX_MAX_COST)은 하지 않는다 — 아무 음표나 바꾸면 맞을 확률이
    반이라 득이 없다. 이미지 단서는 깨끗한 입력에서도 60–70%만 맞아(꼬리 수 세기가 약하다) 혼자서는 못 고치고 AI 읽기를 거든다. 오선 첫·끝의 짧은 마디(못갖춘마디)는 건드리지 않는다. 고친 음표 수를 돌려준다."""
    expected = Fraction(t.beats * 4, t.beat_type)
    fixed = 0
    by_system: dict[int | None, list] = {}
    for m in t.measures:
        by_system.setdefault(m.system, []).append(m)
    for i, m in enumerate(t.measures):
        length = m.length
        if not m.notes or length == expected:
            continue
        group = by_system.get(m.system, [])
        at_edge = i == 0 or i == len(t.measures) - 1 or (group and (m is group[0] or m is group[-1]))
        if length < expected and at_edge:
            continue
        options: list[list[tuple[int, Fraction, float, str]]] = []     # 음표마다 (번호, 새 길이, 비용, 근거)
        for k, n in enumerate(m.notes):
            opts = []
            o = ai.get(id(n)) if ai_trusted else None
            ev = n.evidence
            supported_now = ev is not None and n.duration in ev.quarters()
            for f in _ALT:
                d = n.duration * f
                if not (Fraction(1, 4) <= d <= Fraction(4)) or d.denominator > 8:
                    continue
                cost, why = 1.0, []
                if o is not None and not o.is_rest and o.duration == d:
                    cost -= 0.6; why.append("AI read it so")
                if ev is not None and d in ev.quarters():
                    cost -= 0.25; why.append("image shows it")       # 단서만으로는 못 고친다 (깨끗한 입력에서도 60–70%만 맞는다)
                if supported_now:
                    cost += 0.15
                if cost <= FIX_MAX_COST:
                    opts.append((k, d, cost, ", ".join(why)))
            options.append(opts)
        best = None
        flat = [o for opts in options for o in opts]
        for k, d, c, why in flat:                                   # 하나 고치기
            if length - m.notes[k].duration + d == expected and (best is None or c < best[0]):
                best = (c, [(k, d, why)])
        for a_i, (k1, d1, c1, w1) in enumerate(flat):              # 둘 고치기 (서로 다른 음표)
            for k2, d2, c2, w2 in flat[a_i + 1:]:
                if k1 != k2 and length - m.notes[k1].duration - m.notes[k2].duration + d1 + d2 == expected \
                        and (best is None or c1 + c2 < best[0]):
                    best = (c1 + c2, [(k1, d1, w1), (k2, d2, w2)])
        if best is None:
            continue
        for k, d, why in best[1]:
            n = m.notes[k]
            n.reasons.append(f"length fixed from beat count: {n.duration} → {d} ({why})")
            n.signals.append("rhythm_fixed")
            n.duration = d
            n.dots = 1 if d.numerator == 3 else 0
            fixed += 1
    return fixed


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
                notes[k].chord, notes[k].chord_conf, notes[k].chord_src = c.text, c.conf, c.src
            else:                                  # 오른쪽에 자리가 없으면 왼쪽의 빈 음표
                free = [i for i in range(len(notes)) if not notes[i].chord]
                if free:
                    notes[free[-1]].chord, notes[free[-1]].chord_conf, notes[free[-1]].chord_src = c.text, c.conf, c.src
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
