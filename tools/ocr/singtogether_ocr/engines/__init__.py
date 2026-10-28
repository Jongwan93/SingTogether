"""3단계 멜로디 인식 엔진. 이름으로 고른다: homr, oemer, audiveris(비교용, AGPL), claude(비교용, Claude CLI).

"hybrid"는 엔진이 아니라 조합: 음표는 homr + 오선 위치, 코드·가사·제목은 Claude CLI (cli.recognize가 처리)."""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Protocol

from ..ingest import Page
from ..model import Transcription

NAMES = ["homr", "oemer", "audiveris", "claude", "hybrid"]


class Engine(Protocol):
    name: str

    def transcribe(self, page: Page, work_dir: Path, debug_dir: Path | None = None) -> Transcription: ...


def get(name: str) -> Engine:
    if name not in NAMES or name == "hybrid":
        raise ValueError(f"Unknown engine: {name} (choose from: {', '.join(n for n in NAMES if n != 'hybrid')})")
    return importlib.import_module(f".{name}", __name__).engine()
