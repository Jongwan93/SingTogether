"""singtogether-transpose(Swift) 검증: MusicXML 경로가 정확히 이조되는지.

두 가지를 확인한다.
  1. 왕복: +n 뒤 -n 하면 원본과 음(MIDI)·철자·조표·코드가 같다. (조표 ±6은 이명동음 선택이 바뀔 수 있어 철자는 넘어감)
  2. 오라클: music21이 같은 목표 조로 이조한 결과와 음·철자·조표·코드 근음/베이스가 같다.
     가사·리듬·음표 수는 원본과 그대로다.

입력: music21에 번들된 Lilypond MusicXML 테스트 스위트(조표·코드·이조악기·가사 등 케이스), 코퍼스(바흐 코랄 등),
      tools/ocr/out/*/*.musicxml (리드시트).

실행:  tools/ocr/.venv/bin/python tests/transpose_check.py [--quick] [--files a.xml b.mxl ...]
결과:  tests/transpose_report.md, 하드 실패가 있으면 종료 코드 1
"""
from __future__ import annotations

import argparse
import random
import subprocess
import sys
import tempfile
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

warnings.filterwarnings("ignore")
import music21  # noqa: E402
from music21 import converter, harmony, interval, key, pitch  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / ".build/release/singtogether-transpose"
REPORT = Path(__file__).with_name("transpose_report.md")
ORACLE_SEMIS = (2, -3, 5)
ROUNDTRIP_SEMIS = (3, -5)

# 우리 이조기가 다루지 않기로 한 것들 (실패해도 '알려진 한계'로 분류)
KNOWN = {
    "13c-KeySignatures-NonTraditional": "비전통 조표(key-step 나열)",
    "13d-KeySignatures-Microtones": "미분음 조표",
    "01d-Pitches-Microtones": "미분음",
    "71c-ChordsFrets": "기타 프렛 다이어그램은 옮기지 않음",
    "71d-ChordsFrets-Multistaff": "기타 프렛 다이어그램은 옮기지 않음",
}


@dataclass
class Result:
    name: str
    hard: list[str] = field(default_factory=list)     # 음이 틀림, 내용 손실, 실행 실패
    soft: list[str] = field(default_factory=list)     # 철자·조표 선택 차이
    checks: int = 0


def target_fifths(old: int, s: int) -> int:
    """MusicXML.swift의 targetFifths와 같은 규칙: 5도권 이동 후 -6…6 안의 후보 중 가장 플랫 쪽."""
    d0 = (7 * s) % 12
    cands = [old + d0 + 12 * k for k in range(-3, 4) if -6 <= old + d0 + 12 * k <= 6]
    return min(cands) if cands else old


def run_cli(src: Path, semis: int, out: Path) -> None:
    p = subprocess.run([str(CLI), str(src), str(semis), "--out", str(out)], capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or f"exit {p.returncode}")


def parse(path: Path):
    return converter.parse(str(path), forceSource=True)


def parts(score):
    return list(score.parts) if score.parts else [score]


def pitches(score) -> list[tuple[int, str, float, int]]:
    out = []
    for p in parts(score):
        for el in p.recurse().notes:
            if isinstance(el, harmony.Harmony):
                continue
            for pc in el.pitches:
                out.append((pc.midi, pc.step, float(pc.alter or 0), pc.octave if pc.octave is not None else -1))
    return out


def durations(score) -> list[float]:
    return [float(el.quarterLength) for p in parts(score) for el in p.recurse().notesAndRests if not isinstance(el, harmony.Harmony)]


def lyrics(score) -> list[str]:
    return [ly.text or "" for p in parts(score) for el in p.recurse().notes for ly in el.lyrics]


def keys(score) -> list[int]:
    # 비전통 조표(key-step 나열)는 sharps가 None → 0으로 본다 (우리 이조기도 fifths 없으면 0)
    return [ks.sharps if ks.sharps is not None else 0
            for p in parts(score) for ks in p.recurse().getElementsByClass(key.KeySignature)]


def chords(score) -> list[tuple[str, str | None]]:
    out = []
    for p in parts(score):
        for h in p.recurse().getElementsByClass(harmony.ChordSymbol):
            try:
                root = h.root().name if h.root() else "?"
                bass = h.bass(find=False)
                out.append((root, bass.name if bass is not None and bass.name != root else None))
            except Exception:  # noqa: BLE001 — 이상한 코드 기호는 비교 대상에서 뺀다
                out.append(("?", None))
    return out


def oracle_transpose(score, old_fifths: int, semis: int):
    """원본을 music21로 같은 목표 조에 맞춰 이조한다 (온음계 이동 → 철자까지 결정된다)."""
    new_fifths = target_fifths(old_fifths, semis)
    t_old = key.KeySignature(old_fifths).asKey("major").tonic
    t_new = key.KeySignature(new_fifths).asKey("major").tonic
    p_old, p_new = pitch.Pitch(t_old.name + "4"), pitch.Pitch(t_new.name + "4")
    while p_new.midi - p_old.midi < semis:
        p_new.octave += 1
    while p_new.midi - p_old.midi > semis:
        p_new.octave -= 1
    return score.transpose(interval.Interval(p_old, p_new)), new_fifths


def compare_seq(name: str, a: list, b: list, res: Result, hard: bool = True) -> None:
    res.checks += 1
    if len(a) != len(b):
        (res.hard if hard else res.soft).append(f"{name}: 개수 {len(a)} vs {len(b)}")
        return
    diff = [(i, x, y) for i, (x, y) in enumerate(zip(a, b)) if x != y]
    if diff:
        i, x, y = diff[0]
        (res.hard if hard else res.soft).append(f"{name}: {len(diff)}곳 다름 (첫 번째 #{i}: {x} → {y})")


def check_file(src: Path, tmp: Path) -> Result:
    res = Result(f"{src.parent.name}/{src.name}" if src.parent.name not in ("lilypondTestSuite", "") else src.name)
    try:
        orig = parse(src)
    except Exception as e:  # noqa: BLE001
        res.soft.append(f"music21이 원본을 못 읽음: {str(e)[:80]}")
        return res
    o_pitch, o_dur, o_lyr, o_keys, o_chords = pitches(orig), durations(orig), lyrics(orig), keys(orig), chords(orig)
    old_fifths = o_keys[0] if o_keys else 0

    # 1. 왕복
    for s in ROUNDTRIP_SEMIS:
        a, b = tmp / f"rt{s}a{src.suffix}", tmp / f"rt{s}b{src.suffix}"
        try:
            run_cli(src, s, a)
            run_cli(a, -s, b)
            back = parse(b)
        except Exception as e:  # noqa: BLE001
            res.hard.append(f"왕복 {s:+d} 실행 실패: {str(e)[:100]}")
            continue
        b_pitch = pitches(back)
        compare_seq(f"왕복 {s:+d} MIDI", [p[0] for p in o_pitch], [p[0] for p in b_pitch], res)
        enharmonic_edge = abs(old_fifths) >= 6
        compare_seq(f"왕복 {s:+d} 철자", o_pitch, b_pitch, res, hard=not enharmonic_edge and False)
        compare_seq(f"왕복 {s:+d} 조표", o_keys, keys(back), res, hard=False)
        compare_seq(f"왕복 {s:+d} 코드", o_chords, chords(back), res, hard=not enharmonic_edge)

    # 2. 오라클
    for s in ORACLE_SEMIS:
        out = tmp / f"or{s}{src.suffix}"
        try:
            run_cli(src, s, out)
            ours = parse(out)
            ref, new_fifths = oracle_transpose(orig, old_fifths, s)
        except Exception as e:  # noqa: BLE001
            res.hard.append(f"오라클 {s:+d} 실행 실패: {str(e)[:100]}")
            continue
        u_pitch, r_pitch = pitches(ours), pitches(ref)
        compare_seq(f"{s:+d} MIDI", [p[0] for p in r_pitch], [p[0] for p in u_pitch], res)
        compare_seq(f"{s:+d} 철자", [(p[1], p[2]) for p in r_pitch], [(p[1], p[2]) for p in u_pitch], res, hard=False)
        compare_seq(f"{s:+d} 리듬 보존", o_dur, durations(ours), res)
        compare_seq(f"{s:+d} 가사 보존", o_lyr, lyrics(ours), res)
        compare_seq(f"{s:+d} 코드", chords(ref), chords(ours), res, hard=False)
        u_keys = keys(ours)
        if o_keys:
            res.checks += 1
            if not u_keys or u_keys[0] != new_fifths:
                res.soft.append(f"{s:+d} 조표: 기대 {new_fifths}, 결과 {u_keys[:1]}")
    return res


def collect(quick: bool) -> list[Path]:
    root = Path(music21.__file__).parent
    suite = sorted({p for p in root.glob("**/lilypondTestSuite/*.xml")})
    corpus = sorted(root.glob("corpus/**/*.mxl")) + sorted(root.glob("corpus/**/*.xml"))
    rnd = random.Random(7)
    bach = [p for p in corpus if p.parent.name == "bach"]
    others = [p for p in corpus if p.parent.name != "bach" and p.parent.name != "trecento"]
    picked = rnd.sample(bach, 12 if quick else 40) + rnd.sample(others, min(len(others), 6 if quick else 20))
    ours = sorted((ROOT / "tools/ocr/out").glob("*/*.musicxml"))
    if quick:
        suite = [p for p in suite if any(k in p.name for k in ("Pitches", "Key", "Chord", "Transpos", "Lyrics"))]
    return suite + picked + ours


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="조표·코드·가사 관련 케이스와 적은 수의 코퍼스만")
    ap.add_argument("--files", nargs="*", type=Path, help="이 파일들만")
    a = ap.parse_args()
    if not CLI.exists():
        sys.exit(f"{CLI} 가 없어요. 저장소 루트에서 ./build.sh 를 먼저 실행하세요")
    files = a.files or collect(a.quick)
    results: list[Result] = []
    with tempfile.TemporaryDirectory(prefix="singtogether-tr-") as td:
        for i, f in enumerate(files, 1):
            print(f"[{i}/{len(files)}] {f.name}", flush=True)
            r = check_file(f, Path(td))
            results.append(r)
            for h in r.hard:
                print("    ✗", h)
            for s in r.soft:
                print("    ~", s)

    known = [r for r in results if any(r.name.startswith(k) for k in KNOWN) and (r.hard or r.soft)]
    hard = [r for r in results if r.hard and r not in known]
    soft = [r for r in results if r.soft and not r.hard and r not in known]
    clean = [r for r in results if not r.hard and not r.soft]
    lines = ["# singtogether-transpose 검증", "",
             f"파일 {len(results)}개, 검사 {sum(r.checks for r in results)}건 (왕복 {ROUNDTRIP_SEMIS}, 오라클 {ORACLE_SEMIS})", "",
             f"- 완전 일치: **{len(clean)}**", f"- 철자·조표 선택만 다름: {len(soft)}", f"- **하드 실패: {len(hard)}**",
             f"- 알려진 한계: {len(known)}", ""]
    if hard:
        lines += ["## 하드 실패 (음이 틀리거나 내용이 빠짐)", ""]
        for r in hard:
            lines += [f"- `{r.name}`"] + [f"  - {h}" for h in r.hard] + [f"  - (철자) {s}" for s in r.soft]
        lines.append("")
    if soft:
        lines += ["## 철자·조표 선택만 다른 경우", ""]
        for r in soft:
            lines += [f"- `{r.name}`"] + [f"  - {s}" for s in r.soft]
        lines.append("")
    if known:
        lines += ["## 알려진 한계", ""]
        for r in known:
            why = next(v for k, v in KNOWN.items() if r.name.startswith(k))
            lines += [f"- `{r.name}` — {why}"] + [f"  - {h}" for h in r.hard + r.soft]
        lines.append("")
    kinds = Counter(h.split(":")[0].split(" ", 1)[-1] for r in hard for h in r.hard)
    if kinds:
        lines += ["하드 실패 종류: " + ", ".join(f"{k} ×{v}" for k, v in kinds.most_common()), ""]
    REPORT.write_text("\n".join(lines))
    print("\n" + "\n".join(lines[:8]))
    print(f"→ {REPORT}")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
