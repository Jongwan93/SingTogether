"""압축 텍스트 형식 ↔ Transcription.

정답(truth) 파일, Claude 엔진의 출력, 인식 결과 캐시가 모두 이 형식이다. 사람이 읽고 고치기 쉽다.

    title: 주님의 임재 앞에서
    composer: 박희정
    key: G major
    time: 4/4
    1: r/8 [G] G4/8 G4/8 G4/8 [D/F#] F#4/8 F#4/8 F#4/8 E4/8~ | 주 님 의 임 재 앞 에 _
    2: [C2] E4/8 D4/8 D4/2. | _ 서 _

음표 <음이름><#|b|n><옥타브>/<길이>[.][~]  (C4 = 가온 다, ~ = 다음 음과 붙임줄)
쉼표 r/<길이>[.]   코드 [G]   도돌이 {: :}   괄호 {1 {2
가사는 | 뒤에 절별로, 쉼표를 뺀 음표마다 한 음절. _ 는 새 음절 없음.
"""
from __future__ import annotations

import re
from fractions import Fraction

from .model import MAJOR_BY_FIFTHS, MINOR_BY_FIFTHS, STEP_PC, Measure, Note, Transcription

_NOTE = re.compile(r"^([A-G])([#bn]?)(\d)/(\d+)(\.?)(~?)$")
_REST = re.compile(r"^r/(\d+)(\.?)$")
_CHORD = re.compile(r"^\[(.+)\]$")
_LINE = re.compile(r"^\s*(\d+)\s*:\s*(.*)$")
_BASES = [Fraction(4), Fraction(2), Fraction(1), Fraction(1, 2), Fraction(1, 4), Fraction(1, 8)]


def from_text(text: str, engine: str = "text") -> Transcription:
    t = Transcription(engine)
    pending_chord = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        head, _, rest = line.partition(":")
        key = head.strip().lower()
        if key == "title":
            t.title = rest.strip(); continue
        if key == "composer":
            t.composer = rest.strip(); continue
        if key == "key":
            t.fifths, t.mode = _parse_key(rest); continue
        if key == "time":
            m = re.match(r"\s*(\d+)\s*/\s*(\d+)", rest)
            if m:
                t.beats, t.beat_type = int(m.group(1)), int(m.group(2))
            continue
        m = _LINE.match(line)
        if not m:
            continue
        parts = m.group(2).split("|")
        meas = Measure(int(m.group(1)))
        for tok in parts[0].split():
            if tok == "{:":
                meas.repeat_start = True
            elif tok == ":}":
                meas.repeat_end = True
            elif re.match(r"^\{\d$", tok):
                meas.ending = int(tok[1])
            elif (c := _CHORD.match(tok)):
                pending_chord = c.group(1)
                t.has_chords = True
            elif (n := _NOTE.match(tok)):
                step, acc, octv, d, dot, tie = n.groups()
                note = Note(step, {"#": 1, "b": -1}.get(acc, 0), int(octv), _duration(d, dot), int(bool(dot)), tie_start=bool(tie))
                if meas.notes and meas.notes[-1].tie_start:
                    note.tie_stop = True
                elif not meas.notes and t.measures and t.measures[-1].notes and t.measures[-1].notes[-1].tie_start:
                    note.tie_stop = True
                note.chord, pending_chord = pending_chord, None
                meas.notes.append(note)
            elif (r := _REST.match(tok)):
                note = Note(None, duration=_duration(*r.groups()), dots=int(bool(r.group(2))))
                note.chord, pending_chord = pending_chord, None
                meas.notes.append(note)
        for v, verse in enumerate(parts[1:]):
            sylls = verse.split()
            if not sylls:
                continue
            t.has_lyrics = True
            pitched = [n for n in meas.notes if not n.is_rest]
            for note, s in zip(pitched, sylls):
                while len(note.lyrics) <= v:
                    note.lyrics.append("")
                note.lyrics[v] = "" if s == "_" else s
        t.measures.append(meas)
    return t


def to_text(t: Transcription) -> str:
    lines = [f"title: {t.title}", f"composer: {t.composer}", f"key: {t.key_name}", f"time: {t.beats}/{t.beat_type}"]
    verses = max((len(n.lyrics) for n in t.notes), default=0)
    for m in t.measures:
        toks = []
        if m.repeat_start:
            toks.append("{:")
        if m.ending:
            toks.append(f"{{{m.ending}")
        for n in m.notes:
            if n.chord:
                toks.append(f"[{n.chord}]")
            toks.append(_note_token(n))
        if m.repeat_end:
            toks.append(":}")
        line = f"{m.number}: " + " ".join(toks)
        pitched = [n for n in m.notes if not n.is_rest]
        for v in range(verses):
            sylls = [(n.lyrics[v] if v < len(n.lyrics) and n.lyrics[v] else "_") for n in pitched]
            if any(s != "_" for s in sylls):
                line += " | " + " ".join(sylls)
        lines.append(line)
    return "\n".join(lines) + "\n"


def _note_token(n: Note) -> str:
    if n.is_rest:
        return "r/" + _dur_token(n.duration)
    acc = {1: "#", -1: "b"}.get(n.alter, "")
    return f"{n.step}{acc}{n.octave}/{_dur_token(n.duration)}{'~' if n.tie_start else ''}"


def _dur_token(d: Fraction) -> str:
    for base in _BASES:
        if d == base:
            return str(int(4 / base))
        if d == base * Fraction(3, 2):
            return f"{int(4 / base)}."
    base = min(_BASES, key=lambda b: abs(d - b))       # 셋잇단 등은 가장 가까운 길이로
    return str(int(4 / base))


def _duration(d: str, dot: str) -> Fraction:
    f = Fraction(4, int(d))
    return f * Fraction(3, 2) if dot else f


def _parse_key(s: str) -> tuple[int, str]:
    m = re.match(r"\s*([A-Ga-g])\s*([#b♯♭]?)\s*(major|minor|maj|min|m)?", s)
    if not m:
        return 0, "major"
    tonic = m.group(1).upper() + {"♯": "#", "♭": "b"}.get(m.group(2), m.group(2))
    mode = "minor" if (m.group(3) or "").lower() in ("minor", "min", "m") else "major"
    table = MINOR_BY_FIFTHS if mode == "minor" else MAJOR_BY_FIFTHS
    if tonic in table:
        return table.index(tonic) - 6, mode
    pc = (STEP_PC[tonic[0]] + {"#": 1, "b": -1}.get(tonic[1:], 0)) % 12       # 이명동음 (예: A# minor → Bb minor)
    for i, name in enumerate(table):
        if (STEP_PC[name[0]] + {"#": 1, "b": -1}.get(name[1:], 0)) % 12 == pc:
            return i - 6, mode
    return 0, mode
