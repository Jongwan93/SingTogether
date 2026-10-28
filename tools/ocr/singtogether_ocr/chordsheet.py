"""코드 차트(오선 없이 코드 줄 + 가사 줄) → 텍스트 악보(.txt). 앱의 텍스트 이조(ChordText)가 그대로 읽는다.

글자는 LLM CLI(Claude Code `claude`, Gemini `agy`)가 가장 정확하고(코드 97~100%), 없으면 Apple Vision으로 읽는다(78~96%).
Vision은 외따로 선 한 글자 코드(C, G, D)를 자주 놓치므로 배율을 바꿔 여러 번 읽어 합치고, 어떤 글상자에도 안 든
잉크 덩어리는 하나씩 잘라 다시 읽는다. 결과는 <work>/chordsheet/<이름>-<방법>.txt 에 캐시된다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

import cv2
import numpy as np

from . import ingest, layout
from .text import CHORD_VOCAB, QUALITY_RE, recognize
from .lyrics import recognize_lines

PROMPT = ("Read the image file at {image} (a chord chart: chord symbols on one line, lyrics on the next). "
          "Transcribe it exactly as plain text, keeping every chord line above its lyric line, in reading order "
          "(left column fully before right column if there are two columns). Keep chord symbols verbatim "
          "(e.g. F#m7, Eb/G, Dsus4, C2). Output only the transcription. Do not run shell commands; just view the image.")

LLMS = ("auto", "claude", "gemini", "none")
_CHORD = re.compile(r"^([A-G])([#b♯♭]?)([^\s/]*)(?:/([A-G])([#b♯♭]?))?$")
_NEUTRAL = re.compile(r"^(\||\|\||-+|\(?x\d+\)?|\(?\d+x\)?|N\.?C\.?|%|/)$", re.I)


def is_chord_chart(page: ingest.Page) -> bool:
    """오선이 하나도 안 잡히면 코드 차트로 본다."""
    return not layout.fit_staves(page.binary, layout.find_staff_lines(page.binary), page.interline)


def chord_token(tok: str) -> str | None:
    """코드 기호면 정규화한 문자열, 아니면 None. 괄호·쉼표는 벗긴다."""
    t = tok.strip("(),;:").replace("♯", "#").replace("♭", "b")
    t = re.sub(r"(?i)(sus|add|dim|aug|maj)", lambda m: m.group(1).lower(), t)   # DsUs4 → Dsus4
    m = _CHORD.match(t)
    if not m or (m.group(3) and not QUALITY_RE.match(m.group(3))):
        return None
    return t


def is_chord_line(line: str) -> bool:
    chords = other = 0
    for tok in re.split(r"\s+", line.strip()):
        if not tok or _NEUTRAL.match(tok):
            continue
        parts = [p for p in tok.split("-") if p]
        if parts and all(chord_token(p) for p in parts):
            chords += len(parts)
        else:
            other += 1
    return chords > 0 and chords >= other * 3


# ---------- LLM 전사 ----------

def _claude(image: Path) -> str:
    binary = shutil.which("claude") or "/opt/homebrew/bin/claude"
    model = os.environ.get("SINGTOGETHER_CLAUDE_MODEL", "sonnet")
    from .engines._run import run
    _, out = run([binary, "-p", PROMPT.format(image=image), "--model", model,
                  "--tools", "Read", "--allowedTools", "Read", "--output-format", "json"], timeout=600)
    result = json.loads(out[out.index("{"):])
    if result.get("is_error"):
        raise RuntimeError(f"claude 실패: {result.get('result')}")
    return result["result"]


def _gemini(image: Path) -> str:
    binary = shutil.which("agy") or str(Path.home() / ".local/bin/agy")
    from .engines._run import run
    # 헤드리스 모드에서는 프롬프트를 플래그에 붙여야 하고, 셸 명령을 쓰려 하면 권한을 못 받아 빈 결과가 나온다 (프롬프트로 막음)
    _, out = run([binary, "--add-dir", str(image.parent), "--print-timeout", "240s", "-p=" + PROMPT.format(image=image)], timeout=300)
    return out


def _available(llm: str) -> str:
    if llm == "auto":
        for name, binary in (("claude", shutil.which("claude") or "/opt/homebrew/bin/claude"),
                             ("gemini", shutil.which("agy") or str(Path.home() / ".local/bin/agy"))):
            if Path(binary).exists():
                return name
        return "none"
    return llm


def _clean(text: str) -> str:
    """LLM이 본문 일부를 ``` 울타리로 감싸기도 한다. 울타리 줄만 빼고 나머지는 그대로."""
    lines = [l for l in text.strip().splitlines() if not re.match(r"^\s*```", l)]
    return "\n".join(lines).strip() + "\n"


# ---------- Vision OCR ----------

def _vision_tokens(gray: np.ndarray) -> tuple[list[tuple], list[tuple]]:
    """(글줄 목록, 코드 토큰 목록). 글줄은 ×3 한 번 읽기, 토큰은 ×3/×4/×2 × 교정 유무를 합친 것."""
    lines = []
    found = []           # (x0, y0, x1, y1, token)
    for k in (3, 4, 2):
        big = cv2.resize(gray, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
        for corr in (False, True):
            for s, c, boxes, bb in recognize_lines(big, ("en-US",), corr):
                if k == 3 and not corr:
                    lines.append((s, tuple(v / k for v in bb)))
                for m in re.finditer(r"[^\s\-–—]+", s):
                    n = chord_token(m.group())
                    bx = [b for b in boxes[m.start():m.end()] if b]
                    if not n or not bx:
                        continue
                    x0, y0 = min(b[0] for b in bx) / k, min(b[1] for b in bx) / k
                    x1, y1 = max(b[2] for b in bx) / k, max(b[3] for b in bx) / k
                    if any(abs((fx0 + fx1) / 2 - (x0 + x1) / 2) < (x1 - x0 + fx1 - fx0) / 4 and abs((fy0 + fy1) / 2 - (y0 + y1) / 2) < (y1 - y0)
                           for fx0, fy0, fx1, fy1, _ in found):
                        continue
                    found.append((x0, y0, x1, y1, n))
    return lines, found


def _blob_tokens(gray: np.ndarray, found: list[tuple]) -> list[tuple]:
    """어떤 글상자에도 안 든 글자 크기 잉크 덩어리를 하나씩 잘라(3번 나란히 붙여 키운 뒤) 코드 어휘로 다시 읽는다."""
    h, w = gray.shape
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    ink = (binary < 128).astype(np.uint8)
    th = float(np.median([f[3] - f[1] for f in found])) if found else 12.0
    merged = cv2.dilate(ink, cv2.getStructuringElement(cv2.MORPH_RECT, (max(2, int(th * 0.5)), 1)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(merged, 8)
    extra = []
    for i in range(1, n):
        x, y, bw, bh, _ = stats[i]
        if not (0.5 * th <= bh <= 1.8 * th) or bw > 6 * th:
            continue
        cx, cy = x + bw / 2, y + bh / 2
        if any(fx0 - 2 <= cx <= fx1 + 2 and fy0 - 2 <= cy <= fy1 + 2 for fx0, fy0, fx1, fy1, _ in found):
            continue
        pad = int(th * 0.6)
        crop = gray[max(0, y - pad):min(h, y + bh + pad), max(0, x - pad):min(w, x + bw + pad)]
        gap = np.full((crop.shape[0], pad), 255, np.uint8)
        tile = np.hstack([crop, gap, crop, gap, crop])
        best = None
        for corr in (False, True):
            for tb in recognize(tile, ("en-US",), corr, custom_words=CHORD_VOCAB, min_side=96):
                toks = [chord_token(t) for t in re.split(r"[\s\-–—]+", tb.text) if chord_token(t)]
                if toks and len(set(toks)) == 1 and len(toks) <= 3:
                    best = toks[0]
                    break
            if best:
                break
        if best:
            extra.append((x, y, x + bw, y + bh, best))
    return extra


def _vision(gray: np.ndarray) -> str:
    lines, found = _vision_tokens(gray)
    found += _blob_tokens(gray, found)
    if not lines:
        return ""
    h, w = gray.shape
    heights = [bb[3] - bb[1] for _, bb in lines]
    lh = float(np.median(heights))
    # 2단: 왼쪽 반에서 끝나는 줄과 오른쪽 반에서 시작하는 줄이 둘 다 많고, 가운데를 가로지르는 줄이 거의 없을 때
    left = [l for l in lines if l[1][2] < w * 0.52]
    right = [l for l in lines if l[1][0] > w * 0.48]
    crossing = [l for l in lines if l[1][0] < w * 0.45 and l[1][2] > w * 0.55]
    two_col = len(left) >= 5 and len(right) >= 5 and len(crossing) <= max(2, len(lines) // 10)
    col = (lambda x: 1 if two_col and x > w * 0.5 else 0)
    # 코드 토큰을 y로 묶는다 (가까운 것끼리 한 줄)
    clusters: list[dict] = []
    for x0, y0, x1, y1, tok in sorted(found, key=lambda f: ((f[1] + f[3]) / 2, f[0])):
        cy, c = (y0 + y1) / 2, col(x0)
        for cl in clusters:
            if cl["col"] == c and abs(cl["y"] - cy) < lh * 0.7:
                cl["chords"].append((x0, tok)); cl["y"] = (cl["y"] * (len(cl["chords"]) - 1) + cy) / len(cl["chords"])
                break
        else:
            clusters.append({"y": cy, "col": c, "chords": [(x0, tok)]})
    # 글줄: 코드 줄로 읽힌 글줄은 토큰 묶음으로 바꾼다 (여러 번 읽어 합친 토큰이 더 완전하다). 가사 줄은 그대로.
    rows: list[dict] = []
    for s_, bb in lines:
        cy, c = (bb[1] + bb[3]) / 2, col(bb[0])
        if is_chord_line(s_) or not s_.strip():
            continue
        # "CDEm"처럼 띄어쓰기를 잃은 코드 줄은 코드 줄로 안 잡히지만, 같은 자리의 토큰 묶음을 이어 붙인 것과 같다
        squeezed = re.sub(r"\s+", "", s_)
        if any(cl["col"] == c and abs(cl["y"] - cy) < lh * 0.7 and squeezed == "".join(t for _, t in sorted(cl["chords"])) for cl in clusters):
            continue
        rows.append({"y": cy, "col": c, "text": s_, "chords": None})
    for cl in clusters:
        rows.append({"y": cl["y"], "col": cl["col"], "text": "", "chords": cl["chords"]})
    # 글자 폭: 가사 줄의 너비/글자 수 중앙값
    widths = [(bb[2] - bb[0]) / max(1, len(s_)) for s_, bb in lines if len(s_) >= 8 and not is_chord_line(s_)]
    cw = float(np.median(widths)) if widths else lh * 0.5
    margins = {c: min([l[1][0] for l in lines if col(l[1][0]) == c] or [0]) for c in (0, 1)}
    out = []
    prev_y = None
    for r in sorted(rows, key=lambda r: (r["col"], r["y"])):
        if prev_y is not None and r["y"] - prev_y > lh * 2.2:
            out.append("")                                          # 절 사이 빈 줄
        prev_y = r["y"]
        if r["chords"]:
            line = ""
            for x0, tok in sorted(r["chords"]):
                pos = max(len(line) + (1 if line else 0), int(round((x0 - margins[r["col"]]) / cw)))
                line += " " * (pos - len(line)) + tok
            out.append(line)
        else:
            out.append(r["text"])
    return "\n".join(out) + "\n"


# ---------- 진입점 ----------

def transcribe(src: Path, page: ingest.Page, work: Path, llm: str = "auto") -> tuple[str, str, float]:
    """(텍스트, 방법 이름, 걸린 초). 같은 작업 폴더에 캐시가 있으면 다시 읽지 않는다."""
    method = _available(llm)
    out_dir = work / "chordsheet"
    cached = out_dir / f"{page.name}-{method}.txt"
    if cached.exists():
        return cached.read_text(), method, 0.0
    t0 = time.time()
    if method == "none":
        text = _vision(page.gray if page.scale == 1.0 else cv2.imdecode(np.fromfile(src, np.uint8), cv2.IMREAD_GRAYSCALE)
                       if src.suffix.lower() != ".pdf" else page.gray)
    else:
        from .engines._run import save_png
        image = src.resolve() if src.suffix.lower() != ".pdf" else save_png(page.gray, out_dir / f"{page.name}.png")
        text = _clean(_claude(image) if method == "claude" else _gemini(image))
    out_dir.mkdir(parents=True, exist_ok=True)
    cached.write_text(text)
    return text, method, time.time() - t0
