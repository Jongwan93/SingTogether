"""단계별 오버레이: 회색 원본 위에 검출 결과를 색으로 그려 저장한다.

색: 오선=파랑, 음표=초록, 코드=주황, 가사=보라, 신뢰도 낮음=빨강, 영역(띠)=회색
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

COLORS = {            # BGR
    "staff": (230, 120, 0),
    "note": (40, 170, 40),
    "chord": (0, 140, 255),
    "lyric": (200, 60, 200),
    "low": (0, 0, 230),
    "band": (160, 160, 160),
    "flag": (0, 0, 220),
}
_FONT_PATHS = ["/System/Library/Fonts/AppleSDGothicNeo.ttc", "/System/Library/Fonts/Supplemental/AppleGothic.ttf"]


def canvas(gray: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def boxes(img: np.ndarray, rects, kind: str, labels=None, thickness: int = 1) -> None:
    """rects: (x0, y0, x1, y1) 목록. labels를 주면 상자 위에 글자를 쓴다."""
    for i, (x0, y0, x1, y1) in enumerate(rects):
        cv2.rectangle(img, (int(x0), int(y0)), (int(x1), int(y1)), COLORS[kind], thickness)
    if labels:
        text(img, [((r[0], r[1] - 2), s) for r, s in zip(rects, labels)], kind)


def hlines(img: np.ndarray, ys, x0: int, x1: int, kind: str) -> None:
    for y in ys:
        cv2.line(img, (int(x0), int(y)), (int(x1), int(y)), COLORS[kind], 1)


def band(img: np.ndarray, y0: int, y1: int, kind: str = "band") -> None:
    """가로 띠를 반투명하게 칠한다."""
    overlay = img.copy()
    cv2.rectangle(overlay, (0, int(y0)), (img.shape[1], int(y1)), COLORS[kind], -1)
    cv2.addWeighted(overlay, 0.12, img, 0.88, 0, img)


def text(img: np.ndarray, items, kind: str, size: int = 14) -> None:
    """items: ((x, y), 문자열) 목록. 한글이 나오므로 PIL로 그린다. y는 글자 아래쪽 기준."""
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    font = _font(size)
    r, g, b = COLORS[kind][::-1]
    for (x, y), s in items:
        draw.text((int(x), int(y) - size), s, fill=(r, g, b), font=font)
    img[:] = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)


def caption(img: np.ndarray, s: str) -> None:
    cv2.rectangle(img, (0, 0), (img.shape[1], 22), (255, 255, 255), -1)
    text(img, [((4, 20), s)], "staff", size=16)


def save(path: Path, img: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".png", img)[1].tofile(str(path))      # 한글 경로 대응
    return path


def _font(size: int) -> ImageFont.ImageFont:
    for p in _FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()
