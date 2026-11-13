"""제자리 이조의 검수: 첫 결과를 원본과 대조해 틀린 곳을 찾고, 근거가 있는 곳은 고쳐 한 번 더 돌린다. 사용자가 결과 창에서 찍은
표시(코멘트는 AI가 표시로 바꾼다)도 여기서 이조기의 fixes로 바꾼다.

검사는 둘이다 (AI 없이).
  1. 안 움직인 음표: 이조하면 모든 머리가 움직이므로, 원본과 결과의 같은 자리에 같은 머리 모양이 그대로 남은 것은
     검출기가 놓친 머리다. 그 자리에 머리를 더해 다시 돌린다.
  2. 코드 다시 읽기: 결과 그림의 코드 띠를 Vision으로 다시 읽어, 써야 했던 이름과 근음이 다르면 적어 둔다.
AI가 오선 한 줄씩 대조하던 단계는 뺐다: 의심만 많고(한 곡에 7곳) 근거가 맞아 고친 것은 없었는데 1분이 걸렸다.

결과는 Issue 목록: 종류, 오선·마디(1부터), 원본 그림 좌표의 상자, 무엇이 보이고 무엇이어야 하는지, 고쳤는지.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from . import inplace, notes, text
from .inplace import MAJOR_BY_FIFTHS

LOG: list = []                    # 검사별 후보와 판정 (디버깅용)


@dataclass
class Issue:
    kind: str                     # "chord" / "note_not_moved" / "not_a_note" / "note_wrong" / "accidental" / "key" / "mark"
    staff: int                    # 1부터
    measure: int                  # 1부터, 0이면 모름
    box: tuple[int, int, int, int]   # 원본 그림 좌표
    found: str
    expected: str
    source: str                   # "check" (로컬 검사) / "Claude" 등 (AI)
    status: str = "flagged"       # "fixed" / "flagged"

    def as_json(self) -> dict:
        d = asdict(self)
        d["box"] = [int(v) for v in self.box]
        return d


# MARK: 좌표

def _measure_edges(state: dict, si: int) -> list[float]:
    st = state["systems"][si].staff
    return [float(state["zone"][si])] + [float(b) for b in state["bars"][si]] + [float(st.right)]


def _measure_of(state: dict, si: int, x: float) -> int:
    e = _measure_edges(state, si)
    for m in range(len(e) - 1):
        if e[m] <= x < e[m + 1]:
            return m + 1
    return 0


def _to_original(state: dict, box) -> tuple[int, int, int, int]:
    k = state["page"].scale
    return tuple(int(round(v / k)) for v in box)


def unmoved_heads(state: dict, loose: bool = False) -> list[tuple[int, float, float, bool]]:
    """(오선, x, y, 속빔): 원본과 결과 모두에 같은 자리 같은 모양으로 남은 머리. 오선을 뺀 '둘 다 검은' 잉크에서 머리 검출을 돌린다.
    loose: 사용자가 '안 움직였다'고 고른 곳에서만 쓴다 — 기둥이 곧지 않아도(흐린 기둥) 머리로 본다."""
    page, il = state["page"], state["il"]
    orig = page.binary < 128
    res = state["result"] < 128
    if res.shape != orig.shape:
        return []
    same = np.full(page.binary.shape, 255, np.uint8)
    same[orig & res] = 0
    out = []
    for si, (sys_, plans) in enumerate(state["plans_all"]):
        st = sys_.staff
        t_line = inplace.line_thickness(orig, st)
        # 박자표(C의 안쪽, 4의 세모)를 빼려고 첫 머리 1il 앞부터. 꼬리 고리는 머리 목록을 넘겨 flag_hook으로 거른다
        heads = [p.head for p in plans]
        min_x = max(state["zone"][si] + 0.3 * il, (min(h.x for h in heads) - 1.0 * il) if heads else 0)
        moved = [(p.head.x, p.head.y) for p in plans] + [(p.head.x, st.bottom - p.step_new * il / 2) for p in plans]
        # AI 확인이 필요한 구멍 후보(칸 안 구멍, 기둥 없는 구멍: need_dur)는 뺀다 — 제자리 이조에서도 AI가 확인한 것만 쓰고, 여기서는
        # 기둥 사이 칸·마디줄 옆 칸이 '안 움직인 머리'로 잡혀 고친 것처럼 보고됐다 (이 세상의 부요함보다: 7개)
        found = [h for h in inplace.find_hole_heads(same, sys_, il, heads, min_x, t_line)
                 if not getattr(h, "need_dur", 0) and not any(abs(h.x - o.x) < 0.7 * il and abs(h.y - o.y) < 0.7 * il for o in heads)]
        found += [h for h in inplace.find_whole_notes(same, sys_, il, heads + found, min_x)]
        # 꽉 찬 머리: 오선 줄을 뺀 '둘 다 검은' 잉크를 머리 크기 타원으로 열어 남는 것, 기둥이 있어야
        y0, y1 = max(0, int(st.top - 1.6 * il)), min(page.binary.shape[0], int(st.bottom + 1.6 * il))
        band = (same[y0:y1, st.left:st.right + 1] < 128).astype(np.uint8)
        cut = inplace.strip_staff_lines(band, [y - y0 for y in st.lines], t_line)
        ek = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (max(3, int(0.8 * il)) | 1, max(3, int(0.6 * il)) | 1))
        opened = cv2.morphologyEx(cut, cv2.MORPH_OPEN, ek)
        n, lab_, stt, cen = cv2.connectedComponentsWithStats(opened, connectivity=8)
        for i in range(1, n):
            bx, by, bw, bh, area = stt[i]
            # 빔 토막은 직사각형(채움 0.9 넘음)이고 높이가 0.75il 아래다; 머리는 타원
            if not (0.8 * il <= bw <= 1.6 * il and 0.75 * il <= bh <= 1.3 * il and area / (bw * bh) <= 0.88):
                continue
            hx, hy = cen[i][0] + st.left, cen[i][1] + y0
            if hx < min_x or not (loose or _straight_stem(orig, hx, hy, il)):
                continue
            # 16분음표의 짧은 둘째 빔 토막: 직사각형이라 폭을 다 채우는 줄이 높이의 30% 넘는다. 타원 머리는 가운데 몇 줄뿐
            comp = (lab_[by:by + bh, bx:bx + bw] == i)
            full_rows = int(((comp.sum(axis=1)) >= 0.92 * bw).sum())   # 폭을 거의 다 채우는 줄: 직사각형(빔)은 많고 타원(머리)은 적다
            if full_rows >= 0.3 * bh:
                LOG.append(("unmoved-beam", si, round(hx), round(hy)))
                continue
            h = notes.Notehead(hx, hy, int(bw), int(bh))
            # 8분음표 꼬리 끝·빔 토막: 제자리 이조가 같은 기준으로 머리에서 뺀 것이다. 다시 넣으면 오선 위에 큰 머리가 생긴다 (주 이름 찬양)
            if inplace.column_fill(orig, h) < 0.78:
                LOG.append(("unmoved-flag", si, round(hx), round(hy)))
                continue
            h.evidence = notes.duration_evidence(page.binary, h, il)
            h.evidence.hollow = False
            found.append(h)
        dropped = [(x, y) for s_, x, y in state.get("dropped", []) if s_ == si]
        for h in found:
            if any(abs(h.x - mx) < 0.7 * il and abs(h.y - my) < 0.7 * il for mx, my in moved):
                LOG.append(("unmoved-skip", si, round(h.x), round(h.y)))
                continue
            # 제자리 이조가 일부러 뺀 머리(쉼표, AI가 아니라고 한 고리): 다시 넣으면 같은 규칙에 또 빠지는데 '고쳤다'고만 보고된다
            if any(abs(h.x - dx) < 0.8 * il and abs(h.y - dy) < 0.8 * il for dx, dy in dropped):
                LOG.append(("unmoved-dropped", si, round(h.x), round(h.y)))
                continue
            if any(o[0] == si and abs(o[1] - h.x) < 0.6 * il and abs(o[2] - h.y) < 0.6 * il for o in out):
                continue
            hollow = bool(h.evidence and h.evidence.hollow)
            LOG.append(("unmoved", si, round(h.x), round(h.y), hollow))
            out.append((si, float(h.x), float(h.y), hollow))
    return out


def _straight_stem(ink: np.ndarray, x: float, y: float, il: float, reach: float = 0.8) -> bool:
    """x 둘레(±reach il)의 한 열에서 y 높이부터 2.2il 넘게 곧게 이어지는 세로선. 4분쉼표의 지그재그는 한 열에서 짧다."""
    H, W = ink.shape
    for cx in range(max(0, int(x - reach * il)), min(W, int(x + reach * il) + 1)):
        for d in (-1, 1):
            n, yy = 0, int(y)
            while 0 <= yy < H and (ink[yy, cx] or (0 <= yy + d < H and ink[yy + d, cx])) and n < 5 * il:
                n += 1
                yy += d
            if n >= 2.2 * il:
                return True
    return False


# MARK: 2. 코드 다시 읽기

def _root(name: str) -> str | None:
    m = re.match(r"([A-G])([#b]?)", text.normalize_chord(name or ""))
    return m.group(0) if m else None


def reread_chords(state: dict) -> tuple[list[tuple], list[tuple]]:
    """(틀려 보이는 코드 [(오선, 상자, 보이는 글자, 써야 할 글자)], 안 바뀐 옛 코드 [(오선, 상자, 옛 글자)])."""
    import dataclasses

    from . import ingest
    page, il = state["page"], state["il"]
    res = state["result"]
    if res.shape != page.gray.shape:
        return [], []
    rpage = dataclasses.replace(page, gray=res, binary=ingest.binarize(res))
    try:
        rows = text.read_chords(rpage, state["systems"])
    except Exception as e:                                     # Vision이 없으면 이 검사를 건너뛴다
        LOG.append(("chords-skip", str(e)[:80]))
        return [], []
    drawn = state.get("drawn_chords", [])
    wrong, stale = [], []
    for si, row in enumerate(rows):
        mine = [d for d in drawn if d[0] == si]
        for v in row:
            hit = [d for d in mine if min(d[3], v.x1) - max(d[1], v.x0) > 0.3 * min(d[3] - d[1], v.x1 - v.x0)]
            vr = _root(v.text)
            if not vr:
                continue
            if hit:
                d = hit[0]
                # 근음 글자만 본다: Vision은 ♯·♭을 자주 놓치고(F#m7을 F1로), 슬래시 코드를 둘로 갈라 베이스만 읽기도 한다(E7/B의 B)
                wants = {(_root(part) or "")[:1] for part in d[6].split("/") if part}
                if wants and vr[:1] not in wants:
                    LOG.append(("chord-wrong", si, v.text, d[6]))
                    wrong.append((si, (d[1], d[2], d[3], d[4]), v.text, d[6]))
            # 그린 코드와 안 겹치는 Vision 글자는 판단하지 않는다: 새 코드는 옛 상자보다 넓게 그려져(E/B → F#/C#) 넘친 부분을
            # Vision이 따로 읽는다. 진짜 안 바뀐 코드는 AI 대조가 잡는다
    return wrong, stale


# MARK: 3. AI 대조

def pair_image(state: dict, si: int, orig: np.ndarray, res: np.ndarray, box=None) -> np.ndarray:
    """오선 한 줄: 위 원본, 아래 결과. 사용자가 보는 **원본 크기의 결과 파일**에서 잘라 키운다 (폭 2000px 안으로).
    작업 크기(흐린 캡처를 4–5배 키운 것)를 보내면 원본의 흐린 기둥은 옅고 새로 그린 머리·조표만 진해서, AI가 '기둥이 지워졌다',
    '조표가 뭉쳤다'고 잘못 본다. box(원본 그림 좌표)를 주면 두 줄 모두 그 자리에 빨간 네모를 그린다 (컬러로 낸다)."""
    page, il = state["page"], state["il"]
    k = page.scale
    sys_ = state["systems"][si]
    y0 = max(0, int((sys_.chord_band[0] - 0.3 * il) / k))
    y1 = min(orig.shape[0], int((sys_.staff.bottom + 1.8 * il) / k))
    a, b = orig[y0:y1], res[y0:y1]
    f = min(3.0, 2000 / a.shape[1])
    a = cv2.resize(a, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC if f > 1 else cv2.INTER_AREA)
    b = cv2.resize(b, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC if f > 1 else cv2.INTER_AREA)
    white, black, sep = 255, 0, np.zeros((12, a.shape[1]), np.uint8)
    if box is not None:
        a, b = cv2.cvtColor(a, cv2.COLOR_GRAY2BGR), cv2.cvtColor(b, cv2.COLOR_GRAY2BGR)
        r = (int(box[0] * f), int((box[1] - y0) * f)), (int(box[2] * f), int((box[3] - y0) * f))
        for img in (a, b):
            cv2.rectangle(img, *r, (0, 0, 255), 3)
        white, black, sep = (255, 255, 255), (0, 0, 0), np.zeros((12, a.shape[1], 3), np.uint8)

    def lab(img, t):
        img = cv2.copyMakeBorder(img, 40, 0, 0, 0, cv2.BORDER_CONSTANT, value=white)
        return cv2.putText(img, t, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, black, 2)
    return np.vstack([lab(a, "ORIGINAL"), sep, lab(b, "TRANSPOSED")])


# MARK: 수정 정하기

def check(state: dict) -> tuple[list[Issue], dict]:
    """첫 결과를 검사해 (문제 목록, 다시 돌릴 때 줄 fixes). AI 없이 그림만 본다 (AI 줄 대조는 의심만 많고 고치는 것이 없어 뺐다)."""
    LOG.clear()
    il = state["il"]
    issues: list[Issue] = []
    fixes: dict = {"add_heads": [], "drop_heads": [], "chords": [], "add_chords": []}

    # 1. 안 움직인 음표 → 머리를 더해 다시 돌린다
    for si, x, y, hollow in unmoved_heads(state):
        fixes["add_heads"].append((si, x, y, hollow))
        b = (x - 0.7 * il, y - 0.6 * il, x + 0.7 * il, y + 0.6 * il)
        issues.append(Issue("note_not_moved", si + 1, _measure_of(state, si, x), _to_original(state, b),
                            "a note left where it was", "the note moved like the others", "check", "fixed"))

    # 2. 코드 다시 읽기. AI가 코드 이름을 준 경로에서는 안 한다: 이름은 AI 것이고 글자는 우리가 그렸으니, Vision이 그 글자를
    #    다시 읽어 대는 것은 Vision의 잘못 읽기뿐이다 (13곡에서 15건 모두 맞게 그린 코드: F#m을 E로, Cm7을 G7로)
    wrong, stale = reread_chords(state) if state.get("ai") is None else ([], [])
    for si, box, seen, want in wrong:
        issues.append(Issue("chord", si + 1, _measure_of(state, si, box[0]), _to_original(state, box), seen, want, "check"))
    for si, box, old in stale:
        fixes["add_chords"].append((si, *[int(v) for v in box], old))
        issues.append(Issue("chord", si + 1, _measure_of(state, si, box[0]), _to_original(state, box),
                            f"{old} (not transposed)", "transposed", "check", "fixed"))

    return issues, fixes


COMMENT_PROMPT = """The image {image} has two strips. The top strip (ORIGINAL) is one line of a lead sheet. The bottom strip (TRANSPOSED) is the same line after a program moved every note, the key signature and every chord symbol __DIR__ __SEMI__ semitones, from __OLD__ major to __NEW__ major, directly on the picture. The red box marks the same place on both strips.

The user looked at the red box on the TRANSPOSED strip and wrote: "__TEXT__"

Turn what the user wants into one of these corrections of what is inside the red box. Write exactly one line, in one of these forms, and nothing else:
CHORD <name>: the chord symbol in the box should read <name> (in the new key, for example F#m7 or A/C#)
UP <n>: the note heads in the box should be <n> letter names higher than they are now on the TRANSPOSED strip
DOWN <n>: the note heads in the box should be <n> letter names lower than they are now on the TRANSPOSED strip
NOT_MOVED: note heads in the box were left where they are on the ORIGINAL and must be moved like the others
NOT_A_NOTE: something in the box was moved as a note head but is a rest or another symbol
NONE: none of these does what the user asked"""


def _mark_box(m: dict, k: float, il: float) -> tuple[float, float, float, float]:
    """표시 → 작업 크기 상자. 네모로 고른 표시는 그 네모, 점은 그 둘레 머리 하나 크기."""
    if m.get("box") and len(m["box"]) == 4:
        x0, y0, x1, y1 = (float(v) * k for v in m["box"])
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
    x, y = float(m.get("x", 0)) * k, float(m.get("y", 0)) * k
    return x - 0.8 * il, y - 0.8 * il, x + 0.8 * il, y + 0.8 * il


def read_comment(state: dict, rep, provider: str, work: Path, out: Path, m: dict) -> dict | None:
    """사용자 코멘트 하나를 AI에게 보여 주고(빨간 네모를 친 원본·결과 한 줄) 표시 하나로 바꾼다. 모르겠으면 None."""
    from . import llm
    page, il = state["page"], state["il"]
    k = page.scale
    bx = _mark_box(m, k, il)
    cy = (bx[1] + bx[3]) / 2
    si = min(range(len(state["systems"])), key=lambda i: abs((state["systems"][i].staff.top + state["systems"][i].staff.bottom) / 2 - cy))
    orig = cv2.imdecode(np.fromfile(str(state["src"]), np.uint8), cv2.IMREAD_GRAYSCALE)
    res = cv2.imdecode(np.fromfile(str(out), np.uint8), cv2.IMREAD_GRAYSCALE)
    if orig is None or res is None or orig.shape != res.shape:
        return None
    img = pair_image(state, si, orig, res, [v / k for v in bx])
    semi = state["semitones"]
    said = str(m.get("text", "")).replace('"', "'").replace("{", "(").replace("}", ")")[:400]
    prompt = (COMMENT_PROMPT.replace("__DIR__", "up" if semi > 0 else "down").replace("__SEMI__", str(abs(semi)))
              .replace("__OLD__", MAJOR_BY_FIFTHS[rep.old_fifths + 6]).replace("__NEW__", MAJOR_BY_FIFTHS[rep.new_fifths + 6])
              .replace("__TEXT__", said))
    out_dir = work / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", img)
    key = hashlib.sha1(buf.tobytes() + prompt.encode()).hexdigest()[:16]
    png, ans = out_dir / f"c-{key}.png", out_dir / f"c-{key}.txt"
    if ans.exists():
        answer = ans.read_text()
    else:
        buf.tofile(str(png))
        try:
            answer = llm.ask(provider, prompt, png, timeout=300)
        except Exception as e:
            LOG.append(("comment-fail", str(e)[:120]))
            return None
        ans.write_text(answer)
    LOG.append(("comment", said, answer.strip()[:120]))
    place = {key_: m[key_] for key_ in ("x", "y", "box") if key_ in m}
    for line in answer.splitlines():
        line = line.strip().strip("`*").strip()
        if mm := re.match(r"CHORD\s*:?\s*(\S+)", line, re.I):
            return {"kind": "chord", "text": mm.group(1).strip(".,"), **place}
        if mm := re.match(r"(UP|DOWN)\s*:?\s*(\d+)", line, re.I):
            n = int(mm.group(2)) * (1 if mm.group(1).upper() == "UP" else -1)
            return {"kind": "shift", "steps": n, **place} if n else None
        if re.match(r"NOT_MOVED", line, re.I):
            return {"kind": "not_moved", **place}
        if re.match(r"NOT_A_NOTE", line, re.I):
            return {"kind": "not_a_note", **place}
        if re.match(r"NONE", line, re.I):
            return None
    return None


def user_fixes(src: Path, marks: list[dict]) -> dict:
    """결과 창에서 사용자가 찍은 표시(원본 그림 픽셀 좌표) → 이조기의 fixes (작업 크기 좌표, 오선 번호).
    표시: {"kind": "chord", "x", "y", "text"} / {"kind": "not_moved", "x", "y"} / {"kind": "not_a_note", "x", "y"} /
          {"kind": "shift", "x", "y", "steps": ±1}. 점은 그 기호 위를 찍은 자리다. "box": [x0, y0, x1, y1]이 있으면 네모로 고른
          곳이라 안의 것을 모두 고친다 (x, y는 그 가운데). 네모 안의 '안 움직였다'와 코멘트는 첫 판 결과가 있어야 해서 run이 다룬다."""
    from . import ingest, layout
    page = ingest.prepare(src)[0]
    systems = layout.find_systems(page)
    il, k = page.interline, page.scale
    fixes: dict = {"add_heads": [], "drop_heads": [], "shift_heads": [], "drop_box": [], "shift_box": [], "chords": [], "add_chords": []}

    def staff_of(y: float) -> int | None:
        best = None
        for si, s_ in enumerate(systems):
            top, bot = s_.chord_band[0], s_.staff.bottom + 2.5 * il
            if top <= y <= bot:
                return si
            d = min(abs(y - top), abs(y - bot))
            best = (si, d) if best is None or d < best[1] else best
        return best[0] if best and best[1] < 3 * il else None

    for m in marks:
        x, y = float(m.get("x", 0)) * k, float(m.get("y", 0)) * k
        si = staff_of(y)
        if si is None:
            continue
        kind, boxed = m.get("kind"), bool(m.get("box"))
        bx = _mark_box(m, k, il)
        if kind == "not_moved" and not boxed:
            fixes["add_heads"].append((si, x, y, None))
        elif kind == "not_a_note":
            fixes["drop_box" if boxed else "drop_heads"].append((si, *bx) if boxed else (si, x, y))
        elif kind == "shift" and int(m.get("steps", 0)):
            if boxed:
                fixes.setdefault("shift_box", []).append((si, *bx, int(m["steps"])))
            else:
                fixes["shift_heads"].append((si, x, y, int(m["steps"])))
        elif kind == "chord" and m.get("text"):
            new = text.normalize_chord(str(m["text"]))
            fixes["chords"].append((si, x, y, new))
            # 그 자리에 코드 상자가 없을 때(옛 코드가 안 바뀐 채 남은 경우)를 위해 글자 덩어리로 상자를 만들어 둔다
            y0, y1 = systems[si].chord_band
            blobs = [(bx0, y0 + by0, bx1, y0 + by1) for bx0, by0, bx1, by1 in text.find_text_blobs(page.binary[y0:y1], il)]
            if boxed:
                hit = [b for b in blobs if min(b[2], bx[2]) > max(b[0], bx[0]) and min(b[3], bx[3]) > max(b[1], bx[1])]
            else:
                hit = [b for b in blobs if b[0] - 0.3 * il <= x <= b[2] + 0.3 * il and b[1] - 0.5 * il <= y <= b[3] + 0.5 * il]
            if hit:
                bx0, by0 = min(b[0] for b in hit), min(b[1] for b in hit)
                bx1, by1 = max(b[2] for b in hit), max(b[3] for b in hit)
                fixes["add_chords"].append((si, int(bx0), int(by0), int(bx1), int(by1), new, new))
    return fixes


def _merge(a: dict, b: dict) -> dict:
    return {k: list(a.get(k, [])) + list(b.get(k, [])) for k in set(a) | set(b)}


def run(src: Path, semitones: int, out: Path, llm_name: str = "none", debug_dir: Path | None = None, work: Path | None = None,
        marks: list[dict] | None = None, check_result: bool = True):
    """(사용자 표시대로 고쳐) 이조 → 검수 → (고칠 것이 있으면) 고쳐서 한 번 더. (마지막 Report, 문제 목록)."""
    from . import llm
    work = work or out.parent / ".singtogether-work"
    marks = marks or []
    later = [m for m in marks if m.get("kind") == "comment" or (m.get("kind") == "not_moved" and m.get("box"))]
    given = user_fixes(src, [m for m in marks if m not in later]) if marks else {}
    rep = inplace.transpose_in_place(src, semitones, out, llm_name=llm_name, debug_dir=debug_dir, work=work, fixes=given or None)
    provider = llm.pick(llm_name) if llm_name != "none" else "none"
    if rep.state.get("ai") is None:                       # 첫 판에서 AI가 안 됐으면(한도·로그인) 검수·코멘트도 AI 없이
        provider = "none"
    issues: list[Issue] = []
    if later:
        # 첫 판 결과가 있어야 하는 표시: 네모 안의 안 움직인 머리(느슨하게 찾는다), 코멘트(AI가 표시로 바꾼다)
        state, il, k = rep.state, rep.state["il"], rep.state["page"].scale
        extra: dict = {"add_heads": []}
        loose = None
        for m in later:
            bx = _mark_box(m, k, il)
            si_ = min(range(len(state["systems"])), key=lambda i: abs((state["systems"][i].staff.top + state["systems"][i].staff.bottom) / 2 - (bx[1] + bx[3]) / 2))
            where = _to_original(state, bx)
            if m.get("kind") == "not_moved":
                loose = unmoved_heads(state, loose=True) if loose is None else loose
                hit = [h for h in loose if bx[0] <= h[1] <= bx[2] and bx[1] <= h[2] <= bx[3]]
                extra["add_heads"] += hit
                issues.append(Issue("note_not_moved", si_ + 1, _measure_of(state, si_, bx[0]), where, "marked as not moved",
                                    f"{len(hit)} note{'s' if len(hit) != 1 else ''} found and moved", "you", "fixed" if hit else "flagged"))
                continue
            said = str(m.get("text", ""))
            if provider == "none":
                issues.append(Issue("comment", si_ + 1, _measure_of(state, si_, bx[0]), where, said, "comments need an AI reader", "you"))
                continue
            got = read_comment(state, rep, provider, work, out, m)
            if got and got.get("kind") == "not_moved" and got.get("box"):
                loose = unmoved_heads(state, loose=True) if loose is None else loose
                extra["add_heads"] += [h for h in loose if bx[0] <= h[1] <= bx[2] and bx[1] <= h[2] <= bx[3]]
            elif got:
                extra = _merge(extra, user_fixes(src, [got]))
            what = ("not understood" if not got else f"chord {got['text']}" if got["kind"] == "chord"
                    else f"note {'up' if got['steps'] > 0 else 'down'} {abs(got['steps'])}" if got["kind"] == "shift"
                    else got["kind"].replace("_", " "))
            issues.append(Issue("comment", si_ + 1, _measure_of(state, si_, bx[0]), where, said, what,
                                llm.LABELS.get(provider, provider), "fixed" if got else "flagged"))
        if any(extra.values()):
            given = _merge(given, extra)
            rep = inplace.transpose_in_place(src, semitones, out, llm_name=llm_name, debug_dir=debug_dir, work=work, fixes=given)
    if not check_result or (not rep.steps and semitones % 12 == 0):
        return rep, issues
    found, fixes = check(rep.state)
    issues += found
    if any(fixes.values()):
        rep = inplace.transpose_in_place(src, semitones, out, llm_name=llm_name, debug_dir=debug_dir, work=work,
                                         fixes=_merge(given, fixes))
    return rep, issues
