"""oemer (End-to-end OMR). 별도 Python 3.11 환경(.venv-oemer)의 CLI를 부른다. 출력: MusicXML."""
from __future__ import annotations

import os
from pathlib import Path

from ..ingest import Page
from ..model import Transcription, from_musicxml
from ._run import run, save_png

DEFAULT_BIN = Path(__file__).resolve().parents[2] / ".venv-oemer" / "bin" / "oemer"


class engine:
    name = "oemer"

    def __init__(self, binary: Path | None = None):
        self.binary = Path(os.environ.get("SINGTOGETHER_OEMER", binary or DEFAULT_BIN))

    def transcribe(self, page: Page, work_dir: Path, debug_dir: Path | None = None) -> Transcription:
        if not self.binary.exists():
            raise RuntimeError(f"oemer not found: {self.binary}  (in tools/ocr: uv venv --python 3.11 .venv-oemer, then install)")
        out_dir = work_dir / "oemer"
        png = save_png(page.gray, out_dir / f"{page.name}.png")
        seconds, _ = run([str(self.binary), "-o", str(out_dir), str(png)])
        xml = next(out_dir.glob(f"{page.name}*.musicxml"), None) or next(out_dir.glob("*.musicxml"), None)
        if xml is None:
            raise RuntimeError("oemer did not produce MusicXML")
        t = from_musicxml(xml, self.name)
        t.seconds = seconds
        return t
