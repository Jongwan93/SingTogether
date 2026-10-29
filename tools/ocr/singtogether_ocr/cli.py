"""singtogether-ocr: 악보 이미지/PDF → MusicXML (+ 압축 텍스트). 선택: 이조, 렌더링, 단계별 오버레이.
오선이 없는 코드 차트(코드 줄 + 가사 줄)는 텍스트 악보(.txt)로 만든다.

    singtogether-ocr input.pdf --out output.musicxml
    singtogether-ocr score.jpg --engine homr --debug dbg/ --transpose -2 --render
    singtogether-ocr chart.jpg --llm claude --transpose 2        # 코드 차트 → chart.txt, chart (+2, A).txt
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import assemble, chordsheet, engines, hybrid, llm, ingest, key, layout, lyrics, notes, review, text, textfmt
from .model import Transcription, rekey
from .musicxml_out import to_musicxml

REPO = Path(__file__).resolve().parents[3]
PIPELINE_VERSION = "2026-10-28e"          # 인식 논리가 바뀌면 올린다 → 옛 캐시 무시
CACHE_DIR = Path(os.environ.get("SINGTOGETHER_CACHE") or Path.home() / "Library/Caches/SingTogether/ocr")


def cache_key(src: Path, *parts: str) -> str:
    """파일 내용 해시 + 설정. 같은 악보를 다른 이름·폴더로 저장해도 같은 키."""
    import hashlib
    h = hashlib.sha256(src.read_bytes())
    h.update("|".join((PIPELINE_VERSION, *parts)).encode())
    return h.hexdigest()[:16]


def cache_get(key: str, out: Path) -> list[Path] | None:
    """캐시에 든 결과 파일들을 out 이름으로 복사해 돌려준다. 없으면 None."""
    import shutil
    d = CACHE_DIR / key
    if not d.is_dir() or not any(d.iterdir()):
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    copied = []
    for f in sorted(d.iterdir()):
        target = out.with_suffix(f.suffix)
        shutil.copy(f, target)
        copied.append(target)
    return copied


def cache_put(key: str, files: list[Path]) -> None:
    import shutil
    d = CACHE_DIR / key
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy(f, d / f.name.replace(f.stem, "result"))


def recognize(src: Path, engine_name: str = "homr", work: Path | None = None, debug_dir: Path | None = None,
              chords: bool = True, interline: float = ingest.TARGET_INTERLINE, lyrics_: bool = True,
              mask_text: bool = True, review_dir: Path | None = None, llm_name: str = "auto") -> Transcription:
    """전체 파이프라인. 여러 쪽이면 마디를 이어 붙인다. review_dir을 주면 쪽마다 <이름>.review.png(확인할 곳 표시)를 남긴다.
    engine_name "hybrid"는 음표를 homr로, 코드·가사·제목을 AI CLI(llm_name: claude/gemini/auto)로 읽는다."""
    work = work or Path(tempfile.mkdtemp(prefix="singtogether-"))
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
    text_from_claude = engine_name == "hybrid"
    eng = engines.get("homr" if text_from_claude else engine_name)
    provider = llm.pick(llm_name) if text_from_claude else "none"
    from .engines.claude import for_provider
    claude_eng = for_provider(provider) if provider != "none" else None
    who = llm.LABELS.get(provider, provider)
    merged: Transcription | None = None
    t0 = time.time()
    for page in ingest.prepare(src, target_interline=interline, debug_dir=debug_dir):
        systems = layout.find_systems(page, debug_dir)
        # 엔진에는 코드·가사 글자를 지운 쪽을 준다 (글자를 음표로 오인하는 것이 줄어 homr 원본 인식이 올라간다). 코드·가사 OCR은 원본으로.
        engine_page = layout.clean_staff_image(page, systems, debug_dir) if mask_text and systems else page
        t = eng.transcribe(engine_page, work, debug_dir)
        if systems and not t.has_chords:
            symbols = notes.find_symbols(page, systems, debug_dir)
            assemble.place_notes(t, systems, symbols)
            claude_t = None
            ai_used = False
            if claude_eng:
                try:
                    claude_t = claude_eng.transcribe(page, work, debug_dir)
                except Exception as e:        # 로그인 만료, 가사 저작권 필터 등 → AI 없이 계속
                    print(f"{who} reading failed, using Apple Vision only: {str(e).strip().splitlines()[-1][:200]}")
            if chords:
                chord_boxes = text.read_chords(page, systems, debug_dir)
                if claude_t:
                    # 실측(주님의 임재 앞에서): 제대로 읽으면 exact 코드 92%·가사 80%, 다른 곡의 답을 주면 코드 2%·가사 11%
                    merged_boxes, ratio, exact = hybrid.merge_chords(chord_boxes, claude_t, systems, page.interline)
                    n_ai = sum(1 for n in claude_t.notes if n.chord)
                    if hybrid.trust(exact, n_ai, sum(len(b) for b in chord_boxes)):
                        chord_boxes = merged_boxes
                        ai_used = True
                        print(f"hybrid chords: {who} {n_ai}, anchored {ratio:.0%} (exact {exact:.0%})")
                    else:                    # AI가 엉뚱한 답(다른 곡·지어낸 코드)을 냈다: Vision만 쓴다
                        print(f"hybrid chords: {who} answer does not match the image (exact anchors {exact:.0%}, {n_ai} chords), using Vision only")
                assemble.attach_chords(t, chord_boxes, page.interline)
                inferred = key.infer_key([n.chord for n in t.notes if n.chord])
                if inferred and inferred[2] >= 0.7 and (inferred[0], inferred[1]) != (t.fifths, t.mode) \
                        and key.plausible_change(t, inferred[0]):
                    rekey(t, inferred[0], inferred[1])
            key.infer_time(t)
            if lyrics_:
                sylls = lyrics.read_lyrics(page, systems, debug_dir)
                if claude_t:
                    merged_sylls, ratio, exact = hybrid.merge_lyrics(sylls, claude_t, systems, page.interline)
                    n_ai = sum(1 for n in claude_t.notes if n.lyrics and n.lyrics[0])
                    if hybrid.trust(exact, n_ai, sum(len(v) for verses in sylls for v in verses)):
                        sylls = merged_sylls
                        ai_used = True
                        print(f"hybrid lyrics: anchored {ratio:.0%} (exact {exact:.0%})")
                    else:
                        print(f"hybrid lyrics: {who} answer does not match the image (exact anchors {exact:.0%}), using Vision only")
                lyrics.attach_lyrics(t, sylls, page.interline)
                if claude_t and ai_used and claude_t.title:
                    t.title, t.composer = claude_t.title, claude_t.composer
                if not t.title:
                    t.title, t.composer = lyrics.read_title(page, systems)
            review.check_rhythm(t)
            if claude_t and ai_used:
                review.mark_claude(t, claude_t)
            if debug_dir:
                assemble.overlay(page, t, debug_dir)
            if review_dir:
                review.overlay(page, t, review.assess(t), review_dir / f"{page.name}.review.png")
        if merged is None:
            merged = t
        else:
            merged.measures += t.measures
            merged.has_chords |= t.has_chords
            merged.has_lyrics |= t.has_lyrics
    if merged is None:
        raise RuntimeError("no pages")
    merged.seconds = time.time() - t0
    return merged


def recognize_chordsheet(src: Path, llm: str = "auto", work: Path | None = None, info: dict | None = None) -> str | None:
    """오선이 없는 코드 차트면 텍스트 악보를, 오선 악보면 None을 돌려준다. info를 주면 쓴 방법을 info["read_by"]에 적는다."""
    pages = ingest.prepare(src)
    if not pages or not chordsheet.is_chord_chart(pages[0]):
        return None
    work = work or Path(tempfile.mkdtemp(prefix="singtogether-"))
    texts = []
    for page in pages:
        text, method, seconds = chordsheet.transcribe(src, page, work, llm)
        n = sum(1 for line in text.splitlines() if chordsheet.is_chord_line(line))
        print(f"chord chart ({method}): {page.name} {n} chord lines, {seconds:.0f}s")
        texts.append(text)
        if info is not None:
            info["read_by"] = method
    return "\n".join(texts)


def chart_overlay(src: Path, chart_text: str, semitones: int, transposed_txt: Path, result: dict, font: str | None = None) -> Path | None:
    """코드 차트: 원본 그림 위에서 코드만 바꾼 그림을 이조된 .txt 옆에 같은 이름으로 만든다 (확장자는 원본과 같게)."""
    from . import chordoverlay
    ext = src.suffix.lower() if src.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".pdf", ".bmp", ".tif", ".tiff") else ".png"
    out = transposed_txt.with_suffix(ext)
    review_png = transposed_txt.parent / f"{src.stem}-p1.review.png"
    try:
        r = chordoverlay.overlay(src, chart_text, semitones, out, review_png, font)
    except Exception as e:                          # 그림을 못 만들어도 .txt는 있다
        print(f"chord image failed (text is there): {e}")
        result["overlay_error"] = str(e)
        return None
    if r.image is None:
        result["overlay_error"] = "; ".join(r.notes)
        return None
    print(f"→ {r.image}  ({r.replaced} chords rewritten, font {r.font})")
    for n in r.notes:
        print(f"   ! {n}")
    result.update({"transposed_txt": result.get("transposed"), "transposed": str(r.image),
                   "review_png": [str(r.review)] if r.review else [],
                   "chart": {"replaced": r.replaced, "missed": r.missed, "notes": r.notes, "font": r.font}})
    return r.image


def write_outputs(t: Transcription, out: Path) -> tuple[Path, Path]:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(to_musicxml(t))
    txt = out.with_suffix(".txt")
    txt.write_text(textfmt.to_text(t))
    return out, txt


def transpose(musicxml: Path, semitones: int, result: dict | None = None) -> Path:
    """Swift CLI(singtogether-transpose)로 이조본을 만든다. result를 주면 경로와 키를 적어 준다."""
    binary = os.environ.get("SINGTOGETHER_TRANSPOSE") or next(
        (p for p in (REPO / ".build/release/singtogether-transpose", REPO / "build/singtogether-transpose") if p.exists()), None)
    if not binary:
        raise RuntimeError("singtogether-transpose not found. Run ./build.sh in the repository root first")
    p = subprocess.run([str(binary), str(musicxml), str(semitones)], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    keys, path = p.stdout.strip().splitlines()[:2]
    print(f"transposed: {keys}")
    if result is not None:
        result["transposed"], result["keys"] = path, keys
    return Path(path)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="singtogether-ocr", description="Score image/PDF → MusicXML, transposed copy, review files")
    ap.add_argument("input", type=Path, nargs="?")
    ap.add_argument("--out", type=Path, help="output MusicXML (default: <name>.musicxml next to the input)")
    ap.add_argument("--engine", default="homr", choices=list(engines.NAMES) + ["auto"],
                    help="auto = hybrid when an AI CLI (--llm) is available, otherwise homr")
    ap.add_argument("--debug", type=Path, metavar="DIR", help="folder for one overlay image per pipeline stage")
    ap.add_argument("--work", type=Path, metavar="DIR", help="folder for intermediate files (default: a temporary folder)")
    ap.add_argument("--no-chords", action="store_true", help="skip chord OCR")
    ap.add_argument("--no-lyrics", action="store_true", help="skip lyrics OCR")
    ap.add_argument("--no-mask", action="store_true", help="do not erase chord and lyric text before the note engine")
    ap.add_argument("--interline", type=float, default=ingest.TARGET_INTERLINE, metavar="PX",
                    help=f"target staff-line spacing after scaling (default {ingest.TARGET_INTERLINE:.0f})")
    ap.add_argument("--transpose", type=int, metavar="N", help="semitones; also writes a transposed copy (needs ./build.sh)")
    ap.add_argument("--render", action="store_true", help="render SVG/PDF (the transposed copy too)")
    ap.add_argument("--llm", default="auto", choices=llm.CHOICES,
                    help="AI CLI that reads chords and lyrics: claude (Claude Code) / gemini (agy or Gemini CLI) / none (Apple Vision only) / auto (whichever is installed)")
    ap.add_argument("--no-cache", action="store_true", help="neither read nor write the result cache (~/Library/Caches/SingTogether/ocr)")
    ap.add_argument("--json", action="store_true", help="print result paths and summary as one JSON line at the end (used by the app)")
    ap.add_argument("--check-llm", metavar="NAME", choices=llm.PROVIDERS, help="report whether that AI CLI is installed and signed in, then exit")
    ap.add_argument("--source", type=Path, metavar="IMAGE", help="with a chord-chart .txt input: the original image to rewrite the chords on")
    ap.add_argument("--chart-font", metavar="NAME", help="font for rewritten chords (see chordoverlay.FONTS); default: closest match to the original")
    a = ap.parse_args(argv)
    if a.check_llm:
        status = llm.check(a.check_llm)
        print(status["message"])
        if a.json:
            print("JSON " + json.dumps(status))
        return
    if a.input is None:
        ap.error("input file is required")
    if a.engine == "auto":
        a.engine = "hybrid" if llm.pick(a.llm) != "none" else "homr"
    a.llm = llm.pick(a.llm)          # 캐시 키에 실제로 쓸 것을 넣는다
    result: dict = {"engine": a.engine, "input": str(a.input), "llm": a.llm}

    def finish():
        if a.json:
            print("JSON " + json.dumps(result, ensure_ascii=False))

    key_ = cache_key(a.input, a.engine, a.llm, "chords" if not a.no_chords else "", "lyrics" if not a.no_lyrics else "",
                     f"il{a.interline:g}", "" if a.no_mask else "mask")
    out_base = a.out or a.input.with_suffix(".musicxml")
    if not a.no_cache and (hit := cache_get(key_, out_base)):
        print(f"from cache ({key_})")
        for f in hit:
            print(f"→ {f}")
        result["cached"] = True
        for f in hit:
            result[{".musicxml": "musicxml", ".txt": "txt", ".json": "review_json"}.get(f.suffix, f.suffix)] = str(f)
        if not any(f.suffix == ".musicxml" for f in hit):
            result["chordsheet"] = True
        if a.transpose:
            main_out = next((f for f in hit if f.suffix in (".musicxml", ".txt")), hit[0])
            tr = transpose(main_out, a.transpose, result)
            print(f"→ {tr}")
            if result.get("chordsheet") and a.input.suffix.lower() != ".txt":
                chart_overlay(a.input, main_out.read_text(), a.transpose, tr, result, a.chart_font)
            if a.render and tr.suffix != ".txt":
                from .render import render
                for p in (main_out, tr):
                    for f in render(p, p.with_suffix("")):
                        print(f"→ {f}")
                        if f.suffix == ".pdf":
                            result["transposed_pdf" if p == tr else "pdf"] = str(f)
        finish()
        return

    if a.input.suffix.lower() == ".txt" and a.source:
        # 사람이 고친 코드 차트 텍스트 + 원본 그림 → 이조한 텍스트와, 원본 위에서 코드만 바꾼 그림
        result["chordsheet"], result["txt"] = True, str(a.input)
        if a.transpose:
            tr = transpose(a.input, a.transpose, result)
            print(f"→ {tr}")
            chart_overlay(a.source, a.input.read_text(), a.transpose, tr, result, a.chart_font)
        finish()
        return

    if a.input.suffix.lower() == ".txt" and "key:" in a.input.read_text()[:400]:
        # 사람이 고친 압축 텍스트 → MusicXML (+이조·렌더링). 확인·수정 단계의 마지막 걸음
        t = textfmt.from_text(a.input.read_text())
        out, _ = write_outputs(t, a.out or a.input.with_suffix(".musicxml"))
        print(f"→ {out}")
        result["musicxml"] = str(out)
        targets = [out]
        if a.transpose:
            targets.append(transpose(out, a.transpose, result))
            print(f"→ {targets[-1]}")
        if a.render:
            from .render import render
            for p in targets:
                for f in render(p, p.with_suffix("")):
                    print(f"→ {f}")
                    if f.suffix == ".pdf":
                        result["transposed_pdf" if p != out else "pdf"] = str(f)
        finish()
        return

    if chart := recognize_chordsheet(a.input, a.llm, a.work, result):
        out = (a.out.with_suffix(".txt") if a.out else a.input.with_suffix(".txt"))
        out.write_text(chart)
        print(f"→ {out}")
        result["chordsheet"], result["txt"] = True, str(out)
        if not a.no_cache:
            cache_put(key_, [out])
        if a.transpose:
            tr = transpose(out, a.transpose, result)
            print(f"→ {tr}")
            chart_overlay(a.input, chart, a.transpose, tr, result, a.chart_font)
        finish()
        return

    out_base = a.out or a.input.with_suffix(".musicxml")
    t = recognize(a.input, a.engine, a.work, a.debug, chords=not a.no_chords, interline=a.interline, lyrics_=not a.no_lyrics,
                  mask_text=not a.no_mask, review_dir=out_base.parent, llm_name=a.llm)
    rev = review.assess(t)
    rev["interline"] = a.interline          # review.png 위 코드·가사 상자 위치 계산용 (앱)
    out, txt = write_outputs(t, out_base)
    rev_path = out.with_suffix(".review.json")
    rev_path.write_text(json.dumps(rev, ensure_ascii=False, indent=1))
    s_ = rev["summary"]
    print(f"to check: notes {s_['notes_flagged']}/{s_['notes']}, chords {s_['chords_flagged']}/{s_['chords']}, lyrics {s_['lyrics_flagged']}/{s_['lyrics']} → {rev_path}")
    n_chords = sum(1 for n in t.notes if n.chord)
    n_lyr = sum(1 for n in t.notes if n.lyrics and n.lyrics[0])
    print(f"{a.engine}: {t.title or '(no title)'} — {t.key_name}, {t.beats}/{t.beat_type}, {len(t.measures)} measures, "
          f"{len(t.notes)} notes, {n_chords} chords, {n_lyr} syllables, {t.seconds:.0f}s")
    print(f"→ {out}\n→ {txt}")
    result.update({"musicxml": str(out), "txt": str(txt), "review_json": str(rev_path), "summary": s_, "key": t.key_name,
                   "review_png": [str(p) for p in sorted(out_base.parent.glob(f"{a.input.stem}-p*.review.png"))]})
    if not a.no_cache:
        cache_put(key_, [out, txt, rev_path])

    targets = [out]
    if a.transpose:
        targets.append(transpose(out, a.transpose, result))
        print(f"→ {targets[-1]}")
    if a.render:
        from .render import render
        for p in targets:
            for f in render(p, p.with_suffix("")):
                print(f"→ {f}")
                if f.suffix == ".pdf":
                    result["transposed_pdf" if p != out else "pdf"] = str(f)
    finish()


if __name__ == "__main__":
    main(sys.argv[1:])
