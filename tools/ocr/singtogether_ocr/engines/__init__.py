"""3단계 멜로디 인식 엔진. 이름으로 고른다: homr, oemer, audiveris(비교용, AGPL), claude(비교용, Claude CLI)."""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Protocol

from ..ingest import Page
from ..model import Transcription

NAMES = ["homr", "oemer", "audiveris", "claude"]


class Engine(Protocol):
    name: str

    def transcribe(self, page: Page, work_dir: Path, debug_dir: Path | None = None) -> Transcription: ...


def get(name: str) -> Engine:
    if name not in NAMES:
        raise ValueError(f"모르는 엔진: {name} (가능: {', '.join(NAMES)})")
    return importlib.import_module(f".{name}", __name__).engine()
