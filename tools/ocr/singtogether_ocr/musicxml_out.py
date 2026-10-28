"""Transcription → MusicXML 4.0 (score-partwise). Swift 이조기와 Verovio가 읽을 수 있는 최소 구성."""
from __future__ import annotations

import re
from fractions import Fraction
from math import lcm
from xml.etree import ElementTree as ET

from .model import Transcription
from .text import CHORD_RE

_TYPES = {Fraction(4): "whole", Fraction(2): "half", Fraction(1): "quarter", Fraction(1, 2): "eighth",
          Fraction(1, 4): "16th", Fraction(1, 8): "32nd"}
_KINDS = {"": "major", "m": "minor", "min": "minor", "7": "dominant", "maj7": "major-seventh", "M7": "major-seventh",
          "m7": "minor-seventh", "sus4": "suspended-fourth", "sus": "suspended-fourth", "sus2": "suspended-second",
          "dim": "diminished", "dim7": "diminished-seventh", "aug": "augmented", "+": "augmented", "6": "major-sixth",
          "m6": "minor-sixth", "9": "dominant-ninth", "maj9": "major-ninth", "m9": "minor-ninth", "5": "power",
          "2": "major", "add2": "major", "add9": "major", "7sus4": "dominant"}


FLAG = 0.6                 # 이보다 낮은 신뢰도 = 확인 필요 (review.FLAG와 같은 값)
FLAG_COLOR = "#D00000"


def to_musicxml(t: Transcription) -> bytes:
    divisions = max(1, lcm(*(n.duration.denominator for n in t.notes)) if t.notes else 1)
    root = ET.Element("score-partwise", version="4.0")
    if t.title:
        ET.SubElement(ET.SubElement(root, "work"), "work-title").text = t.title
    if t.composer:
        ET.SubElement(ET.SubElement(root, "identification"), "creator", type="composer").text = t.composer
    sp = ET.SubElement(ET.SubElement(root, "part-list"), "score-part", id="P1")
    ET.SubElement(sp, "part-name").text = "Melody"
    part = ET.SubElement(root, "part", id="P1")

    open_ending: int | None = None
    for i, m in enumerate(t.measures):
        me = ET.SubElement(part, "measure", number=str(m.number))
        if i == 0:
            attrs = ET.SubElement(me, "attributes")
            ET.SubElement(attrs, "divisions").text = str(divisions)
            key = ET.SubElement(attrs, "key")
            ET.SubElement(key, "fifths").text = str(t.fifths)
            ET.SubElement(key, "mode").text = t.mode
            time = ET.SubElement(attrs, "time")
            ET.SubElement(time, "beats").text = str(t.beats)
            ET.SubElement(time, "beat-type").text = str(t.beat_type)
            clef = ET.SubElement(attrs, "clef")
            ET.SubElement(clef, "sign").text = "G"
            ET.SubElement(clef, "line").text = "2"
        if m.ending and open_ending and part[-2:-1]:          # 앞 괄호가 안 닫혔으면 앞 마디에서 끝낸다
            _ending(part[-2], "right", open_ending, "discontinue")
            open_ending = None
        if m.repeat_start or m.ending:
            bl = ET.SubElement(me, "barline", location="left")
            if m.repeat_start:
                ET.SubElement(bl, "bar-style").text = "heavy-light"
                ET.SubElement(bl, "repeat", direction="forward")
            if m.ending:
                ET.SubElement(bl, "ending", number=str(m.ending), type="start").text = f"{m.ending}."
                open_ending = m.ending
        for n in m.notes:
            if n.chord:
                h = _harmony(me, n.chord)
                if n.chord_conf is not None and n.chord_conf < FLAG:
                    h.set("color", FLAG_COLOR)
            ne = ET.SubElement(me, "note")
            if n.conf is not None and n.conf < FLAG:          # 확인이 필요한 음표는 빨갛게 (Verovio가 그대로 그린다)
                ne.set("color", FLAG_COLOR)
            if n.is_rest:
                ET.SubElement(ne, "rest")
            else:
                p = ET.SubElement(ne, "pitch")
                ET.SubElement(p, "step").text = n.step
                if n.alter:
                    ET.SubElement(p, "alter").text = str(n.alter)
                ET.SubElement(p, "octave").text = str(n.octave)
            ET.SubElement(ne, "duration").text = str(int(n.duration * divisions))
            if n.tie_stop:
                ET.SubElement(ne, "tie", type="stop")
            if n.tie_start:
                ET.SubElement(ne, "tie", type="start")
            typ, dots = _type(n.duration)
            ET.SubElement(ne, "type").text = typ
            for _ in range(dots):
                ET.SubElement(ne, "dot")
            if not n.is_rest and n.alter and _needs_accidental(n.step, n.alter, t.fifths):
                ET.SubElement(ne, "accidental").text = {1: "sharp", -1: "flat", 2: "double-sharp", -2: "flat-flat"}[n.alter]
            if n.tie_start or n.tie_stop:
                nt = ET.SubElement(ne, "notations")
                if n.tie_stop:
                    ET.SubElement(nt, "tied", type="stop")
                if n.tie_start:
                    ET.SubElement(nt, "tied", type="start")
            for v, text in enumerate(n.lyrics, 1):
                if text:
                    ly = ET.SubElement(ne, "lyric", number=str(v))
                    if v == 1 and n.lyric_conf is not None and n.lyric_conf < FLAG:
                        ly.set("color", FLAG_COLOR)
                    ET.SubElement(ly, "syllabic").text = "single"
                    ET.SubElement(ly, "text").text = text
        is_last = i == len(t.measures) - 1
        if m.repeat_end or is_last:
            bl = ET.SubElement(me, "barline", location="right")
            ET.SubElement(bl, "bar-style").text = "light-heavy"
            if open_ending:
                ET.SubElement(bl, "ending", number=str(open_ending), type="stop" if m.repeat_end else "discontinue")
                open_ending = None
            if m.repeat_end:
                ET.SubElement(bl, "repeat", direction="backward")
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


def _ending(me: ET.Element, location: str, number: int, type_: str) -> None:
    bl = ET.SubElement(me, "barline", location=location)
    ET.SubElement(bl, "ending", number=str(number), type=type_)


def _harmony(me: ET.Element, chord: str) -> ET.Element:
    m = CHORD_RE.match(chord)
    h = ET.SubElement(me, "harmony")
    root = ET.SubElement(h, "root")
    if not m:
        ET.SubElement(root, "root-step").text = "C"
        ET.SubElement(h, "kind", text=chord).text = "other"
        return h
    step, acc, quality, bass, bacc = m.groups()
    ET.SubElement(root, "root-step").text = step
    if acc:
        ET.SubElement(root, "root-alter").text = "1" if acc in "#♯" else "-1"
    # 인쇄된 표기(text)를 그대로 보여 준다. C2 같은 건 <degree>로 풀면 Verovio가 ♭ 글리프를 깨뜨려 그리므로 넣지 않는다.
    kind = ET.SubElement(h, "kind", text=quality)
    kind.text = _KINDS.get(quality, _KINDS.get(re.sub(r"\(.*\)", "", quality), "other"))
    if bass:
        b = ET.SubElement(h, "bass")
        ET.SubElement(b, "bass-step").text = bass
        if bacc:
            ET.SubElement(b, "bass-alter").text = "1" if bacc in "#♯" else "-1"
    return h


def _type(d: Fraction) -> tuple[str, int]:
    if d in _TYPES:
        return _TYPES[d], 0
    if d * Fraction(2, 3) in _TYPES:
        return _TYPES[d * Fraction(2, 3)], 1
    return _TYPES[min(_TYPES, key=lambda b: abs(d - b))], 0


def _needs_accidental(step: str, alter: int, fifths: int) -> bool:
    """조표에 이미 있는 임시표면 안 그린다 (같은 마디 안 반복은 Verovio가 처리)."""
    sharps = ["F", "C", "G", "D", "A", "E", "B"][:max(0, fifths)]
    flats = ["B", "E", "A", "D", "G", "C", "F"][:max(0, -fifths)]
    return not ((alter == 1 and step in sharps) or (alter == -1 and step in flats))
