"""M3 확인·수정 단계: 음표·코드·가사마다 신뢰도(0–1)와 근거를 매기고, 확인이 필요한 것을 표시한다.

근거는 조립 단계가 남긴 것(머리를 찾았나, 마디 머리 수가 맞나, 오선 위치로 고쳤나), 박자 합, 하이브리드일 때 AI 전사와의
일치 여부다. 세 출처(엔진 homr · 오선 위치 · AI)가 같은 음을 말하면 믿고, 서로 다르면 의심한다.
결과는 <이름>.review.json(목록, 의심스러운 순서)과 <이름>.review.png(원본 위 표시), <이름>.page.png(표시 없는 쪽 그림, 앱이
직접 표시를 그린다), MusicXML의 color(빨강)로 나간다. 사용자는 앱에서 클릭해 고치거나 .txt를 고쳐 다시 넣으면 된다.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from fractions import Fraction
from pathlib import Path

from . import debug
from .ingest import Page
from .model import STEP_PC, Note, Transcription

FLAG = 0.6                       # 이보다 낮으면 "확인 필요"
TOP = 12                         # 앱이 진하게 강조하는 가장 의심스러운 음표 수

# 신호별 가중치 (신뢰도 1.0에서 신호마다 곱한다. 1보다 크면 믿음이 올라가고, 최종값은 1.0을 넘지 않는다).
# 값은 eval/tune.py(singtogether-eval --tune)가 정답 악보의 hybrid 결과로 찾은 방향을 따르되, 정답이 4곡뿐이라 극단값은 피해
# 손으로 다듬은 것 (2026-10-29, AI 시스템별 크롭 기본값 기준 4곡: 평균 F1 0.67, 탐색 최고 0.69 — 곡마다 최적이 달라 탐색값은 과적합).
# 정답이 늘면 다시 돌려 볼 것.
# 신호 하나로는 오선 위치 고침·AI 음높이/길이 차이만 표시되고, 나머지는 둘이 겹쳐야 표시된다.
WEIGHTS = {
    "no_head": 0.8,              # 오선에서 음표 머리를 못 찾음 (자리를 시간 비율로 어림)
    "head_mismatch": 0.6,        # 마디 안 머리 수 ≠ 음표 수
    "rhythm": 0.7,               # 마디 박자 합이 박자표와 다름
    "override": 0.55,            # 오선 위치로 음높이를 고침 (엔진과 오선 위치가 달랐다)
    "head_pitch": 0.75,          # 오선 위치가 다른 음을 가리키지만 바꾸지 않음 (작은 이미지, AI 읽기가 바탕일 때)
    "ai_pitch": 0.6,             # AI가 다른 음높이로 읽음
    "ai_dur": 0.6,               # AI가 같은 음높이, 다른 길이로 읽음
    "ai_missing": 0.65,          # AI 전사에 없는 음표
    "ai_engine": 0.65,           # 오선 위치로 고치기 전 엔진이 읽은 음과 AI가 같음 (고친 쪽이 틀렸을 수 있다)
    "ai_same": 1.2,              # AI와 음높이·길이 일치 (1보다 크면 다른 의심을 상쇄한다)
    "rhythm_fixed": 0.8,         # 박자 합을 맞추려고 길이를 고친 음표 (근거가 있었지만 확인할 만하다)
    "inserted": 0.6,             # 엔진이 건너뛴 머리로 넣은 음표·쉼표 (틀릴 수 있으니 확인 목록에)
    # AI 전사가 엔진 음표 열과 잘 안 맞을 때(claude_match < AI_TRUST: 다른 곡을 읽었거나 많이 밀렸을 때)의 같은 신호.
    # 그런 곡에서는 AI가 다르다는 것이 음표가 틀렸다는 뜻이 아니다 (이 세상의 부요함보다: AI 음높이 다름 27개 중 틀린 것 2개).
    "ai_pitch_weak": 0.6,
    "ai_dur_weak": 1.0,
    "ai_missing_weak": 0.75,
    "ai_engine_weak": 0.9,
    "ai_same_weak": 1.3,
}
AI_TRUST = 0.5                   # claude_match가 이보다 낮으면 AI 신호를 _weak로 본다

_WHY = {
    "no_head": "notehead not found",
    "head_mismatch": "notehead count differs in measure",
    "rhythm": "beats do not add up",
    "override": "pitch fixed from staff position",
    "head_pitch": "staff position suggests a different pitch",
    "ai_pitch": "AI read a different pitch",
    "ai_dur": "AI read a different length",
    "ai_missing": "not in AI reading",
    "ai_engine": "AI agrees with the engine, not the staff position",
    "rhythm_fixed": "length changed to fit the beat count",
    "inserted": "added from a skipped notehead",
}


def check_rhythm(t: Transcription) -> None:
    """마디 박자 합이 박자표와 다르면 그 마디 음표 전부에 근거를 단다. 못 갖춘 첫·끝 마디, 도돌이 앞 마디는 짧아도 봐준다."""
    expected = Fraction(t.beats * 4, t.beat_type)
    for i, m in enumerate(t.measures):
        length = m.length
        short_ok = length < expected and (i == 0 or i == len(t.measures) - 1 or m.repeat_end
                                          or (i + 1 < len(t.measures) and t.measures[i + 1].ending))
        m.rhythm_ok = length == expected or short_ok or not m.notes
        if not m.rhythm_ok:
            for n in m.notes:
                n.reasons.append(f"beats add up to {length}/{expected}")


def _name_midi(name: str) -> int | None:
    m = re.match(r"^([A-G])(#{1,2}|b{1,2})?(\d)$", name)
    if not m:
        return None
    step, acc, octave = m.groups()
    return 12 * (int(octave) + 1) + STEP_PC[step] + {"#": 1, "##": 2, "b": -1, "bb": -2}.get(acc or "", 0)


def _align(t: Transcription, claude_t: Transcription):
    """엔진 음표 열과 AI 전사 음표 열을 (음높이, 길이)로 맞춘다. AI는 조표가 많은 곡에서 음 전체를 몇 도 밀어 읽는 일이 잦으므로
    (♭4개 곡을 3도 위로) 반음 이동량을 바꿔 가며 가장 잘 맞는 정렬을 쓴다. (일치 비율, 이동량, SequenceMatcher)"""
    a, b = t.notes, claude_t.notes
    best = None
    for shift in range(-12, 13):
        key = lambda n, s=shift: (None if n.midi is None else n.midi + s, n.duration)
        sm = SequenceMatcher(None, [(n.midi, n.duration) for n in a], [key(n) for n in b], autojunk=False)
        if best is None or sm.ratio() > best[0]:
            best = (sm.ratio(), shift, sm)
    return best


def counterparts(t: Transcription, claude_t: Transcription) -> tuple[float, int, dict[int, tuple[Note, str]]]:
    """(일치 비율, 이동량, id(엔진 음표) → (AI 음표, 관계)). 관계: "same" 음높이·길이 일치, "dur" 음높이만 일치,
    "pitch" 자리는 맞지만 음높이가 다름. AI 전사에 없는 음표는 빠진다."""
    ratio, shift, sm = _align(t, claude_t)
    a, b = t.notes, claude_t.notes
    ai_midi = lambda n: None if n.midi is None else n.midi + shift
    out: dict[int, tuple[Note, str]] = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for n, o in zip(a[i1:i2], b[j1:j2]):
                out[id(n)] = (o, "same")
        elif tag == "replace":
            # 다른 구간 안에서 음높이만으로 다시 맞춰 본다: 음높이는 같고 길이만 다른 것과 음높이가 다른 것을 가른다
            sub_a, sub_b = a[i1:i2], b[j1:j2]
            sub = SequenceMatcher(None, [n.midi for n in sub_a], [ai_midi(n) for n in sub_b], autojunk=False)
            for stag, k1, k2, l1, l2 in sub.get_opcodes():
                if stag == "equal":
                    for n, o in zip(sub_a[k1:k2], sub_b[l1:l2]):
                        out[id(n)] = (o, "dur")
                elif stag == "replace":
                    for k, n in enumerate(sub_a[k1:k2]):
                        if l1 + k < l2:
                            out[id(n)] = (sub_b[l1 + k], "pitch")
    return round(ratio, 2), shift, out


def mark_claude(t: Transcription, claude_t: Transcription) -> None:
    """엔진 음표마다 AI 전사와 같은지, 다르면 무엇이 다른지(음높이 / 길이 / 아예 없음)를 신호로 적는다.
    오선 위치로 고친 음표는 AI가 엔진의 원래 음과 같은지도 본다."""
    ratio, shift, pairs = counterparts(t, claude_t)
    t.claude_match = ratio
    for n in t.notes:
        pair = pairs.get(id(n))
        if pair is None:
            n.signals.append("ai_missing")
            n.reasons.append("not in AI reading")
            continue
        o, rel = pair
        if rel == "same":
            n.signals.append("ai_same")
            n.reasons.append("matches AI")
        elif rel == "dur":
            n.signals.append("ai_dur")
            n.reasons.append(f"AI read {o.name}/{o.duration}")
        else:
            n.signals.append("ai_pitch")
            n.reasons.append(f"AI read {o.name}/{o.duration}")
            # 오선 위치로 고친 음표: AI가 고치기 전 엔진의 음과 같다면 고친 쪽이 틀렸을 수 있다
            if n.engine_read and o.midi is not None and o.midi + shift == _name_midi(n.engine_read):
                n.signals.append("ai_engine")
                n.reasons.append(f"AI agrees with the engine ({n.engine_read}), not the staff position")


def effective(signals: list[str], claude_match: float | None) -> list[str]:
    """AI 전사를 믿기 어려운 곡이면 AI 신호를 _weak 변형으로 바꾼다."""
    if claude_match is None or claude_match >= AI_TRUST:
        return list(signals)
    return [s + "_weak" if s.startswith("ai_") else s for s in signals]


def confidence(signals: list[str], weights: dict[str, float] = WEIGHTS) -> float:
    c = 1.0
    for s in signals:
        c *= weights.get(s, 1.0)
    return round(min(1.0, c), 2)


def note_signals(m, n: Note) -> list[str]:
    """조립·박자 검사·AI 비교가 남긴 것을 신호 코드로 모은다 (review.json의 signals, eval/tune.py가 쓴다)."""
    sig = []
    if not n.is_rest and not n.head:
        sig.append("no_head")
    if m.head_match is False:
        sig.append("head_mismatch")
    if m.rhythm_ok is False:
        sig.append("rhythm")
    if n.engine_read:
        sig.append("override")
    sig += n.signals
    return sig


def assess(t: Transcription) -> dict:
    """근거 → 신뢰도. 음표는 n.conf, 코드·가사는 n.chord_conf·n.lyric_conf를 최종값으로 덮어쓴다. 확인 목록을 돌려준다."""
    from .sanitize import canon, common_chords

    common = common_chords(t.fifths, t.mode)
    notes, chords, lyrics = [], [], []
    for m in t.measures:
        for i, n in enumerate(m.notes):
            sig = note_signals(m, n)
            n.conf = confidence(effective(sig, t.claude_match))
            why = [r for r in n.reasons if r != "matches AI"]
            for s in ("no_head", "head_mismatch"):
                if s in sig and _WHY[s] not in why:
                    why.append(_WHY[s])
            notes.append({"measure": m.number, "index": i, "note": n.name, "duration": str(n.duration), "conf": n.conf,
                          "signals": sig, "reasons": why, "x": n.x, "y": n.y})
            if n.chord:
                cc = n.chord_conf if n.chord_conf is not None else 0.5
                cc = {"both": max(cc, 0.9), "claude": 0.5}.get(n.chord_src, cc)
                why = []
                if n.chord_src == "claude":
                    why.append("only AI read it (position guessed)")
                elif n.chord_src == "vision" and cc < 0.9:
                    why.append("only Vision read it")
                if canon(n.chord) not in common:
                    cc *= 0.7
                    why.append(f"unusual chord in {t.key_name}")
                n.chord_conf = round(min(1.0, cc), 2)
                chords.append({"measure": m.number, "index": i, "chord": n.chord, "conf": n.chord_conf, "reasons": why, "x": n.x, "y": n.y})
            if n.lyrics and n.lyrics[0]:
                lc = n.lyric_conf if n.lyric_conf is not None else 0.5
                lc = {"both": max(lc, 0.9), "claude": 0.5}.get(n.lyric_src, lc)
                why = {"claude": ["only AI read it (position guessed)"], "vision": ["only Vision read it"] if lc < 0.9 else []}.get(n.lyric_src, [])
                n.lyric_conf = round(min(1.0, lc), 2)
                lyrics.append({"measure": m.number, "index": i, "syllable": n.lyrics[0], "conf": n.lyric_conf, "reasons": why, "x": n.x, "y": n.y})
    flagged = lambda items: [it for it in items if it["conf"] < FLAG]
    # 전부 싣되 의심스러운 순서로: 신뢰도가 낮은 것부터, 같으면 신호가 많은 것부터. rank는 그 순서 (1 = 가장 의심스러움).
    notes.sort(key=lambda it: (it["conf"], -len(it["signals"]), it["measure"], it["index"]))
    for r, it in enumerate(notes, 1):
        it["rank"] = r
    return {
        "threshold": FLAG,
        "top": TOP,
        "claude_match": t.claude_match,
        "summary": {"notes": len(notes), "notes_flagged": len(flagged(notes)),
                    "chords": len(chords), "chords_flagged": len(flagged(chords)),
                    "lyrics": len(lyrics), "lyrics_flagged": len(flagged(lyrics))},
        "notes": notes,
        "chords": sorted(chords, key=lambda it: it["conf"]),
        "lyrics": sorted(lyrics, key=lambda it: it["conf"]),
    }


def overlay(page: Page, t: Transcription, review: dict, path: Path) -> Path:
    """원본 위에 확인할 곳을 표시한다: 음표 빨강(가장 의심스러운 TOP개는 굵게), 코드 주황, 가사 보라. 마디 번호와 근거를 적는다.
    표시 없는 쪽 그림도 <이름>.page.png로 남긴다 (앱이 그 위에 직접 그린다)."""
    img = debug.canvas(page.gray)
    debug.save(path.with_name(page.name + ".page.png"), img)
    il = page.interline
    thr = review["threshold"]
    top = review.get("top", TOP)
    for it in review["notes"]:
        if it["x"] is None or it["conf"] >= thr:
            continue
        y = it["y"] if it["y"] is not None else 0
        strong = it.get("rank", top + 1) <= top
        debug.boxes(img, [(it["x"] - 0.7 * il, y - 0.7 * il, it["x"] + 0.7 * il, y + 0.7 * il)], "flag", thickness=3 if strong else 1)
        debug.text(img, [((it["x"] - 0.7 * il, y + 0.8 * il), f"{it['measure']}: {it['note']}")], "flag", size=13)
    for it in review["chords"]:
        if it["x"] is None or it["conf"] >= thr:
            continue
        y = (it["y"] if it["y"] is not None else 0) - 2.2 * il
        debug.boxes(img, [(it["x"] - 0.5 * il, y - 0.6 * il, it["x"] + 1.5 * il, y + 0.6 * il)], "chord", thickness=2)
    for it in review["lyrics"]:
        if it["x"] is None or it["conf"] >= thr:
            continue
        y = (it["y"] if it["y"] is not None else 0) + 2.5 * il
        debug.boxes(img, [(it["x"] - 0.6 * il, y - 0.6 * il, it["x"] + 0.6 * il, y + 0.6 * il)], "lyric", thickness=2)
    s = review["summary"]
    debug.caption(img, f"{page.name}  to check: notes {s['notes_flagged']}/{s['notes']}, chords {s['chords_flagged']}/{s['chords']}, "
                       f"lyrics {s['lyrics_flagged']}/{s['lyrics']}  (confidence < {FLAG}; thick = {top} most doubtful)")
    return debug.save(path, img)
