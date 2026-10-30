"""singtogether-eval: music_scores/의 샘플마다 엔진들을 돌려 정답(truth/<이름>.txt)과 비교한 표를 만든다.

    singtogether-eval                       # 정답이 있는 샘플 전부, 기본 엔진들
    singtogether-eval --engines homr claude --samples "주님의 임재 앞에서"
    singtogether-eval --engines hybrid:claude hybrid:gemini      # 하이브리드의 글자 읽기 AI를 바꿔서
    singtogether-eval --engines claude+page claude claude+half   # AI에게 쪽 전체 / 시스템별(기본) / 반 시스템별 크롭을 보내서
    singtogether-eval --engines homr homr@denoise homr@sharpen homr@binary homr@staff   # 전처리 실험 (1-e)
    singtogether-eval --cached              # 이미 돌린 결과(out/<엔진>/<이름>.txt)만 다시 채점
    singtogether-eval --missing             # music_scores/에 있지만 정답(truth/)이 없는 악보 목록
    singtogether-eval --tune                # hybrid 결과로 검토 표시 가중치(review.WEIGHTS)를 다시 찾는다

정답은 앱의 확인 창에서 고친 뒤 "Save as Truth…"로 저장하거나(truth/<이름>.txt), .txt를 손으로 써서 둔다.
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
SCORES = REPO / "music_scores"        # 저작권 있는 샘플 (git 밖)
SAMPLES = REPO / "samples"            # 공개 도메인 찬송을 깨끗하게 그린 것 (저장소 안; CI와 README가 쓴다)
TRUTH = OCR_ROOT / "truth"
OUT = OCR_ROOT / "out"
DEFAULT_ENGINES = ["homr", "oemer", "audiveris", "claude"]
COLUMNS = ["notes", "pitch", "chords", "lyrics", "measures", "key", "time", "sec", "review"]


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)       # macOS 파일 이름은 NFD, 코드·인자는 NFC


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".pdf")


def score_images() -> dict[str, Path]:
    """이름(확장자 뺀 파일 이름) → 그림. music_scores/가 samples/보다 우선한다 (같은 이름이면 진짜 악보를 평가)."""
    out: dict[str, Path] = {}
    for root in (SCORES, SAMPLES):
        if root.exists():
            for p in sorted(root.rglob("*")):
                if p.suffix.lower() in IMAGE_SUFFIXES:
                    out.setdefault(_nfc(p.stem), p)
    return out


def samples(names: list[str] | None) -> list[tuple[str, Path]]:
    wanted = {_nfc(n) for n in names} if names else None
    images = score_images()
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
            base, _, llm_name = engine.partition(":")      # "hybrid:gemini" = 하이브리드, 글자는 Gemini로
            from .. import ingest
            from ..engines import claude as claude_engine
            base, _, ingest.PREPROCESS = base.partition("@")   # "homr@denoise+sharpen" = 전처리 실험 (1-e)
            base, _, crops = base.partition("+")           # "claude+page" = AI에게 쪽 전체, "+system"(기본) / "+half" = 크롭을 보낸다
            claude_engine.CROPS = "" if crops == "page" else (crops or "system")
            t = recognize(img, base, out_dir / "work", out_dir / "dbg", interline=interline, review_dir=out_dir / "review",
                          llm_name=llm_name or "auto")
            from ..review import assess
            rev = assess(t)
            write_outputs(t, out_dir / f"{name}.musicxml")
            (out_dir / f"{name}.sec").write_text(f"{t.seconds:.1f}")
            import json
            (out_dir / f"{name}.review.json").write_text(json.dumps(rev, ensure_ascii=False, indent=1))
            return score(t, truth), ""                      # 신뢰도는 방금 돌린 결과에만 있다
        except Exception as e:
            (out_dir / f"{name}.error.txt").parent.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{name}.error.txt").write_text(traceback.format_exc())
            return None, f"failed: {str(e).splitlines()[0][:60]}"
    if not txt.exists():
        return None, "no result"
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

    images = score_images()
    lines = [f"| sample | method | chord recall | extra | missing | sec |", "|---|---|---|---|---|---|"]
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


def rendered_eval(names: list[str] | None, widths=(2480, 1240, 620)) -> tuple[str, int | None]:
    """정답 악보를 Verovio로 깨끗하게 그려 파이프라인에 넣는다 = 입력이 좋을 때의 상한이자 조립 단계의 회귀 검사.
    엔진(homr)은 이 입력을 100% 읽으므로, 최종 점수가 100%에서 모자란 만큼이 조립 단계의 손실이다.
    돌려주는 값: (표, 가장 낮은 음높이 점수 %). 실패한 행은 0으로 친다 (CI의 --fail-below용)."""
    import subprocess

    from ..render import render

    worst: int | None = None
    lines = ["| sample | render width | " + " | ".join(COLUMNS) + " |", "|" + "---|" * (len(COLUMNS) + 2)]
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
                pct = int(row["pitch"].split("%")[0]) if "%" in row["pitch"] else 0
            except Exception as e:
                row = {c: "" for c in COLUMNS}
                row["notes"] = f"failed: {str(e).splitlines()[0][:50]}"
                pct = 0
            worst = pct if worst is None else min(worst, pct)
            lines.append(f"| {name} | {w}px | " + " | ".join(row[c] for c in COLUMNS) + " |")
            print(lines[-1], flush=True)
    return "\n".join(lines) + "\n", worst


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="singtogether-eval", description="Compare recognition accuracy against hand-checked truth")
    ap.add_argument("--engines", nargs="+", default=DEFAULT_ENGINES)
    ap.add_argument("--samples", nargs="+", help="sample names (truth/<name>.txt)")
    ap.add_argument("--cached", action="store_true", help="re-score existing results only")
    ap.add_argument("--interline", type=float, default=24.0)
    ap.add_argument("--chordsheets", action="store_true", help="evaluate chord charts (music_scores/english, truth/chordsheets)")
    ap.add_argument("--rendered", action="store_true", help="render the truth with Verovio and recognize it (assembly regression test) → out/rendered.md")
    ap.add_argument("--widths", nargs="+", type=int, default=[2480, 1240, 620], metavar="PX", help="render widths for --rendered")
    ap.add_argument("--fail-below", type=int, metavar="PCT", help="with --rendered: exit 1 when any pitch score is below PCT (CI gate)")
    ap.add_argument("--llm", default="auto", help="AI for chords and lyrics: auto|claude|gemini|none")
    ap.add_argument("--missing", action="store_true", help="list scores in music_scores/ that have no truth file yet")
    ap.add_argument("--tune", action="store_true", help="search review flag weights on out/hybrid results (prints a WEIGHTS dict for review.py)")
    a = ap.parse_args(argv)

    if a.missing:
        have = {_nfc(p.stem) for p in TRUTH.glob("*.txt")} | {_nfc(p.stem) for p in (TRUTH / "chordsheets").glob("*.txt")}
        for name, img in sorted(score_images().items()):
            print(f"{'  truth ' if name in have else 'missing'}  {img.relative_to(REPO)}")
        return
    if a.tune:
        from .tune import main as tune
        tune(OUT / "hybrid", TRUTH)
        return

    if a.rendered:
        report, worst = rendered_eval(a.samples, tuple(a.widths))
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "rendered.md").write_text("# Clean rendered input\n\n" + report)
        print("\n" + report)
        if a.fail_below is not None and (worst is None or worst < a.fail_below):
            sys.exit(f"pitch accuracy {worst}% is below the required {a.fail_below}%")
        return
    if a.chordsheets:
        report = chordsheet_eval(a.llm, a.samples)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "chordsheets.md").write_text("# Chord charts\n\n" + report)
        print("\n" + report)
        return

    items = samples(a.samples)
    if not items:
        sys.exit("No score in music_scores/ or samples/ matches a truth file (truth/*.txt)")
    lines = ["# Engine comparison", "", "| sample | engine | " + " | ".join(COLUMNS) + " |", "|" + "---|" * (len(COLUMNS) + 2)]
    for name, img in items:
        for engine in a.engines:
            print(f"{name} × {engine} ...", flush=True)
            s, err = run_one(engine, name, img, a.cached, a.interline)
            row = s.row() if s else {c: "" for c in COLUMNS}
            if err:
                row["notes"] = err
            lines.append(f"| {name} | {engine} | " + " | ".join(row[c] for c in COLUMNS) + " |")
            print("   ", err or row)
    report = "\n".join(lines) + "\n\nnotes = pitch and length both right, pitch = pitch only; counted in order even if measure boundaries differ.\n" \
             "review = review flags (confidence < 0.6): precision P (flagged that are wrong), recall R (wrong that are flagged); " \
             "topN = share of the N most doubtful notes that are really wrong. Fresh runs only.\n"
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.md").write_text(report)
    print("\n" + report)


if __name__ == "__main__":
    main(sys.argv[1:])
