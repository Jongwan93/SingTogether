"""하이브리드: 위치는 로컬 파이프라인, 글자는 Claude CLI.

로컬(Vision)은 코드 기호와 가사 음절의 *위치*는 정확하지만 글자를 20~40% 틀리고, Claude는 글자는 거의 다 맞지만
위치를 모른다. 두 열을 글자 일치를 앵커로 DP 정렬해서 Vision 자리에 Claude 글자를 심는다.
Claude에만 있는 항목은 앞뒤 앵커 사이에 고르게 끼워 넣고, 정렬이 잘 됐을 때는 Vision에만 있는 항목(잡음)을 버린다.
"""
from __future__ import annotations

import os
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
          gap_a: float = 0.6, gap_b: float = 0.6, keep_vision_only=lambda text, conf: True,
          claude_sys: list[int | None] | None = None, reconcile=None) -> tuple[list[Placed], float]:
    """vision: (오선, x, y, 글자, 확신도) 읽기 순서. claude: 글자 열. → Placed 목록과 앵커 비율.
    keep_vision_only: Claude에 짝이 없는 Vision 항목을 남길지 (글자, 확신도)로 정한다.
    claude_sys: Claude 항목이 든 오선 번호(알면). 다른 오선의 Vision 항목과는 짝을 비싸게 하고, 짝 없는 항목은 제 오선 안에 놓는다
    (오선 끝의 코드가 다음 오선 앞에 놓여 엉뚱한 글자를 덮던 일: 찬양의 열기·주 이름 찬양)."""
    csys = claude_sys or [None] * len(claude)
    pairs = align(list(enumerate(vision)), list(enumerate(claude)),
                  lambda v, c: cost(v[1][3], c[1]) + (0.8 if csys[c[0]] is not None and csys[c[0]] != v[1][0] else 0.0), gap_a, gap_b)
    matched = sum(1 for i, j in pairs if i is not None and j is not None)
    ratio = matched / max(1, len(claude))
    # 글자가 똑같이 읽힌 짝만 센 비율. 한글 음절끼리는 비슷하면 짝이 되므로(OCR 혼동 허용) 엉뚱한 곡도 matched는 꽤 나온다
    exact = sum(1 for i, j in pairs if i is not None and j is not None and cost(vision[i][3], claude[j]) == 0) / max(1, len(claude))
    out: list[Placed] = []
    pending: list[tuple[str, int | None]] = []     # 앵커 사이에 낀 Claude 전용 항목 (글자, 오선)
    last_anchor: Placed | None = None

    def spread(sys_i: int, x0: float, x1: float, y: float, items: list[str], conf: float):
        for k, t in enumerate(items, 1):
            out.append(Placed(sys_i, x0 + (x1 - x0) * k / (len(items) + 1), y, t, conf, "claude"))

    def flush(next_anchor: Placed | None):
        """pending을 last_anchor와 next_anchor 사이에 고르게 놓는다. 한쪽이 없으면 있는 쪽 옆에 붙인다.
        오선을 아는 항목은 그 오선 안에서만: 앵커가 다른 오선이면 제 오선의 빈 쪽(앞/뒤/전체)에 놓는다."""
        if not pending:
            return
        groups: dict[int | None, list[str]] = {}
        for t, sy in pending:
            groups.setdefault(sy, []).append(t)
        pending.clear()
        for sy, items in groups.items():
            la = last_anchor if last_anchor and (sy is None or last_anchor.system == sy) else None
            na = next_anchor if next_anchor and (sy is None or next_anchor.system == sy) else None
            if la and na and la.system == na.system:
                spread(la.system, la.x, na.x, la.y, items, 0.5)
            elif na is not None:                   # 오선 앞부분: 왼쪽 끝 ~ 앵커 사이
                st = systems[na.system].staff
                spread(na.system, st.left + 3 * st.interline, na.x, na.y, items, 0.4)
            elif la is not None:                   # 오선 뒷부분: 앵커 ~ 오른쪽 끝 사이
                st = systems[la.system].staff
                spread(la.system, la.x, st.right, la.y, items, 0.4)
            elif sy is not None:                   # 그 오선에 앵커가 하나도 없다: 오선 전체에 고르게
                s_ = systems[sy]
                st = s_.staff
                spread(sy, st.left + 3 * st.interline, st.right, (s_.chord_band[0] + s_.chord_band[1]) / 2, items, 0.3)

    for (i, _), (j, _) in ((a if a else (None, None), b if b else (None, None)) for a, b in
                           ((None if i is None else (i, vision[i]), None if j is None else (j, claude[j])) for i, j in pairs)):
        if i is not None and j is not None:
            si, x, y, _, conf = vision[i]
            anchor = Placed(si, x, y, reconcile(vision[i][3], claude[j]) if reconcile else claude[j], max(conf, 0.7), "both")
            flush(anchor)
            out.append(anchor)
            last_anchor = anchor
        elif j is not None:
            # Claude 전용 빈 항목(음절 없는 음표)은 자리를 모르니 끼워 넣지 않는다. 바로 앞 Claude 항목과 같은 코드도 넣지 않는다:
            # AI가 한 마디의 코드를 다음 마디에 또 적은 것이라(주 이름 찬양 G♯m7 ×2), 넣으면 Vision만 읽은 진짜 코드(B)를 덮는다
            if claude[j] and not (j > 0 and claude[j] == claude[j - 1]) and not (j + 1 < len(claude) and claude[j] == claude[j + 1]):
                pending.append((claude[j], csys[j]))
        else:
            si, x, y, text, conf = vision[i]
            if keep_vision_only(text, conf):
                out.append(Placed(si, x, y, text, conf, "vision"))
    flush(None)
    out.sort(key=lambda p: (p.system, p.x))
    return out, ratio, exact


# MARK: 코드
_CHORD_DIGITS = set("24579")          # 코드에 흔한 숫자. 6은 Vision이 ♭을 6으로 읽은 것(A♭→A6), 1·3은 괄호 번호·마디줄(E1·E13)
_PLAUSIBLE = re.compile(r"^[A-G][#b]?(m|m7|m9|maj7|M7|7|9|6|2|4|sus|sus2|sus4|7sus4|add9|add2|dim|dim7|m7b5|aug|\+|m6|69)?(/[A-G][#b]?)?$")


def _reconcile_chord(vision: str, claude: str) -> str:
    """짝지어진 Vision·AI 코드의 글자: 보통 AI(흐린 캡처에서 91–100%, Vision 48–62%). 다만 Vision이 읽은 숫자(겹쳐 읽은 G77은 G7)가
    AI 코드에 없으면 AI가 윗첨자를 빼먹거나 지어낸 것이다(주님 다시 오실때까지: G⁷을 G·Gsus4로): AI에 숫자가 없으면 Vision의 숫자를
    붙이고, 다른 숫자가 있으면 Vision 쪽이 그럴듯한 코드일 때 Vision을 쓴다."""
    v = re.sub(r"(\d)\1+", r"\1", normalize_chord(vision))
    rv, rc = re.match(r"[A-G][#b]?", v), re.match(r"[A-G][#b]?", claude)
    if not rv or not rc or rv.group(0) != rc.group(0):
        return claude
    # Vision이 읽은 슬래시 베이스(G/B)를 AI가 빼먹었으면 붙인다
    vb = re.search(r"/([A-G][#b]?)$", v)
    if vb and "/" not in claude and _PLAUSIBLE.match(claude + "/" + vb.group(1)):
        claude = claude + "/" + vb.group(1)
    vd = [d for d in re.findall(r"\d", v) if d in _CHORD_DIGITS]
    ad = re.findall(r"\d", claude)
    if not vd or all(d in ad for d in vd):
        return claude
    if not ad:
        base, _, bass = claude.partition("/")
        cand = base + vd[0] + ("/" + bass if bass else "")
        return cand if _PLAUSIBLE.match(cand) else claude
    # 2와 7은 Vision이 서로 바꿔 읽는다 (C²를 C7로, E⁷m을 E2로): 그 차이뿐이면 AI를 믿는다
    if all(d in ad or (d in "27" and any(a in "27" for a in ad)) for d in vd):
        return claude
    return v if _PLAUSIBLE.match(v) else claude


def _chord_cost(v: str, c: str) -> float:
    nv, nc = normalize_chord(v), normalize_chord(c)
    if nv == nc:
        return 0.0
    rv, rc = re.match(r"[A-G][#b]?", nv), re.match(r"[A-G][#b]?", nc)
    if rv and rc and rv.group(0) == rc.group(0):
        # 근음이 같으면 뒤가 비슷할수록 싸게: AI가 "Esus4 E"의 E를 빼먹었을 때 Esus4가 Vision의 "E"가 아니라 "Esus" 상자에 붙게
        common = len(os.path.commonprefix([nv, nc]))
        return 0.1 + 0.3 * (1 - common / max(len(nv), len(nc)))
    if rv and rc and rv.group(0)[0] == rc.group(0)[0]:   # Vision은 윗첨자 ♭을 6으로 읽는다 (A♭→A6): 글자만 같아도 짝이 된다
        return 0.7
    return 1.3


def trust(exact_ratio: float, n_ai: int, n_vision: int) -> bool:
    """AI 전사를 믿을지. 글자가 똑같이 읽힌 앵커가 너무 적으면 AI가 다른 곡을 읽었거나 지어낸 것이다.
    Vision이 거의 못 읽은 쪽이면 비교할 수 없으니 AI를 믿는다. 기준값은 cli의 실측 주석 참고."""
    if n_ai == 0:
        return False
    if n_vision < 3:
        return True
    if n_ai < 0.5 * n_vision:                 # 일부만 읽은 답(거절하며 몇 개만 적은 경우): 합치면 Vision 코드가 버려진다
        return False
    return exact_ratio >= TRUST_MIN


TRUST_MIN = 0.3


def merge_chords(vision: list[list[TextBox]], claude_t: Transcription, systems: list[System], il: float,
                 keep_vision_min: float = 0.75) -> tuple[list[list[TextBox]], float, float]:
    """Vision 코드 상자(오선별) + Claude 코드 열 → 오선별 TextBox (글자는 Claude). (결과, 앵커 비율, 똑같이 읽힌 비율)
    keep_vision_min: Claude에 짝이 없는 Vision 코드를 남길 최소 확신도. 잡음일 때가 많아 여러 번 읽어도 같았던 것(0.75+)만
    남기는 게 기본; 제자리 이조는 안 남기면 옛 코드가 그대로 남으니 0을 준다."""
    v_items = [(si, (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2, b.text, b.conf) for si, boxes in enumerate(vision) for b in boxes]
    c_items = [normalize_chord(n.chord) for m in claude_t.measures for n in m.notes if n.chord]     # AI도 B⁷m을 B7m으로 적는다 → Bm7
    c_sys = [m.system for m in claude_t.measures for n in m.notes if n.chord]
    if not c_items:
        return vision, 0.0, 0.0
    placed, ratio, exact = place(v_items, c_items, _chord_cost, systems, keep_vision_only=lambda text, conf: conf >= keep_vision_min,
                                 claude_sys=c_sys if any(x is not None for x in c_sys) else None, reconcile=_reconcile_chord)
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
