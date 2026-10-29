"""정확도 계산.

마디 경계를 틀려도 순서가 맞으면 점수를 주도록, 전체를 한 줄로 펴서 가장 긴 공통 부분열(difflib)로 맞춘다.
  notes  : (음높이, 길이) 둘 다 맞은 음표·쉼표 비율
  pitch  : 음높이만 맞은 비율 (쉼표 제외)
  chords : 코드 기호가 순서대로 맞은 비율
  lyrics : 1절 음절이 순서대로 맞은 비율
  review : 검토 표시의 정밀도·재현율, 그리고 가장 의심스러운 순서로 늘어놓았을 때 앞 TOP개 중 실제 오류 비율
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from ..model import Transcription
from ..text import normalize_chord


@dataclass
class Flags:
    tp: int            # 표시했고 실제 틀린 수
    flagged: int       # 표시한 수
    wrong: int         # 실제 틀린 수
    top_hit: int       # 의심 순서 앞 top개 중 실제 틀린 수
    top: int

    @property
    def precision(self) -> float | None:
        return self.tp / self.flagged if self.flagged else None

    @property
    def recall(self) -> float | None:
        return self.tp / self.wrong if self.wrong else None

    @property
    def f1(self) -> float:
        p, r = self.precision or 0.0, self.recall or 0.0
        return 2 * p * r / (p + r) if p + r else 0.0

    def __str__(self) -> str:
        pct = lambda v: "–" if v is None else f"{100 * v:.0f}%"
        return f"P {pct(self.precision)} R {pct(self.recall)} ({self.flagged} flagged/{self.wrong} wrong) · top{self.top} {pct(self.top_hit / self.top if self.top else None)}"


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
    flags: Flags | None = None       # 검토 표시의 품질 (신뢰도가 있는 결과만)

    @property
    def review(self) -> str:
        return str(self.flags) if self.flags else "–"

    def row(self) -> dict:
        def pct(p):
            return f"{100 * p[0] / p[1]:.0f}% ({p[0]}/{p[1]})" if p[1] else "–"
        return {"notes": pct(self.notes), "pitch": pct(self.pitch), "chords": pct(self.chords), "lyrics": pct(self.lyrics),
                "measures": f"{self.measures[0]}/{self.measures[1]}", "key": "✓" if self.key_ok else "✗",
                "time": "✓" if self.time_ok else "✗", "sec": f"{self.seconds:.0f}", "review": self.review}


def _matched(a: list, b: list) -> int:
    return sum(m.size for m in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())


def wrong_notes(t: Transcription, truth: Transcription) -> set[int]:
    """정답과 맞춰지지 않은 음표의 (전체 순서) 번호."""
    sm = SequenceMatcher(None, [(n.midi, n.duration) for n in t.notes], [(n.midi, n.duration) for n in truth.notes], autojunk=False)
    ok = set()
    for m in sm.get_matching_blocks():
        ok.update(range(m.a, m.a + m.size))
    return {i for i in range(len(t.notes)) if i not in ok}


def flag_quality(t: Transcription, truth: Transcription, threshold: float = 0.6, top: int = 12) -> Flags:
    """검토 표시(신뢰도 < threshold)가 실제 오류를 얼마나 잡는가, 의심 순서(신뢰도 오름차순) 앞 top개는 얼마나 맞는가."""
    tn = t.notes
    wrong = wrong_notes(t, truth)
    flagged = {i for i, n in enumerate(tn) if n.conf is not None and n.conf < threshold}
    order = sorted((i for i, n in enumerate(tn) if n.conf is not None), key=lambda i: tn[i].conf)[:top]
    return Flags(len(wrong & flagged), len(flagged), len(wrong), sum(1 for i in order if i in wrong), len(order))


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
