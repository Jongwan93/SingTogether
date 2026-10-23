"""엔진 공통 출력 형식. 모든 엔진은 Page를 받아 Transcription을 돌려준다.

길이 단위: 4분음표 = 1 (Fraction). 좌표: 페이지 픽셀 (Page.gray 기준).
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET

STEP_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
MAJOR_BY_FIFTHS = ["Gb", "Db", "Ab", "Eb", "Bb", "F", "C", "G", "D", "A", "E", "B", "F#"]
MINOR_BY_FIFTHS = ["Eb", "Bb", "F", "C", "G", "D", "A", "E", "B", "F#", "C#", "G#", "D#"]


@dataclass
class Note:
    step: str | None                     # 'C'…'B', 쉼표면 None
    alter: int = 0
    octave: int = 4
    duration: Fraction = Fraction(1)
    dots: int = 0                        # 표기용. duration에는 이미 반영돼 있다
    tie_start: bool = False
    tie_stop: bool = False
    x: float | None = None               # 음표 머리 중심 (페이지 픽셀)
    y: float | None = None
    conf: float | None = None            # 0–1, 모르면 None
    chord: str | None = None             # 이 음표 위의 코드 기호
    lyrics: list[str] = field(default_factory=list)   # 절별 음절. '' = 새 음절 없음

    @property
    def is_rest(self) -> bool:
        return self.step is None

    @property
    def midi(self) -> int | None:
        return None if self.step is None else 12 * (self.octave + 1) + STEP_PC[self.step] + self.alter

    @property
    def name(self) -> str:
        if self.step is None:
            return "r"
        return self.step + {1: "#", -1: "b", 2: "##", -2: "bb"}.get(self.alter, "") + str(self.octave)


@dataclass
class Measure:
    number: int
    notes: list[Note] = field(default_factory=list)
    repeat_start: bool = False
    repeat_end: bool = False
    ending: int | None = None            # 1, 2 … 괄호 시작 마디
    system: int | None = None            # 몇 번째 오선에 있는지
    x0: float | None = None
    x1: float | None = None

    @property
    def length(self) -> Fraction:
        return sum((n.duration for n in self.notes), Fraction(0))


@dataclass
class Transcription:
    engine: str
    measures: list[Measure] = field(default_factory=list)
    fifths: int = 0
    mode: str = "major"
    beats: int = 4
    beat_type: int = 4
    title: str = ""
    composer: str = ""
    has_chords: bool = False             # 엔진이 코드 기호까지 읽었는가
    has_lyrics: bool = False
    seconds: float = 0.0

    @property
    def notes(self) -> list[Note]:
        return [n for m in self.measures for n in m.notes]

    @property
    def key_name(self) -> str:
        i = max(-6, min(6, self.fifths)) + 6
        return (MINOR_BY_FIFTHS[i] + " minor") if self.mode == "minor" else (MAJOR_BY_FIFTHS[i] + " major")


# MARK: MusicXML 읽기 (homr, oemer, Audiveris 출력)

_KIND_TEXT = {
    "major": "", "minor": "m", "dominant": "7", "major-seventh": "maj7", "minor-seventh": "m7",
    "suspended-fourth": "sus4", "suspended-second": "sus2", "diminished": "dim", "augmented": "aug",
    "major-sixth": "6", "minor-sixth": "m6", "dominant-ninth": "9", "major-ninth": "maj9", "minor-ninth": "m9",
    "half-diminished": "m7b5", "diminished-seventh": "dim7", "power": "5", "none": "",
}


def read_musicxml_bytes(path: Path) -> bytes:
    if path.suffix.lower() != ".mxl":
        return path.read_bytes()
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith((".xml", ".musicxml")) and not n.startswith("META-INF")]
        try:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            rootfile = container.find(".//rootfile")
            if rootfile is not None and rootfile.get("full-path") in z.namelist():
                names = [rootfile.get("full-path")]
        except KeyError:
            pass
        if not names:
            raise ValueError(f"MXL 안에 악보가 없어요: {path}")
        return z.read(names[0])


_SHARPS = ["F", "C", "G", "D", "A", "E", "B"]


def _key_alter(step: str, fifths: int) -> int:
    if fifths > 0 and step in _SHARPS[:fifths]:
        return 1
    if fifths < 0 and step in _SHARPS[::-1][:-fifths]:
        return -1
    return 0


def key_alter(step: str, fifths: int) -> int:
    return _key_alter(step, fifths)


def rekey(t: Transcription, fifths: int, mode: str) -> None:
    """조표를 바꾼다. 조표에서 온 임시표(alter가 옛 조표와 같은 음)는 새 조표에 맞게 옮기고, 명시적 임시표는 둔다."""
    for n in t.notes:
        if not n.is_rest and n.alter == _key_alter(n.step, t.fifths):
            n.alter = _key_alter(n.step, fifths)
    t.fifths, t.mode = fifths, mode


def from_musicxml(src: Path | bytes, engine: str, key_implied_alters: bool = False) -> Transcription:
    """score-partwise → Transcription. 성부가 여럿이면 가사가 있는 성부, 없으면 음표가 가장 많은 성부를 멜로디로 본다.

    key_implied_alters: homr처럼 조표에 해당하는 음에 <alter>를 안 쓰는 출력용. alter도 <accidental>도 없으면
    같은 마디에서 앞서 나온 임시표, 없으면 조표를 적용한다.
    """
    data = read_musicxml_bytes(src) if isinstance(src, Path) else src
    root = ET.fromstring(data)
    if root.tag != "score-partwise":
        raise ValueError(f"score-partwise만 지원해요: {root.tag}")
    parts = root.findall("part")
    if not parts:
        return Transcription(engine)
    part = max(parts, key=lambda p: (len(p.findall(".//lyric")), len(p.findall(".//note/pitch"))))

    t = Transcription(engine)
    t.title = (root.findtext("work/work-title") or root.findtext("movement-title") or "").strip()
    t.composer = (root.findtext("identification/creator[@type='composer']") or "").strip()
    divisions = 1
    pending_chord: str | None = None
    for i, m in enumerate(part.findall("measure"), 1):
        num = re.sub(r"\D", "", m.get("number") or "")
        meas = Measure(int(num) if num else i)
        measure_alters: dict[tuple[str, int], int] = {}      # 이 마디에서 임시표가 붙은 (음이름, 옥타브) → alter
        for el in m:
            if el.tag == "attributes":
                if (d := el.findtext("divisions")):
                    divisions = int(float(d))
                if (f := el.findtext("key/fifths")):
                    t.fifths = int(f)
                    t.mode = (el.findtext("key/mode") or "major").strip()
                if (b := el.findtext("time/beats")):
                    t.beats, t.beat_type = int(b), int(el.findtext("time/beat-type") or 4)
            elif el.tag == "harmony":
                pending_chord = _harmony_text(el)
                t.has_chords = True
            elif el.tag == "barline":
                rep = el.find("repeat")
                if rep is not None:
                    if rep.get("direction") == "forward":
                        meas.repeat_start = True
                    elif rep.get("direction") == "backward":
                        meas.repeat_end = True
                end = el.find("ending")
                if end is not None and end.get("type") == "start":
                    digits = re.sub(r"\D", "", end.get("number") or "")
                    meas.ending = int(digits[0]) if digits else 1
            elif el.tag == "note":
                if el.find("grace") is not None:
                    continue
                dur = Fraction(int(float(el.findtext("duration") or 0)), divisions)
                if el.find("chord") is not None:
                    # 같은 자리의 여러 음: 가장 높은 음을 멜로디로
                    pitch = _pitch(el)
                    last = meas.notes[-1] if meas.notes else None
                    if pitch and last and (last.is_rest or _midi(*pitch) > last.midi):
                        last.step, last.alter, last.octave = pitch
                    continue
                pitch = _pitch(el)
                if pitch and key_implied_alters:
                    step, alter, octave = pitch
                    if el.find("pitch/alter") is None and el.find("accidental") is None:
                        alter = measure_alters.get((step, octave), _key_alter(step, t.fifths))
                    else:
                        measure_alters[(step, octave)] = alter
                    pitch = (step, alter, octave)
                note = Note(*(pitch or (None, 0, 4)), duration=dur, dots=len(el.findall("dot")))
                for tie in el.findall("tie"):
                    if tie.get("type") == "start":
                        note.tie_start = True
                    elif tie.get("type") == "stop":
                        note.tie_stop = True
                for lyr in el.findall("lyric"):
                    n = re.sub(r"\D", "", lyr.get("number") or "1")
                    idx = max(0, (int(n) if n else 1) - 1)
                    text = (lyr.findtext("text") or "").strip()
                    while len(note.lyrics) <= idx:
                        note.lyrics.append("")
                    note.lyrics[idx] = text
                    if text:
                        t.has_lyrics = True
                if pending_chord and not note.is_rest:
                    note.chord, pending_chord = pending_chord, None
                elif pending_chord and note.is_rest:
                    note.chord, pending_chord = pending_chord, None
                meas.notes.append(note)
        t.measures.append(meas)
    return t


def _pitch(el: ET.Element) -> tuple[str, int, int] | None:
    p = el.find("pitch")
    if p is None:
        return None
    return (p.findtext("step") or "C").strip(), int(round(float(p.findtext("alter") or 0))), int(p.findtext("octave") or 4)


def _midi(step: str, alter: int, octave: int) -> int:
    return 12 * (octave + 1) + STEP_PC[step] + alter


def _harmony_text(el: ET.Element) -> str:
    acc = {1: "#", -1: "b"}
    root = (el.findtext("root/root-step") or "?") + acc.get(int(float(el.findtext("root/root-alter") or 0)), "")
    kind = el.find("kind")
    if kind is not None and kind.get("text"):
        suffix = kind.get("text")
    else:
        suffix = _KIND_TEXT.get((kind.text or "major").strip() if kind is not None else "major", "")
    for deg in el.findall("degree"):
        if (deg.findtext("degree-type") or "").strip() == "add":
            suffix += "add" + (deg.findtext("degree-value") or "").strip()
    bass = el.findtext("bass/bass-step")
    if bass:
        suffix += "/" + bass.strip() + acc.get(int(float(el.findtext("bass/bass-alter") or 0)), "")
    return root + suffix
