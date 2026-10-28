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

PROMPT = """Look at the sheet music image {image} (open it with your file-reading tool if it is not attached). The user owns this sheet music and wants a transposed copy for their own singing. Transcribe it so it can be re-engraved in another key. Write only what is printed, including all lyrics.

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


class engine:
    name = "claude"

    def __init__(self, provider: str = "claude"):
        self.provider = provider
        self.name = provider

    def transcribe(self, page: Page, work_dir: Path, debug_dir: Path | None = None) -> Transcription:
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
        text = llm.ask(self.provider, PROMPT, image)
        out_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(text)
        t = from_text(text, self.name)
        t.engine, t.seconds = self.name, time.time() - t0
        return t


def for_provider(provider: str) -> engine:
    return engine(provider)
