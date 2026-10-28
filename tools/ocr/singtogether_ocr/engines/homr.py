"""homr (vision transformer 기반 OMR). 같은 환경에 설치된 CLI를 부른다. 출력: 이미지 옆 <이름>.musicxml."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ..ingest import Page
from ..model import Transcription, from_musicxml
from ._run import run, save_png


class engine:
    name = "homr"

    def __init__(self):
        self.binary = Path(sys.executable).parent / "homr"

    def transcribe(self, page: Page, work_dir: Path, debug_dir: Path | None = None) -> Transcription:
        if not self.binary.exists():
            raise RuntimeError(f"homr not found: {self.binary}")
        out_dir = work_dir / "homr"
        png = save_png(page.gray, out_dir / f"{page.name}.png")
        cmd = [str(self.binary), str(png)] + (["--debug"] if debug_dir else [])
        seconds, log = run(cmd, timeout=1800)
        xml = png.with_suffix(".musicxml")
        if not xml.exists():
            raise RuntimeError("homr did not produce MusicXML:\n" + log[-1500:])
        if debug_dir:
            for p in out_dir.glob(f"{page.name}_*.png"):       # homr 자체 디버그 이미지
                shutil.copy(p, debug_dir / f"{page.name}-3-homr-{p.name[len(page.name) + 1:]}")
        t = from_musicxml(xml, self.name, key_implied_alters=True)   # homr는 조표에 해당하는 음에 alter를 안 쓴다
        t.seconds = seconds
        return t
