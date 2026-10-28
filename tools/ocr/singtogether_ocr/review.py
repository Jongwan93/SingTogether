"""M3 확인·수정 단계: 음표·코드·가사마다 신뢰도(0–1)와 근거를 매기고, 확인이 필요한 것을 표시한다.

근거는 조립 단계가 남긴 것(머리를 찾았나, 마디 머리 수가 맞나, 오선 위치로 고쳤나), 박자 합, 하이브리드일 때 Claude 전사와의
일치 여부다. 결과는 <이름>.review.json(목록)과 <이름>.review.png(원본 위 표시), MusicXML의 color(빨강)로 나간다.
사용자는 .txt를 고쳐 다시 넣으면 된다 (singtogether-ocr 고친파일.txt --transpose N).
"""
from __future__ import annotations

from difflib import SequenceMatcher
from fractions import Fraction
from pathlib import Path

from . import debug
from .ingest import Page
from .model import Transcription

FLAG = 0.6                       # 이보다 낮으면 "확인 필요"

# 근거별 가중치 (신뢰도에 곱한다). 정답 3곡의 hybrid 결과로 격자 탐색해 평균 F1이 가장 높은 값 (2026-10-28):
# 평균 정밀도 60%, 재현율 83% (주님 P74/R79, 부요함 P39/R72, 모든 능력 P67/R97). 근거 하나로도 표시되는 것은 박자 합과
# 오선 위치 고침뿐이고, 나머지는 둘이 겹쳐야 표시된다. Claude 일치는 (claude_match가 높을 때 95% 맞지만) 가산하면 재현율이
# 떨어져 가중치 1.0으로 둔다.
W_NO_HEAD = 0.8
W_HEAD_MISMATCH = 0.8
W_RHYTHM = 0.6
W_OVERRIDE = 0.6
W_CLAUDE_DIFF = 0.65
W_CLAUDE_DIFF_WEAK = 0.65
W_CLAUDE_SAME = 1.0


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
                n.reasons.append(f"박자 합 {length}/{expected}")


def mark_claude(t: Transcription, claude_t: Transcription) -> None:
    """엔진 음표 열과 Claude 전사 음표 열을 (음높이, 길이)로 맞춰 본다. 같은 자리면 '일치', 다르면 Claude가 읽은 것을 적는다."""
    a = [n for n in t.notes]
    b = [n for n in claude_t.notes]
    # Claude는 조표가 많은 곡에서 음 전체를 몇 도 밀어 읽는 일이 잦다 (♭4개 곡을 3도 위로). 상대적인 모양은 맞으므로
    # 반음 이동량을 바꿔 가며 가장 잘 맞는 정렬을 쓴다. 그러면 '다름'이 진짜 다른 곳만 가리킨다.
    best = None
    for shift in range(-12, 13):
        key = lambda n, s=shift: (None if n.midi is None else n.midi + s, n.duration)
        sm = SequenceMatcher(None, [(n.midi, n.duration) for n in a], [key(n) for n in b], autojunk=False)
        if best is None or sm.ratio() > best[0]:
            best = (sm.ratio(), shift, sm)
    ratio, shift, sm = best
    t.claude_match = round(ratio, 2)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for n in a[i1:i2]:
                n.reasons.append("AI 일치")
        elif tag == "replace":
            for k, n in enumerate(a[i1:i2]):
                other = b[j1 + k] if j1 + k < j2 else None
                n.reasons.append(f"AI와 다름: {other.name}/{other.duration}" if other else "AI에 없음")
        elif tag == "delete":
            for n in a[i1:i2]:
                n.reasons.append("AI에 없음")


def assess(t: Transcription) -> dict:
    """근거 → 신뢰도. 음표는 n.conf, 코드·가사는 n.chord_conf·n.lyric_conf를 최종값으로 덮어쓴다. 확인 목록을 돌려준다."""
    from .sanitize import canon, common_chords

    common = common_chords(t.fifths, t.mode)
    claude_ok = t.claude_match is not None and t.claude_match >= 0.6
    notes, chords, lyrics = [], [], []
    for m in t.measures:
        for i, n in enumerate(m.notes):
            c = 1.0
            why = list(n.reasons)
            if not n.is_rest and not n.head:
                c *= W_NO_HEAD
            if m.head_match is False:
                c *= W_HEAD_MISMATCH
                why.append("마디 머리 수 불일치")
            if m.rhythm_ok is False:
                c *= W_RHYTHM
            for r in n.reasons:
                if r.startswith("오선 위치로 고침"):
                    c *= W_OVERRIDE
                elif r.startswith("AI와 다름") or r == "AI에 없음":
                    c *= W_CLAUDE_DIFF if claude_ok else W_CLAUDE_DIFF_WEAK
                elif r == "AI 일치":
                    c = min(1.0, c * W_CLAUDE_SAME)
            n.conf = round(c, 2)
            notes.append({"measure": m.number, "index": i, "note": n.name, "duration": str(n.duration), "conf": n.conf,
                          "reasons": [r for r in why if r != "AI 일치"], "x": n.x, "y": n.y})
            if n.chord:
                cc = n.chord_conf if n.chord_conf is not None else 0.5
                cc = {"both": max(cc, 0.9), "claude": 0.5}.get(n.chord_src, cc)
                why = []
                if n.chord_src == "claude":
                    why.append("AI만 읽음 (자리 추정)")
                elif n.chord_src == "vision" and cc < 0.9:
                    why.append("Vision만 읽음")
                if canon(n.chord) not in common:
                    cc *= 0.7
                    why.append(f"{t.key_name}에서 드문 코드")
                n.chord_conf = round(min(1.0, cc), 2)
                chords.append({"measure": m.number, "index": i, "chord": n.chord, "conf": n.chord_conf, "reasons": why, "x": n.x, "y": n.y})
            if n.lyrics and n.lyrics[0]:
                lc = n.lyric_conf if n.lyric_conf is not None else 0.5
                lc = {"both": max(lc, 0.9), "claude": 0.5}.get(n.lyric_src, lc)
                why = {"claude": ["AI만 읽음 (자리 추정)"], "vision": ["Vision만 읽음"] if lc < 0.9 else []}.get(n.lyric_src, [])
                n.lyric_conf = round(min(1.0, lc), 2)
                lyrics.append({"measure": m.number, "index": i, "syllable": n.lyrics[0], "conf": n.lyric_conf, "reasons": why, "x": n.x, "y": n.y})
    flagged = lambda items: [it for it in items if it["conf"] < FLAG]
    return {
        "threshold": FLAG,
        "claude_match": t.claude_match,
        "summary": {"notes": len(notes), "notes_flagged": len(flagged(notes)),
                    "chords": len(chords), "chords_flagged": len(flagged(chords)),
                    "lyrics": len(lyrics), "lyrics_flagged": len(flagged(lyrics))},
        # 전부 싣는다 (앱이 신뢰도를 색으로 보여 줄 수 있게). 확인 목록은 conf < threshold.
        "notes": sorted(notes, key=lambda it: it["conf"]),
        "chords": sorted(chords, key=lambda it: it["conf"]),
        "lyrics": sorted(lyrics, key=lambda it: it["conf"]),
    }


def overlay(page: Page, t: Transcription, review: dict, path: Path) -> Path:
    """원본 위에 확인할 곳을 표시한다: 음표 빨강, 코드 주황, 가사 보라. 마디 번호와 근거를 적는다."""
    img = debug.canvas(page.gray)
    il = page.interline
    thr = review["threshold"]
    for it in review["notes"]:
        if it["x"] is None or it["conf"] >= thr:
            continue
        y = it["y"] if it["y"] is not None else 0
        debug.boxes(img, [(it["x"] - 0.7 * il, y - 0.7 * il, it["x"] + 0.7 * il, y + 0.7 * il)], "flag", thickness=2)
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
    debug.caption(img, f"{page.name}  확인 필요: 음표 {s['notes_flagged']}/{s['notes']}, 코드 {s['chords_flagged']}/{s['chords']}, "
                       f"가사 {s['lyrics_flagged']}/{s['lyrics']}  (신뢰도 < {FLAG})")
    return debug.save(path, img)
