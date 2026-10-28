"""singtogether-eval: music_scores/의 샘플마다 엔진들을 돌려 정답(truth/<이름>.txt)과 비교한 표를 만든다.

    singtogether-eval                       # 정답이 있는 샘플 전부, 기본 엔진들
    singtogether-eval --engines homr claude --samples "주님의 임재 앞에서"
    singtogether-eval --cached              # 이미 돌린 결과(out/<엔진>/<이름>.txt)만 다시 채점

결과: out/<엔진>/<이름>.txt|.musicxml, out/<엔진>/dbg/ 오버레이, out/report.md
"""
from __future__ import annotations

import argparse
import sys
import traceback
import unicodedata
from pathlib import Path

from ..cli import recognize, write_outputs
from ..textfmt import from_text
from .score import Score, score

OCR_ROOT = Path(__file__).resolve().parents[2]          # tools/ocr
REPO = OCR_ROOT.parents[1]
SCORES = REPO / "music_scores"
TRUTH = OCR_ROOT / "truth"
OUT = OCR_ROOT / "out"
DEFAULT_ENGINES = ["homr", "oemer", "audiveris", "claude"]
COLUMNS = ["notes", "pitch", "chords", "lyrics", "measures", "key", "time", "sec"]


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)       # macOS 파일 이름은 NFD, 코드·인자는 NFC


def samples(names: list[str] | None) -> list[tuple[str, Path]]:
    wanted = {_nfc(n) for n in names} if names else None
    images = {_nfc(p.stem): p for p in SCORES.rglob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".pdf")}
    out = []
    for truth in sorted(TRUTH.glob("*.txt")):
        name = _nfc(truth.stem)
        if wanted and name not in wanted:
            continue
        if name in images:
            out.append((name, images[name]))
    return out


def run_one(engine: str, name: str, img: Path, cached: bool, interline: float) -> tuple[Score | None, str]:
    out_dir = OUT / engine
    txt = out_dir / f"{name}.txt"
    truth_file = next(p for p in TRUTH.glob("*.txt") if _nfc(p.stem) == name)
    truth = from_text(truth_file.read_text())
    if not (cached and txt.exists()):
        try:
            t = recognize(img, engine, out_dir / "work", out_dir / "dbg", interline=interline)
            write_outputs(t, out_dir / f"{name}.musicxml")
            (out_dir / f"{name}.sec").write_text(f"{t.seconds:.1f}")
        except Exception as e:
            (out_dir / f"{name}.error.txt").parent.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{name}.error.txt").write_text(traceback.format_exc())
            return None, f"실패: {str(e).splitlines()[0][:60]}"
    if not txt.exists():
        return None, "결과 없음"
    t = from_text(txt.read_text())
    # 캐시에서 읽으면 시간을 모르므로 musicxml 옆 .sec 파일에서 가져온다
    sec = out_dir / f"{name}.sec"
    if t.seconds == 0 and sec.exists():
        t.seconds = float(sec.read_text())
    return score(t, truth), ""


def chordsheet_eval(llm: str, names: list[str] | None) -> str:
    """코드 차트: truth/chordsheets/<이름>.txt(코드만, | 로 줄 구분)와 비교해 코드 재현율을 낸다."""
    import re
    from collections import Counter
    from ..chordsheet import chord_token, is_chord_line
    from ..cli import recognize_chordsheet

    def tokens(text: str) -> Counter:
        out: list[str] = []
        for line in text.splitlines():
            if not is_chord_line(line):
                continue
            for raw in re.split(r"[\s\-–—|]+", line):
                t = chord_token(raw) if raw else None
                if t:
                    out.append(t)
        return Counter(out)

    folder = SCORES / "english"
    images = {_nfc(p.stem): p for p in folder.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".pdf")} if folder.exists() else {}
    lines = [f"| 샘플 | 방법 | 코드 재현율 | 엉뚱 | 빠짐 | 초 |", "|---|---|---|---|---|---|"]
    for truth_file in sorted((TRUTH / "chordsheets").glob("*.txt")):
        name = _nfc(truth_file.stem)
        if (names and name not in {_nfc(n) for n in names}) or name not in images:
            continue
        T = Counter(sum((list(tokens("| ".join(l.split("|"))).elements()) for l in truth_file.read_text().splitlines()), []))
        out_dir = OUT / "chordsheets" / llm
        import time
        t0 = time.time()
        text = recognize_chordsheet(images[name], llm, out_dir / "work") or ""
        (out_dir / f"{name}.txt").parent.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{name}.txt").write_text(text)
        R = tokens(text)
        hit = sum((T & R).values())
        lines.append(f"| {name} | {llm} | {hit / max(1, sum(T.values())):.0%} ({hit}/{sum(T.values())}) | {sum((R - T).values())} | "
                     f"{' '.join(f'{k}×{v}' for k, v in (T - R).items()) or '-'} | {time.time() - t0:.0f} |")
        print(lines[-1], flush=True)
    return "\n".join(lines) + "\n"


def rendered_eval(names: list[str] | None, widths=(2480, 1240, 620)) -> str:
    """정답 악보를 Verovio로 깨끗하게 그려 파이프라인에 넣는다 = 입력이 좋을 때의 상한이자 조립 단계의 회귀 검사.
    엔진(homr)은 이 입력을 100% 읽으므로, 최종 점수가 100%에서 모자란 만큼이 조립 단계의 손실이다."""
    import subprocess

    from ..render import render

    lines = ["| 샘플 | 렌더 폭 | " + " | ".join(COLUMNS) + " |", "|" + "---|" * (len(COLUMNS) + 2)]
    for truth_file in sorted(TRUTH.glob("*.txt")):
        name = _nfc(truth_file.stem)
        if names and name not in {_nfc(n) for n in names}:
            continue
        truth = from_text(truth_file.read_text())
        d = OUT / "rendered" / name
        d.mkdir(parents=True, exist_ok=True)
        xml, _ = write_outputs(truth, d / "truth.musicxml")
        svg = next(p for p in render(xml, d / "page", pdf=False) if p.suffix == ".svg")
        for w in widths:
            png = d / f"w{w}.png"
            subprocess.run(["rsvg-convert", "-w", str(w), "-b", "white", "-o", str(png), str(svg)], check=True)
            try:
                t = recognize(png, "homr", d / f"work-{w}", None)
                write_outputs(t, d / f"w{w}-result.musicxml")
                row = score(t, truth).row()
            except Exception as e:
                row = {c: "" for c in COLUMNS}
                row["notes"] = f"실패: {str(e).splitlines()[0][:50]}"
            lines.append(f"| {name} | {w}px | " + " | ".join(row[c] for c in COLUMNS) + " |")
            print(lines[-1], flush=True)
    return "\n".join(lines) + "\n"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="singtogether-eval", description="엔진별 인식 정확도 비교")
    ap.add_argument("--engines", nargs="+", default=DEFAULT_ENGINES)
    ap.add_argument("--samples", nargs="+", help="샘플 이름 (truth/<이름>.txt)")
    ap.add_argument("--cached", action="store_true", help="이미 돌린 결과만 다시 채점")
    ap.add_argument("--interline", type=float, default=24.0)
    ap.add_argument("--chordsheets", action="store_true", help="코드 차트(music_scores/english, truth/chordsheets) 평가")
    ap.add_argument("--rendered", action="store_true", help="정답을 Verovio로 그려 넣는 상한·조립 회귀 검사 → out/rendered.md")
    ap.add_argument("--llm", default="auto", help="코드 차트 글자 읽기: auto|claude|gemini|none")
    a = ap.parse_args(argv)

    if a.rendered:
        report = rendered_eval(a.samples)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "rendered.md").write_text("# 깨끗한 렌더링 입력\n\n" + report)
        print("\n" + report)
        return
    if a.chordsheets:
        report = chordsheet_eval(a.llm, a.samples)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "chordsheets.md").write_text("# 코드 차트\n\n" + report)
        print("\n" + report)
        return

    items = samples(a.samples)
    if not items:
        sys.exit("정답 파일(truth/*.txt)과 같은 이름의 악보가 music_scores/에 없어요")
    lines = ["# 엔진 비교", "", "| 샘플 | 엔진 | " + " | ".join(COLUMNS) + " |", "|" + "---|" * (len(COLUMNS) + 2)]
    for name, img in items:
        for engine in a.engines:
            print(f"{name} × {engine} ...", flush=True)
            s, err = run_one(engine, name, img, a.cached, a.interline)
            row = s.row() if s else {c: "" for c in COLUMNS}
            if err:
                row["notes"] = err
            lines.append(f"| {name} | {engine} | " + " | ".join(row[c] for c in COLUMNS) + " |")
            print("   ", err or row)
    report = "\n".join(lines) + "\n\nnotes = 음높이·길이 둘 다 맞은 비율, pitch = 음높이만, 마디 경계가 달라도 순서가 맞으면 인정.\n"
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.md").write_text(report)
    print("\n" + report)


if __name__ == "__main__":
    main(sys.argv[1:])
