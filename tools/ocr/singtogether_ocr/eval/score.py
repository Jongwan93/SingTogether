"""정확도 계산.

마디 경계를 틀려도 순서가 맞으면 점수를 주도록, 전체를 한 줄로 펴서 가장 긴 공통 부분열(difflib)로 맞춘다.
  notes  : (음높이, 길이) 둘 다 맞은 음표·쉼표 비율
  pitch  : 음높이만 맞은 비율 (쉼표 제외)
  chords : 코드 기호가 순서대로 맞은 비율
  lyrics : 1절 음절이 순서대로 맞은 비율
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from ..model import Transcription
from ..text import normalize_chord


@dataclass
class Score:
    notes: tuple[int, int]
    pitch: tuple[int, int]
    chords: tuple[int, int]
    lyrics: tuple[int, int]
    measures: tuple[int, int]        # (엔진, 정답)
    key_ok: bool
    time_ok: bool
    seconds: float
    flags: tuple[int, int, int] | None = None     # (표시했고 실제 틀린 수, 표시한 수, 실제 틀린 수) — 검토 표시의 정밀도·재현율

    @property
    def review(self) -> str:
        if not self.flags:
            return "–"
        tp, flagged, wrong = self.flags
        p = f"{100 * tp / flagged:.0f}%" if flagged else "–"
        r = f"{100 * tp / wrong:.0f}%" if wrong else "–"
        return f"P {p} R {r} ({flagged} flagged/{wrong} wrong)"

    def row(self) -> dict:
        def pct(p):
            return f"{100 * p[0] / p[1]:.0f}% ({p[0]}/{p[1]})" if p[1] else "–"
        return {"notes": pct(self.notes), "pitch": pct(self.pitch), "chords": pct(self.chords), "lyrics": pct(self.lyrics),
                "measures": f"{self.measures[0]}/{self.measures[1]}", "key": "✓" if self.key_ok else "✗",
                "time": "✓" if self.time_ok else "✗", "sec": f"{self.seconds:.0f}", "review": self.review}


def _matched(a: list, b: list) -> int:
    return sum(m.size for m in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())


def flag_quality(t: Transcription, truth: Transcription, threshold: float = 0.6) -> tuple[int, int, int]:
    """검토 표시(신뢰도 < threshold)가 실제 오류(정답과 맞춰지지 않은 음표)를 얼마나 잡는가. (둘 다, 표시, 오류)"""
    tn, gn = t.notes, truth.notes
    sm = SequenceMatcher(None, [(n.midi, n.duration) for n in tn], [(n.midi, n.duration) for n in gn], autojunk=False)
    ok = set()
    for m in sm.get_matching_blocks():
        ok.update(range(m.a, m.a + m.size))
    wrong = {i for i in range(len(tn)) if i not in ok}
    flagged = {i for i, n in enumerate(tn) if n.conf is not None and n.conf < threshold}
    return len(wrong & flagged), len(flagged), len(wrong)


def score(t: Transcription, truth: Transcription) -> Score:
    tn, gn = t.notes, truth.notes
    notes = (_matched([(n.midi, n.duration) for n in tn], [(n.midi, n.duration) for n in gn]), len(gn))
    pitch = (_matched([n.midi for n in tn if not n.is_rest], [n.midi for n in gn if not n.is_rest]),
             sum(1 for n in gn if not n.is_rest))
    chords = (_matched([normalize_chord(n.chord) for n in tn if n.chord], [normalize_chord(n.chord) for n in gn if n.chord]),
              sum(1 for n in gn if n.chord))
    lyrics = (_matched([n.lyrics[0] for n in tn if n.lyrics and n.lyrics[0]], [n.lyrics[0] for n in gn if n.lyrics and n.lyrics[0]]),
              sum(1 for n in gn if n.lyrics and n.lyrics[0]))
    has_conf = any(n.conf is not None for n in tn)
    return Score(notes, pitch, chords, lyrics, (len(t.measures), len(truth.measures)),
                 (t.fifths, t.mode) == (truth.fifths, truth.mode), (t.beats, t.beat_type) == (truth.beats, truth.beat_type),
                 t.seconds, flag_quality(t, truth) if has_conf else None)
