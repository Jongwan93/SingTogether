"""코드 기호로 조성 추론. OMR 엔진이 조표를 자주 틀리는데(♭4개를 ♭3개로 등), 찬양 악보는 코드가 거의 다 다이어토닉이라 코드가 더 믿을 만하다."""
from __future__ import annotations

import re
from collections import Counter
from fractions import Fraction

from .model import STEP_PC

_SCALE = (0, 2, 4, 5, 7, 9, 11)


def _root_pc(chord: str) -> int | None:
    m = re.match(r"([A-G])([#b]?)", chord)
    if not m:
        return None
    return (STEP_PC[m.group(1)] + {"#": 1, "b": -1}.get(m.group(2), 0)) % 12


def infer_time(t) -> None:
    """마디 길이의 최빈값으로 박자표를 바로잡는다 (엔진이 2/2·3/4로 잘못 읽는 경우). 60% 이상 마디가 같은 길이일 때만."""
    if (t.beats, t.beat_type) == (2, 2):          # 찬양 악보에 2/2는 사실상 없다. 엔진이 4/4를 2/2로 읽은 것 (리듬은 같다)
        t.beats, t.beat_type = 4, 4
    lengths = Counter(m.length for m in t.measures if m.notes)
    if not lengths:
        return
    length, count = lengths.most_common(1)[0]
    if count < 0.6 * sum(lengths.values()):
        return
    if length == Fraction(t.beats * 4, t.beat_type):   # 엔진이 읽은 박자표가 마디 길이와 맞으면 그대로 (6/8을 3/4로 바꾸지 않게)
        return
    for beats, beat_type in ((4, 4), (3, 4), (2, 4), (6, 8), (2, 2)):
        if length == beats * 4 / beat_type:
            if (beats, beat_type) != (t.beats, t.beat_type) and not (length == 4 and (t.beats, t.beat_type) == (4, 4)):
                t.beats, t.beat_type = beats, beat_type
            return


_DEGREE_QUALITY = {0: "major", 2: "minor", 4: "minor", 5: "major", 7: "major", 9: "minor", 11: "dim"}


def _quality(chord: str) -> str | None:
    """장·단 3화음 성질. sus·슬래시·텐션만 있으면 장화음으로 본다. 모르면 None."""
    body = re.sub(r"^[A-G][#b]?", "", chord).split("/")[0]
    if re.match(r"m(?!aj)", body) or body.startswith(("min", "-")):
        return "minor"
    if body.startswith(("dim", "°", "ø")):
        return "dim"
    if re.fullmatch(r"(maj7|maj9|maj|M7|7|9|2|6|add\d+|sus\d?|7sus4|)", body):
        return "major"
    return None


def infer_key(chords: list[str], prefer: int | None = None) -> tuple[int, str, float] | None:
    """(fifths, mode, 확신도 0–1). 코드가 3개 미만이면 None.

    장조 후보(-6…6)마다 근음이 음계 안에 있으면 +1, 코드 성질이 그 도수의 다이어토닉 3화음과 맞으면 +1, 틀리면 -1.
    (C장조와 A장조는 근음만으로는 구분이 안 되지만, E가 장화음이고 B가 단화음이면 A장조다.)
    동점이면 엔진이 읽은 조표(prefer), 그다음 최빈 코드·첫 코드가 으뜸화음인 쪽. 첫 코드가 단화음이고 vi도이면 단조.
    (D A Bm만 읽힌 D장조 악보: D와 A가 동점인데 A를 골라 G가 G♯이 된 적이 있다 — 조표가 D라면 D.)
    """
    roots = [(c, _root_pc(c), _quality(c)) for c in chords]
    roots = [(c, pc, q) for c, pc, q in roots if pc is not None]
    if len(roots) < 3:
        return None
    first = roots[0]
    common = Counter(pc for _, pc, _ in roots).most_common(1)[0][0]
    best = None
    for fifths in range(-6, 7):
        tonic = (7 * fifths) % 12
        score, hits = 0, 0
        for _, pc, q in roots:
            degree = (pc - tonic) % 12
            if degree in _DEGREE_QUALITY:
                hits += 1
                score += 1
                if q is not None:
                    score += 1 if q == _DEGREE_QUALITY[degree] else -1
        key_score = (score, fifths == prefer, common == tonic, first[1] == tonic)
        if best is None or key_score > best[0]:
            best = (key_score, fifths, tonic, hits)
    _, fifths, tonic, hits = best
    mode = "major"
    if re.match(r"[A-G][#b]?m(?!aj)", first[0]) and first[1] == (tonic + 9) % 12:
        mode = "minor"
    return fifths, mode, hits / len(roots)


def scale_fit(t, fifths: int) -> float:
    """엔진이 읽은 음 가운데 그 조의 온음계에 드는 비율."""
    tonic = (fifths * 7) % 12
    scale = {(tonic + d) % 12 for d in _SCALE}
    pcs = [n.midi % 12 for n in t.notes if not n.is_rest and n.midi is not None]
    return sum(pc in scale for pc in pcs) / len(pcs) if pcs else 0.0


def plausible_change(t, fifths: int) -> bool:
    """코드로 추론한 조를 엔진 조표 대신 쓸지. 엔진은 조표를 한두 칸 틀리게 읽는 일이 흔하지만(♭4→♭3),
    멀리 떨어진 조는 코드 OCR이 깨진 쪽일 가능성이 크다. 그때는 음들이 어느 조에 더 잘 맞는지로 가른다
    (렌더링 악보에서 코드의 ♭를 놓쳐 A♭장조가 E장조로 바뀌며 음높이 14%가 된 사례)."""
    if abs(fifths - t.fifths) <= 2:
        return True
    return scale_fit(t, fifths) >= scale_fit(t, t.fifths)
