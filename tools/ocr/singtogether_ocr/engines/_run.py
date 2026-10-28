"""외부 명령 실행 도우미."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np


def save_png(gray: np.ndarray, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".png", gray)[1].tofile(str(path))
    return path


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None, timeout: float = 900) -> tuple[float, str]:
    """명령을 돌리고 (걸린 시간, 출력)을 돌려준다. 실패하면 RuntimeError."""
    t0 = time.time()
    p = subprocess.run(cmd, cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True, text=True, timeout=timeout)
    out = p.stdout + p.stderr
    if p.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} failed (exit {p.returncode}):\n{out[-2000:]}")
    return time.time() - t0, out
