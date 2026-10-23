"""Audiveris. 비교용으로만 쓴다 (AGPL 라이선스라 배포에 포함하려면 별도 결정 필요).

앱 위치: 환경변수 SINGTOGETHER_AUDIVERIS 또는 /Applications/Audiveris.app.
가사 OCR용 tessdata가 있으면 TESSDATA_PREFIX로 넘긴다.
"""
from __future__ import annotations

import os
from pathlib import Path

from ..ingest import Page
from ..model import Transcription, from_musicxml
from ._run import run, save_png

DEFAULT_APP = Path("/Applications/Audiveris.app")


class engine:
    name = "audiveris"

    def __init__(self, app: Path | None = None):
        self.app = Path(os.environ.get("SINGTOGETHER_AUDIVERIS", app or DEFAULT_APP))

    def transcribe(self, page: Page, work_dir: Path, debug_dir: Path | None = None) -> Transcription:
        binary = self.app / "Contents" / "MacOS" / "Audiveris"
        if not binary.exists():
            raise RuntimeError(f"Audiveris가 없어요: {self.app}")
        out_dir = work_dir / "audiveris"
        png = save_png(page.gray, out_dir / f"{page.name}.png")
        cmd = [str(binary), "-batch", "-export", "-output", str(out_dir)]
        if os.environ.get("TESSDATA_PREFIX"):
            cmd += ["-constant", "org.audiveris.omr.text.Language.defaultSpecification=eng+kor"]
        cmd += ["--", str(png)]
        seconds, log = run(cmd)
        mxl = next(out_dir.glob(f"{page.name}*.mxl"), None)
        if mxl is None:
            raise RuntimeError("Audiveris가 MXL을 만들지 않았어요:\n" + log[-1500:])
        t = from_musicxml(mxl, self.name)
        t.seconds = seconds
        return t
