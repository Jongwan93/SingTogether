"""1단계 입력 처리: PDF/이미지 → 쪽별 회색 이미지 → 원근·기울기 보정 → 저해상도 확대 → 이진화."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from . import debug

TARGET_INTERLINE = 24.0   # 확대 목표 오선 간격(px). 300dpi 악보가 15–20px, homr는 폭 1920px 입력에서 가장 좋았다


@dataclass
class Page:
    name: str             # "<파일이름>-p<쪽>"
    gray: np.ndarray      # 보정·확대 뒤 회색 이미지
    binary: np.ndarray    # 이진화 (검정 0, 흰색 255)
    scale: float          # 원본 대비 확대 배율
    angle: float          # 보정한 기울기 (도)
    interline: float      # 오선 간격 추정 (확대 뒤 px)
    source: Path          # 원본 파일


def prepare(path: Path, dpi: int = 300, target_interline: float = TARGET_INTERLINE,
            debug_dir: Path | None = None) -> list[Page]:
    pages = []
    for i, raw in enumerate(load_pages(path, dpi), 1):
        name = f"{path.stem}-p{i}"
        gray, _ = correct_perspective(raw)
        gray, angle = deskew(gray)
        interline = estimate_interline(binarize(gray))
        scale = 1.0
        if interline < target_interline * 0.8:            # 웹 이미지처럼 작은 악보는 키운다
            scale = target_interline / interline
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
        binary = binarize(gray)
        page = Page(name, gray, binary, scale, angle, estimate_interline(binary), path)
        pages.append(page)
        if debug_dir:
            img = debug.canvas(binary)
            debug.caption(img, f"{name}  scale x{scale:.2f}  angle {angle:+.2f}deg  interline {page.interline:.0f}px")
            debug.save(debug_dir / f"{name}-1-ingest.png", img)
    return pages


def load_pages(path: Path, dpi: int = 300) -> list[np.ndarray]:
    """PDF는 쪽마다 dpi로 렌더링, 이미지는 그대로. 회색 uint8 배열 목록."""
    if path.suffix.lower() == ".pdf":
        import fitz
        pages = []
        with fitz.open(path) as doc:
            for p in doc:
                pix = p.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
                pages.append(np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width).copy())
        return pages
    img = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_GRAYSCALE)   # 한글 경로 대응
    if img is None:
        raise ValueError(f"이미지를 읽을 수 없어요: {path}")
    return [img]


def binarize(gray: np.ndarray) -> np.ndarray:
    """배경이 고르면(스캔·웹 이미지) Otsu, 사진이면 조명을 평탄화한 뒤 Otsu."""
    bg = cv2.GaussianBlur(gray, (0, 0), max(gray.shape) / 40)
    src = gray if bg.std() < 12 else cv2.divide(gray, bg, scale=255)
    _, binary = cv2.threshold(src, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


def estimate_interline(binary: np.ndarray) -> float:
    """열마다 검정 구간 시작점 사이 거리를 모아 최빈값을 취한다. 오선 5줄이 폭 전체에 걸쳐 있어 오선 간격이 압도한다."""
    ink = binary < 128
    h, w = ink.shape
    diffs = []
    for x in range(0, w, max(1, w // 300)):
        starts = np.flatnonzero(np.diff(ink[:, x].astype(np.int8)) == 1)
        if len(starts) > 1:
            diffs.append(np.diff(starts))
    if not diffs:
        return TARGET_INTERLINE
    d = np.concatenate(diffs)
    d = d[(d >= 3) & (d <= 80)]
    return float(np.bincount(d).argmax()) if d.size else TARGET_INTERLINE


def deskew(gray: np.ndarray, max_angle: float = 4.0) -> tuple[np.ndarray, float]:
    """오선이 수평이 되는 각도 = 가로 투영의 분산이 최대인 각도. 0.5도 간격으로 훑고 0.1도로 다듬는다."""
    small = cv2.resize(gray, None, fx=min(1.0, 800 / gray.shape[1]), fy=min(1.0, 800 / gray.shape[1]))
    ink = (binarize(small) < 128).astype(np.float32)
    h, w = ink.shape

    def score(a: float) -> float:
        m = cv2.getRotationMatrix2D((w / 2, h / 2), a, 1)
        return float(cv2.warpAffine(ink, m, (w, h)).sum(axis=1).var())

    coarse = max(np.arange(-max_angle, max_angle + 0.01, 0.5), key=score)
    fine = max(np.arange(coarse - 0.5, coarse + 0.51, 0.1), key=score)
    if abs(fine) < 0.1:
        return gray, 0.0
    H, W = gray.shape
    m = cv2.getRotationMatrix2D((W / 2, H / 2), float(fine), 1)
    return cv2.warpAffine(gray, m, (W, H), flags=cv2.INTER_CUBIC, borderValue=255), float(fine)


def correct_perspective(gray: np.ndarray) -> tuple[np.ndarray, bool]:
    """사진에서 종이의 네 모서리가 또렷하면 평면으로 편다. 못 찾으면 그대로 둔다."""
    h, w = gray.shape
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        if cv2.contourArea(c) < 0.3 * h * w:
            break
        quad = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        if len(quad) != 4:
            continue
        src = _order_corners(quad.reshape(4, 2).astype(np.float32))
        tw = int(max(np.linalg.norm(src[1] - src[0]), np.linalg.norm(src[2] - src[3])))
        th = int(max(np.linalg.norm(src[3] - src[0]), np.linalg.norm(src[2] - src[1])))
        if tw < 0.5 * w or th < 0.5 * h:
            continue
        dst = np.array([[0, 0], [tw - 1, 0], [tw - 1, th - 1], [0, th - 1]], np.float32)
        m = cv2.getPerspectiveTransform(src, dst)
        return cv2.warpPerspective(gray, m, (tw, th), borderValue=255), True
    return gray, False


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """왼위, 오른위, 오른아래, 왼아래 순서로."""
    s, d = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
    return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]], np.float32)
