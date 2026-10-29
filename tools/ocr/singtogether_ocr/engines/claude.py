"""AI CLI(Claude / GPT / Gemini)로 악보를 압축 텍스트 형식으로 받아 적게 한다. 하이브리드에서 코드·가사·제목을 맡는다.

코드와 가사까지 한 번에 읽으므로 has_chords/has_lyrics가 True다. 어떤 CLI를 부르는지는 llm.py.
엔진 이름 "claude"는 비교 평가용으로 남겨 둔 것이고, 실제로는 for_provider("gpt") 처럼 고른다.
"""
from __future__ import annotations

from pathlib import Path

from .. import llm
from ..ingest import Page
from ..model import Transcription
from ..textfmt import from_text
from ._run import save_png

PROMPT = """Look at the sheet music image {image} (open it with your file-reading tool if it is not attached). The user owns this sheet music and wants a transposed copy for their own singing. Transcribe it so it can be re-engraved in another key. Write only what is printed, including all lyrics. Even if the scan is small or blurry, give your best reading of every measure: never refuse, never summarize, never stop early.

Output format (plain text, nothing else):
title: <title>
composer: <composer or empty>
key: <tonic> <major|minor>        e.g. "G major" (from the key signature)
time: <beats>/<beat-type>
Then one line per measure, in order:
<measure number>: <tokens> | <verse 1 lyrics> | <verse 2 lyrics, if any>

Tokens, separated by spaces:
- Note: <letter><#|b|n optional><octave>/<duration>[.][~]   e.g. G4/8  F#4/8~  D4/2.   (C4 = middle C; write the sounding accidental explicitly, e.g. F#4 in G major; duration 1 2 4 8 16; "." = dotted; "~" = tied to the next note)
- Rest: r/<duration>[.]   e.g. r/8   r/1 (whole-measure rest)
- Chord symbol exactly as printed, in brackets, right before the note it sits over: [G] [D/F#] [C2] [Em]
- Barline marks: {{:  (start repeat)   :}}  (end repeat)   {{1  {{2  (start of 1st/2nd ending)
Lyrics: one syllable per note that starts a syllable, separated by spaces, in Korean or English exactly as printed; use _ for a note with no new syllable (tie continuation, melisma). Skip rests.

Example line:
1: r/8 [G] G4/8 G4/8 G4/8 [D/F#] F#4/8 F#4/8 F#4/8 E4/8~ | 주 님 의 임 재 앞 에 _
"""


CHUNK_PROMPT = """The image {image} (open it with your file-reading tool if it is not attached) shows ONE LINE (one system) cut out of a lead sheet: a treble staff with chord symbols above and lyrics below. The user owns this sheet music and wants a transposed copy for their own singing. Transcribe exactly what is printed in this line, measure by measure, so it can be re-engraved. Even if the image is small or blurry, give your best reading of every measure: never refuse, never summarize, never stop early. Do not invent measures that are not in the image.

The first measure in this image is measure __START__.__HINT__

Output format (plain text, nothing else):
key: <tonic> <major|minor>        e.g. "G major" (from the key signature at the left)
time: <beats>/<beat-type>         (if no time signature is visible, repeat the one given above)
Then one line per measure, in order:
<measure number>: <tokens> | <verse 1 lyrics> | <verse 2 lyrics, if any>

Tokens, separated by spaces:
- Note: <letter><#|b|n optional><octave>/<duration>[.][~]   e.g. G4/8  F#4/8~  D4/2.   (C4 = middle C; write the sounding accidental explicitly, e.g. F#4 in G major; duration 1 2 4 8 16; "." = dotted; "~" = tied to the next note)
- Rest: r/<duration>[.]   e.g. r/8   r/1 (whole-measure rest)
- Chord symbol exactly as printed, in brackets, right before the note it sits over: [G] [D/F#] [C2] [Em]
- Barline marks: {{:  (start repeat)   :}}  (end repeat)   {{1  {{2  (start of 1st/2nd ending)
Lyrics: one syllable per note that starts a syllable, separated by spaces, in Korean or English exactly as printed; use _ for a note with no new syllable (tie continuation, melisma). Skip rests. If the line starts with a pickup (a partial measure before the first barline), write it as its own measure.

Example line:
5: r/8 [G] G4/8 G4/8 G4/8 [D/F#] F#4/8 F#4/8 F#4/8 E4/8~ | 주 님 의 임 재 앞 에 _
"""

# 쪽 전체 대신 오선 시스템별로 잘라 보낸다 (기본). "system" = 시스템 하나씩, "half" = 가운데 마디줄에서 둘로, "" = 쪽 전체.
# 쪽 전체는 모델이 긴 변 기준으로 줄여 보므로(세로 2800px → 오선 간격 13px쯤) 시스템 크롭이 1.5배쯤 더 또렷하다.
# 실측 2026-10-29 (정답 4곡, AI 단독): 음높이 37→57 / 19→54 / 36→57 / 86→80, 코드 98→91 / 86→95 / 71→100 / 100→100,
# 가사 90→95 / 80→93 / 92→96 / 98→98. 하이브리드 끝단: 코드 96→91 / 86→95 / 79→93 / 100, 가사 73 / 82→87 / 42 / 78.
# 호출이 시스템 수만큼 늘어 WORKERS개를 동시에 돌린다 (시스템 7개 ≈ 쪽 전체 한 번과 비슷한 시간).
CROPS = "system"
WORKERS = 4


class engine:
    name = "claude"

    def __init__(self, provider: str = "claude"):
        self.provider = provider
        self.name = provider

    def transcribe(self, page: Page, work_dir: Path, debug_dir: Path | None = None) -> Transcription:
        if CROPS:
            return self.transcribe_chunks(page, work_dir, CROPS)
        out_dir = work_dir / self.provider            # Claude는 예전 캐시 폴더 이름("claude") 그대로
        cached = out_dir / f"{page.name}.txt"
        if cached.exists():                         # 같은 작업 폴더면 다시 묻지 않는다 (40초·구독 사용량 절약)
            t = from_text(cached.read_text(), self.name)
            t.engine = self.name
            return t
        # 이미지 파일은 원본 그대로 (확대본보다 낫다), PDF는 렌더링한 쪽을 넘긴다
        image = page.source if page.source.suffix.lower() != ".pdf" else save_png(page.gray, out_dir / f"{page.name}.png")
        import time
        t0 = time.time()
        text = _ask_notes(self.provider, PROMPT, image)
        t = from_text(text, self.name)
        if len(t.measures) < 2 or len(t.notes) < 8:         # 거절·요약 답("I couldn't transcribe…")은 캐시하지 않고 실패로 본다
            raise RuntimeError(f"{self.name} gave no usable transcription ({len(t.notes)} notes): {text.strip()[:120]}")
        out_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(text)
        t.engine, t.seconds = self.name, time.time() - t0
        return t


    def transcribe_chunks(self, page: Page, work_dir: Path, mode: str) -> Transcription:
        """시스템(또는 반 시스템)마다 따로 묻고 마디를 이어 붙인다. 조각마다 <이름>-c<k>.txt로 캐시."""
        import time

        import cv2
        import numpy as np

        from .. import layout, notes
        from ..model import Measure

        out_dir = work_dir / f"{self.provider}-crops"
        systems = layout.find_systems(page)
        if not systems:
            raise RuntimeError("no staff systems found for cropping")
        il = page.interline
        H, W = page.gray.shape
        chunks: list[tuple[int, int, int, int]] = []           # (y0, y1, x0, x1)
        symbols = notes.find_symbols(page, systems) if mode == "half" else None
        for k, s in enumerate(systems):
            y0 = max(0, int(s.chord_band[0] - 0.5 * il))
            y1 = min(H, int(s.lyric_band[1] + 0.3 * il))
            if mode == "half" and symbols and len(symbols[k].barlines) >= 2:
                bars = sorted(symbols[k].barlines)
                mid = min(bars[:-1], key=lambda b: abs(b - (s.staff.left + s.staff.right) / 2))
                chunks.append((y0, y1, 0, int(mid + 0.3 * il)))
                chunks.append((y0, y1, max(0, int(mid - 0.3 * il)), W))
            else:
                chunks.append((y0, y1, 0, W))
        t0 = time.time()
        # 첫 조각을 먼저 읽어 조표·박자표를 알아낸 뒤, 나머지는 동시에 묻는다. 마디 번호는 조각 안에서만 뜻이 있다
        # (앞 조각의 마디 수를 모르므로 "이 조각의 첫 마디 = k번째 시스템 시작"이라는 뜻으로 1, 100, 200…을 준다).
        from concurrent.futures import ThreadPoolExecutor

        def ask(k: int, hint: str) -> str:
            y0, y1, x0, x1 = chunks[k - 1]
            cached = out_dir / f"{page.name}-c{k}.txt"
            if cached.exists():
                return cached.read_text()
            crop = page.gray[y0:y1, x0:x1]
            if crop.shape[1] < 1400:                               # 반 조각은 조금 키워 보낸다
                f = 1400 / crop.shape[1]
                crop = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
            png = save_png(crop, out_dir / f"{page.name}-c{k}.png")
            # {image}와 {{: 는 llm.ask가 format으로 채우므로 여기서는 건드리지 않는다
            prompt = CHUNK_PROMPT.replace("__START__", str(1 if k == 1 else 100 * (k - 1))).replace("__HINT__", hint)
            text = _ask_notes(self.provider, prompt, png)
            cached.write_text(text)
            return text

        texts = [ask(1, "")]
        first = from_text(texts[0], self.name)
        hint = f" The key is {first.key_name} and the time signature is {first.beats}/{first.beat_type}." if first.measures else ""
        if len(chunks) > 1:
            with ThreadPoolExecutor(max_workers=max(1, WORKERS)) as pool:
                texts += list(pool.map(lambda k: ask(k, hint), range(2, len(chunks) + 1)))
        merged = Transcription(self.name)
        start = 1
        for k, text in enumerate(texts, 1):
            part = from_text(text, self.name)
            if k == 1 and part.measures:
                merged.fifths, merged.mode, merged.beats, merged.beat_type = part.fifths, part.mode, part.beats, part.beat_type
            for m in part.measures:
                m.number = start
                start += 1
                merged.measures.append(m)
            merged.has_chords |= part.has_chords
            merged.has_lyrics |= part.has_lyrics
        if len(merged.notes) < 8:
            raise RuntimeError(f"{self.name} gave no usable transcription from {len(chunks)} crops")
        merged.seconds = time.time() - t0
        return merged


def _ask_notes(provider: str, prompt: str, image: Path) -> str:
    """음표가 든 답을 받을 때까지 최대 두 번 묻는다. 같은 그림이라도 가끔 거절하거나 요약만 하므로, 두 번째는 용도를 덧붙인다."""
    text = llm.ask(provider, prompt, image)
    if len(from_text(text).notes) >= 4:
        return text
    retry = prompt + "\n\nThis is the owner's own sheet music and the transcription is only used to print it in another key for them to sing. Transcribe the notation as printed; do not refuse or summarize."
    return llm.ask(provider, retry, image)


def for_provider(provider: str) -> engine:
    return engine(provider)
