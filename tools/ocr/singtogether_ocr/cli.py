"""singtogether-ocr: 악보 이미지/PDF → MusicXML (+ 압축 텍스트). 선택: 이조, 렌더링, 단계별 오버레이.
오선이 없는 코드 차트(코드 줄 + 가사 줄)는 텍스트 악보(.txt)로 만든다.

    singtogether-ocr input.pdf --out output.musicxml
    singtogether-ocr score.jpg --engine homr --debug dbg/ --transpose -2 --render
    singtogether-ocr chart.jpg --llm claude --transpose 2        # 코드 차트 → chart.txt, chart (+2, A).txt
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import assemble, chordsheet, engines, hybrid, ingest, key, layout, lyrics, notes, text, textfmt
from .model import Transcription, rekey
from .musicxml_out import to_musicxml

REPO = Path(__file__).resolve().parents[3]


def recognize(src: Path, engine_name: str = "homr", work: Path | None = None, debug_dir: Path | None = None,
              chords: bool = True, interline: float = ingest.TARGET_INTERLINE, lyrics_: bool = True,
              mask_text: bool = False) -> Transcription:
    """전체 파이프라인. 여러 쪽이면 마디를 이어 붙인다."""
    work = work or Path(tempfile.mkdtemp(prefix="singtogether-"))
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
    text_from_claude = engine_name == "hybrid"
    eng = engines.get("homr" if text_from_claude else engine_name)
    claude_eng = engines.get("claude") if text_from_claude else None
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
            claude_t = claude_eng.transcribe(page, work, debug_dir) if claude_eng else None
            if chords:
                chord_boxes = text.read_chords(page, systems, debug_dir)
                if claude_t:
                    chord_boxes, ratio = hybrid.merge_chords(chord_boxes, claude_t, systems, page.interline)
                    print(f"하이브리드 코드: Claude {sum(1 for n in claude_t.notes if n.chord)}개, 앵커 {ratio:.0%}")
                assemble.attach_chords(t, chord_boxes, page.interline)
                inferred = key.infer_key([n.chord for n in t.notes if n.chord])
                if inferred and inferred[2] >= 0.7 and (inferred[0], inferred[1]) != (t.fifths, t.mode):
                    rekey(t, inferred[0], inferred[1])
            key.infer_time(t)
            if lyrics_:
                sylls = lyrics.read_lyrics(page, systems, debug_dir)
                if claude_t:
                    sylls, ratio = hybrid.merge_lyrics(sylls, claude_t, systems, page.interline)
                    print(f"하이브리드 가사: 앵커 {ratio:.0%}")
                lyrics.attach_lyrics(t, sylls, page.interline)
                if claude_t and claude_t.title:
                    t.title, t.composer = claude_t.title, claude_t.composer
                if not t.title:
                    t.title, t.composer = lyrics.read_title(page, systems)
            if debug_dir:
                assemble.overlay(page, t, debug_dir)
        if merged is None:
            merged = t
        else:
            merged.measures += t.measures
            merged.has_chords |= t.has_chords
            merged.has_lyrics |= t.has_lyrics
    if merged is None:
        raise RuntimeError("쪽이 없어요")
    merged.seconds = time.time() - t0
    return merged


def recognize_chordsheet(src: Path, llm: str = "auto", work: Path | None = None) -> str | None:
    """오선이 없는 코드 차트면 텍스트 악보를, 오선 악보면 None을 돌려준다."""
    pages = ingest.prepare(src)
    if not pages or not chordsheet.is_chord_chart(pages[0]):
        return None
    work = work or Path(tempfile.mkdtemp(prefix="singtogether-"))
    texts = []
    for page in pages:
        text, method, seconds = chordsheet.transcribe(src, page, work, llm)
        n = sum(1 for line in text.splitlines() if chordsheet.is_chord_line(line))
        print(f"코드 차트 ({method}): {page.name} 코드 줄 {n}개, {seconds:.0f}초")
        texts.append(text)
    return "\n".join(texts)


def write_outputs(t: Transcription, out: Path) -> tuple[Path, Path]:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(to_musicxml(t))
    txt = out.with_suffix(".txt")
    txt.write_text(textfmt.to_text(t))
    return out, txt


def transpose(musicxml: Path, semitones: int) -> Path:
    """Swift CLI(singtogether-transpose)로 이조본을 만든다."""
    binary = os.environ.get("SINGTOGETHER_TRANSPOSE") or next(
        (p for p in (REPO / ".build/release/singtogether-transpose", REPO / "build/singtogether-transpose") if p.exists()), None)
    if not binary:
        raise RuntimeError("singtogether-transpose가 없어요. 저장소 루트에서 ./build.sh 를 먼저 실행하세요")
    p = subprocess.run([str(binary), str(musicxml), str(semitones)], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    keys, path = p.stdout.strip().splitlines()[:2]
    print(f"이조: {keys}")
    return Path(path)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="singtogether-ocr", description="악보 이미지/PDF → MusicXML")
    ap.add_argument("input", type=Path)
    ap.add_argument("--out", type=Path, help="출력 MusicXML (기본: 입력 옆 <이름>.musicxml)")
    ap.add_argument("--engine", default="homr", choices=engines.NAMES)
    ap.add_argument("--debug", type=Path, metavar="DIR", help="단계별 오버레이 이미지를 저장할 폴더")
    ap.add_argument("--work", type=Path, metavar="DIR", help="중간 파일 폴더 (기본: 임시 폴더)")
    ap.add_argument("--no-chords", action="store_true", help="코드 OCR 생략")
    ap.add_argument("--no-lyrics", action="store_true", help="가사 OCR 생략")
    ap.add_argument("--mask-text", action="store_true",
                    help="엔진에 주기 전 코드·가사 글자를 지운다 (homr 원본 인식은 오르지만 최종 결과는 아직 비슷해 기본 꺼짐)")
    ap.add_argument("--interline", type=float, default=ingest.TARGET_INTERLINE, metavar="PX",
                    help=f"확대 목표 오선 간격 (기본 {ingest.TARGET_INTERLINE:.0f})")
    ap.add_argument("--transpose", type=int, metavar="N", help="반음 수. 이조본도 만든다 (./build.sh 필요)")
    ap.add_argument("--render", action="store_true", help="SVG/PDF로 그린다 (이조본이 있으면 그것도)")
    ap.add_argument("--llm", default="auto", choices=chordsheet.LLMS,
                    help="코드 차트 글자 읽기: claude(Claude Code CLI) / gemini(agy) / none(Apple Vision) / auto(있는 것)")
    a = ap.parse_args(argv)

    if chart := recognize_chordsheet(a.input, a.llm, a.work):
        out = (a.out.with_suffix(".txt") if a.out else a.input.with_suffix(".txt"))
        out.write_text(chart)
        print(f"→ {out}")
        if a.transpose:
            print(f"→ {transpose(out, a.transpose)}")
        if a.render:
            print("코드 차트는 렌더링하지 않아요 (.txt 그대로 쓰세요)")
        return

    t = recognize(a.input, a.engine, a.work, a.debug, chords=not a.no_chords, interline=a.interline, lyrics_=not a.no_lyrics,
                  mask_text=a.mask_text)
    out, txt = write_outputs(t, a.out or a.input.with_suffix(".musicxml"))
    n_chords = sum(1 for n in t.notes if n.chord)
    n_lyr = sum(1 for n in t.notes if n.lyrics and n.lyrics[0])
    print(f"{a.engine}: {t.title or '(제목 없음)'} — {t.key_name}, {t.beats}/{t.beat_type}, {len(t.measures)}마디, "
          f"음표 {len(t.notes)}, 코드 {n_chords}, 가사 {n_lyr}음절, {t.seconds:.0f}초")
    print(f"→ {out}\n→ {txt}")

    targets = [out]
    if a.transpose:
        targets.append(transpose(out, a.transpose))
        print(f"→ {targets[-1]}")
    if a.render:
        from .render import render
        for p in targets:
            for f in render(p, p.with_suffix("")):
                print(f"→ {f}")


if __name__ == "__main__":
    main(sys.argv[1:])
