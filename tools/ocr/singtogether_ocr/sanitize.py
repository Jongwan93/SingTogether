"""코드 OCR 후보정. (1) 자리별 글자 혼동표로 문법에 맞는 후보를 만들고, (2) 후보가 여럿이면 조성의 흔한 코드 사전에 있는 쪽을 고른다.

문법에 이미 맞는 토큰은 건드리지 않는다. 측정 결과(2026-10) 조 사전으로 *유효한* 읽기를 바꾸는 건 조 추정이 틀릴 때
맞는 코드를 망쳤고(D2→D7), 혼동표로 무효 토큰을 살리는 것만 ±0~1개 수준이라 후자만 파이프라인에 연결돼 있다.
"""
from __future__ import annotations

import re
from itertools import product

from .model import STEP_PC
from .text import CHORD_RE, QUALITY_RE

# OCR 글자 → 실제 글자 후보 (자리별). 근음 자리의 숫자, 임시표 자리의 4/t/i, 슬래시 자리의 I/l/|, 텐션 숫자의 Z/O.
_ROOT = {"8": "B", "6": "G", "0": "D", "(": "C", "£": "E", "€": "E"}
_ACC = {"4": "#", "t": "#", "i": "#", "I": "#", "l": "#", "1": "#", "†": "#", "+": "#", "h": "b", "6": "b", "ь": "b"}
_SLASH = {"I": "/", "l": "/", "|": "/", "1": "/", "\\": "/", "7": "/"}
_QUAL = {"Z": "2", "z": "2", "O": "0", "o": "0", "S": "5", "l": "1", "I": "1"}   # "?"는 윗첨자 2가 흔해 짐작하지 않는다
_NAMES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_NAMES_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]


def _pc(letter: str, acc: str) -> int:
    return (STEP_PC[letter] + {"#": 1, "b": -1}.get(acc, 0)) % 12


def canon(chord: str) -> tuple[int, str, int | None] | None:
    """(근음 pc, 성질, 베이스 pc). 이명동음을 같게 본다. 코드가 아니면 None."""
    m = CHORD_RE.match(chord)
    if not m or not QUALITY_RE.match(m.group(3)):
        return None
    bass = _pc(m.group(4), m.group(5)) if m.group(4) else None
    return _pc(m.group(1), m.group(2)), m.group(3), bass


def plausible(chord: str) -> bool:
    """문법에 맞고, 성질이 0·1·3·4·8로 시작하지 않는다 ("F4m"의 4는 #을 잘못 읽은 것. 2·5·6·7·9는 실제로 쓰인다)."""
    m = CHORD_RE.match(chord)
    return bool(m) and bool(QUALITY_RE.match(m.group(3))) and not re.match(r"[01348]", m.group(3))


def candidates(token: str, max_subs: int = 2) -> list[str]:
    """혼동표를 최대 max_subs번 적용해 코드 문법에 맞게 되는 문자열들. 치환 횟수가 적은 것부터."""
    if plausible(token):
        return [token]
    seen: dict[str, int] = {}
    frontier = [token]
    for depth in range(1, max_subs + 1):
        nxt = []
        for s in frontier:
            for v in _variants(s):
                if v not in seen:
                    seen[v] = depth
                    nxt.append(v)
        frontier = nxt
        if any(plausible(v) for v in frontier):
            break
    return sorted((v for v in seen if plausible(v)), key=lambda v: (seen[v], len(v)))


def _variants(s: str) -> list[str]:
    out = []
    if s and s[0] in _ROOT:
        out.append(_ROOT[s[0]] + s[1:])
    if len(s) > 2 and s[1] in _ACC and re.match(r"[A-G]", s[0]):     # 뒤에 뭔가 더 있어야 한다 ("C4"만으로는 C#인지 Csus4인지 모른다)
        out.append(s[0] + _ACC[s[1]] + s[2:])
    # 베이스 음의 임시표 자리 (A/C4 → A/C#)
    bm = re.search(r"/([A-G])(.)$", s)
    if bm and bm.group(2) in _ACC:
        out.append(s[:bm.start(2)] + _ACC[bm.group(2)])
    # 슬래시 자리: 근음(+성질) 뒤, 베이스 글자 앞
    for m in re.finditer(r"(?<=[A-Za-z0-9#b])([Il|1\\7])(?=[A-G][#b]?$)", s):
        out.append(s[:m.start()] + "/" + s[m.end():])
    # 성질 자리의 글자 (근음·임시표 뒤부터 슬래시 앞까지)
    head = re.match(r"[A-G][#b]?", s)
    if head:
        body_end = s.find("/") if "/" in s else len(s)
        for i in range(head.end(), body_end):
            if s[i] in _QUAL:
                out.append(s[:i] + _QUAL[s[i]] + s[i + 1:])
    return out


def common_chords(fifths: int, mode: str = "major") -> set[tuple[int, str, int | None]]:
    """조성에서 흔한 코드의 canon 집합: 다이어토닉 3·7화음(+sus·2·add9·6), 부속 도미넌트, 차용(bVII·bVI·iv), 온음계 베이스의 슬래시."""
    tonic = (fifths * 7) % 12
    if mode == "minor":
        tonic = (tonic + 9) % 12
        degrees = {0: "minor", 2: "dim", 3: "major", 5: "minor", 7: "minor", 8: "major", 10: "major", 11: "major"}   # 화성 단조 V도
    else:
        degrees = {0: "major", 2: "minor", 4: "minor", 5: "major", 7: "major", 9: "minor", 11: "dim"}
    scale = sorted(degrees)
    out: set[tuple[int, str, int | None]] = set()
    ext = {"major": ["", "maj7", "M7", "7", "sus4", "sus2", "sus", "2", "add9", "add2", "6", "9", "maj9", "7sus4"],
           "minor": ["m", "m7", "m9", "m6", "m(add9)", "madd9", "m7(b5)"],
           "dim": ["dim", "dim7", "m7b5", "m7(b5)", "°"]}
    for d, q in degrees.items():
        for e in ext[q]:
            out.add(((tonic + d) % 12, e, None))
    for d in (2, 4, 9, 11, 0, 5):                           # 부속 도미넌트 II7 III7 VI7 VII7, 블루스 I7 IV7
        out.add(((tonic + d) % 12, "7", None)); out.add(((tonic + d) % 12, "", None))
    for d, q in ((10, "major"), (8, "major"), (5, "minor"), (3, "major"), (1, "major")):   # bVII bVI iv bIII bII
        for e in ext[q][:3]:
            out.add(((tonic + d) % 12, e, None))
    for root, q, _ in list(out):
        for d in scale:                                    # 슬래시: 온음계 음을 베이스로
            out.add((root, q, (tonic + d) % 12))
    return out


def fix(token: str, key: tuple[int, str] | None = None) -> str | None:
    """OCR 토큰 → 코드 문자열. 문법에 맞으면 그대로, 아니면 혼동표 후보 중 (사전에 있는 것 우선) 첫 것. 못 고치면 None."""
    cands = candidates(token)
    if not cands:
        return None
    if key and len(cands) > 1:
        common = common_chords(*key)
        for c in cands:
            if canon(c) in common:
                return c
    return cands[0]

