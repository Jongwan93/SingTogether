"""하이브리드: 위치는 로컬 파이프라인, 글자는 Claude CLI.

로컬(Vision)은 코드 기호와 가사 음절의 *위치*는 정확하지만 글자를 20~40% 틀리고, Claude는 글자는 거의 다 맞지만
위치를 모른다. 두 열을 글자 일치를 앵커로 DP 정렬해서 Vision 자리에 Claude 글자를 심는다.
Claude에만 있는 항목은 앞뒤 앵커 사이에 고르게 끼워 넣고, 정렬이 잘 됐을 때는 Vision에만 있는 항목(잡음)을 버린다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .layout import System
from .lyrics import Syllable
from .model import Transcription
from .text import TextBox, normalize_chord

_HANGUL = re.compile(r"[가-힣]")


@dataclass
class Placed:
    """정렬 결과 한 항목: 어느 오선의 어느 x에 어떤 글자를 둘지."""
    system: int
    x: float
    y: float
    text: str
    conf: float
    source: str          # "both" | "claude" | "vision"


def align(a: list, b: list, cost, gap_a: float, gap_b: float) -> list[tuple[int | None, int | None]]:
    """순서를 지키는 정렬. (i, j) 짝, (i, None) a만, (None, j) b만. 비용 최소."""
    I, J = len(a), len(b)
    INF = float("inf")
    best = [[INF] * (J + 1) for _ in range(I + 1)]
    back = [[None] * (J + 1) for _ in range(I + 1)]
    best[0][0] = 0.0
    for i in range(I + 1):
        for j in range(J + 1):
            cur = best[i][j]
            if cur == INF:
                continue
            if i < I and j < J:
                c = cur + cost(a[i], b[j])
                if c < best[i + 1][j + 1]:
                    best[i + 1][j + 1], back[i + 1][j + 1] = c, (i, j, "pair")
            if i < I and cur + gap_a < best[i + 1][j]:
                best[i + 1][j], back[i + 1][j] = cur + gap_a, (i, j, "a")
            if j < J and cur + gap_b < best[i][j + 1]:
                best[i][j + 1], back[i][j + 1] = cur + gap_b, (i, j, "b")
    out, i, j = [], I, J
    while (i, j) != (0, 0):
        pi, pj, kind = back[i][j]
        out.append((pi, pj) if kind == "pair" else (pi, None) if kind == "a" else (None, pj))
        i, j = pi, pj
    return out[::-1]


def place(vision: list[tuple[int, float, float, str, float]], claude: list[str], cost, systems: list[System],
          gap_a: float = 0.6, gap_b: float = 0.6, keep_vision_only=lambda text, conf: True) -> tuple[list[Placed], float]:
    """vision: (오선, x, y, 글자, 확신도) 읽기 순서. claude: 글자 열. → Placed 목록과 앵커 비율.
    keep_vision_only: Claude에 짝이 없는 Vision 항목을 남길지 (글자, 확신도)로 정한다."""
    pairs = align(vision, claude, lambda v, c: cost(v[3], c), gap_a, gap_b)
    matched = sum(1 for i, j in pairs if i is not None and j is not None)
    ratio = matched / max(1, len(claude))
    # 글자가 똑같이 읽힌 짝만 센 비율. 한글 음절끼리는 비슷하면 짝이 되므로(OCR 혼동 허용) 엉뚱한 곡도 matched는 꽤 나온다
    exact = sum(1 for i, j in pairs if i is not None and j is not None and cost(vision[i][3], claude[j]) == 0) / max(1, len(claude))
    out: list[Placed] = []
    pending: list[str] = []                        # 앵커 사이에 낀 Claude 전용 항목
    last_anchor: Placed | None = None

    def flush(next_anchor: Placed | None):
        """pending을 last_anchor와 next_anchor 사이에 고르게 놓는다. 한쪽이 없으면 있는 쪽 옆에 붙인다."""
        if not pending:
            return
        n = len(pending)
        if last_anchor and next_anchor and last_anchor.system == next_anchor.system:
            for k, t in enumerate(pending, 1):
                x = last_anchor.x + (next_anchor.x - last_anchor.x) * k / (n + 1)
                out.append(Placed(last_anchor.system, x, last_anchor.y, t, 0.5, "claude"))
        else:
            ref = next_anchor or last_anchor
            if ref is None:
                pending.clear()
                return
            sys_ = systems[ref.system]
            if next_anchor is not None:            # 다음 오선 앞부분: 왼쪽 끝 ~ 앵커 사이
                left = sys_.staff.left + 3 * sys_.staff.interline
                for k, t in enumerate(pending, 1):
                    out.append(Placed(ref.system, left + (ref.x - left) * k / (n + 1), ref.y, t, 0.4, "claude"))
            else:                                  # 마지막 오선 뒷부분: 앵커 ~ 오른쪽 끝 사이
                right = sys_.staff.right
                for k, t in enumerate(pending, 1):
                    out.append(Placed(ref.system, ref.x + (right - ref.x) * k / (n + 1), ref.y, t, 0.4, "claude"))
        pending.clear()

    for i, j in pairs:
        if i is not None and j is not None:
            si, x, y, _, conf = vision[i]
            anchor = Placed(si, x, y, claude[j], max(conf, 0.7), "both")
            flush(anchor)
            out.append(anchor)
            last_anchor = anchor
        elif j is not None:
            if claude[j]:                          # Claude 전용 빈 항목(음절 없는 음표)은 자리를 모르니 끼워 넣지 않는다
                pending.append(claude[j])
        else:
            si, x, y, text, conf = vision[i]
            if keep_vision_only(text, conf):
                out.append(Placed(si, x, y, text, conf, "vision"))
    flush(None)
    out.sort(key=lambda p: (p.system, p.x))
    return out, ratio, exact


# MARK: 코드

def _chord_cost(v: str, c: str) -> float:
    nv, nc = normalize_chord(v), normalize_chord(c)
    if nv == nc:
        return 0.0
    rv, rc = re.match(r"[A-G][#b]?", nv), re.match(r"[A-G][#b]?", nc)
    if rv and rc and rv.group(0) == rc.group(0):
        return 0.4
    return 1.3


def trust(exact_ratio: float, n_ai: int, n_vision: int) -> bool:
    """AI 전사를 믿을지. 글자가 똑같이 읽힌 앵커가 너무 적으면 AI가 다른 곡을 읽었거나 지어낸 것이다.
    Vision이 거의 못 읽은 쪽이면 비교할 수 없으니 AI를 믿는다. 기준값은 cli의 실측 주석 참고."""
    if n_ai == 0:
        return False
    if n_vision < 3:
        return True
    return exact_ratio >= TRUST_MIN


TRUST_MIN = 0.3


def merge_chords(vision: list[list[TextBox]], claude_t: Transcription, systems: list[System], il: float
                 ) -> tuple[list[list[TextBox]], float, float]:
    """Vision 코드 상자(오선별) + Claude 코드 열 → 오선별 TextBox (글자는 Claude). (결과, 앵커 비율, 똑같이 읽힌 비율)"""
    v_items = [(si, (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2, b.text, b.conf) for si, boxes in enumerate(vision) for b in boxes]
    c_items = [n.chord for n in claude_t.notes if n.chord]
    if not c_items:
        return vision, 0.0, 0.0
    # Claude에 없는 Vision 코드는 잡음일 때가 많다. 여러 번 읽어도 같았던 것(확신도 0.75+)만 남긴다
    placed, ratio, exact = place(v_items, c_items, _chord_cost, systems, keep_vision_only=lambda text, conf: conf >= 0.75)
    out: list[list[TextBox]] = [[] for _ in systems]
    for p in placed:
        out[p.system].append(TextBox(p.text, p.conf, int(p.x - 0.3 * il), int(p.y - 0.5 * il), int(p.x + 0.9 * il), int(p.y + 0.5 * il), p.source))
    return out, ratio, exact


# MARK: 가사

def _lyric_cost(v: str, c: str) -> float:
    if v == c:
        return 0.0
    if v and c and _HANGUL.search(v) and _HANGUL.search(c):
        return 0.5                                  # OCR이 흔히 헷갈리는 비슷한 글자 (상/사, 함/학)
    if v and c and v.isalpha() and c.isalpha():
        return 0.3 if v[:2].lower() == c[:2].lower() else 0.8
    return 1.3                                      # 글자 ↔ 자리 표시자는 짝짓지 않는다


def merge_lyrics(vision: list[list[list[Syllable]]], claude_t: Transcription, systems: list[System], il: float
                 ) -> tuple[list[list[list[Syllable]]], float, float]:
    """Vision 음절(오선별·절별) + Claude 가사(음표별) → 오선별·절별 Syllable (글자는 Claude). (결과, 1절 앵커 비율, 1절 똑같이 읽힌 비율)"""
    n_verses = max((len(n.lyrics) for n in claude_t.notes), default=0)
    out: list[list[list[Syllable]]] = [[] for _ in systems]
    ratio0 = exact0 = 0.0
    for v in range(n_verses):
        c_items = [n.lyrics[v] if v < len(n.lyrics) else "" for n in claude_t.notes if not n.is_rest]
        if not any(c_items):
            continue
        v_items = [(si, s.x, s.y, s.text, s.conf) for si, verses in enumerate(vision) if v < len(verses) for s in verses[v]]
        if not v_items:                             # Vision이 그 절을 못 봤으면 자리 정보가 없다 → 건너뜀
            continue
        # Claude가 빠뜨린 음절(회수율 ~90%)을 Vision이 읽었으면 남긴다. 한글·영어 글자만 (기호 잡음은 버림)
        placed, ratio, exact = place(v_items, c_items, _lyric_cost, systems,
                              keep_vision_only=lambda text, conf: bool(text) and (bool(_HANGUL.search(text)) or text.isalpha()))
        if v == 0:
            ratio0, exact0 = ratio, exact
        for row in out:
            while len(row) <= v:
                row.append([])
        for p in placed:
            out[p.system][v].append(Syllable(p.text, p.x, p.y, p.conf, p.source))
    for si in range(len(systems)):                 # Claude 절이 없거나 못 합친 절은 Vision 것을 둔다
        for v, sylls in enumerate(vision[si]):
            if v >= len(out[si]):
                out[si].append(list(sylls))
            elif not out[si][v]:
                out[si][v] = list(sylls)
    return out, ratio0, exact0
