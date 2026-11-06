"""제자리 이조의 검수: 첫 결과를 원본과 대조해 틀린 곳을 찾고, 근거가 둘 맞는 곳은 고쳐 한 번 더 돌리고, 나머지는 표시로 남긴다.

검사는 셋이다.
  1. 안 움직인 음표 (AI 없이): 이조하면 모든 머리가 움직이므로, 원본과 결과의 같은 자리에 같은 머리 모양이 그대로 남은 것은
     검출기가 놓친 머리다. 그 자리에 머리를 더해 다시 돌린다.
  2. 코드 다시 읽기 (AI 없이): 결과 그림의 코드 띠를 Vision으로 다시 읽어, 써야 했던 이름과 근음이 다르면 표시한다. 옛 코드가
     그대로 읽히는데 그 자리에 그린 코드가 없으면 그 코드를 더해 다시 돌린다.
  3. AI 대조 (AI가 있을 때): 원본과 결과를 오선 한 줄씩 위아래로 붙여 틀린 곳을 적게 한다. AI만의 판단으로는 고치지 않고, 그 마디에
     로컬 근거가 있을 때만 고친다 (쉼표를 음표로 옮긴 곳: 그 마디의 기둥 없는 머리가 하나뿐일 때 뺀다). 나머지는 표시로 남긴다.

결과는 Issue 목록: 종류, 오선·마디(1부터), 원본 그림 좌표의 상자, 무엇이 보이고 무엇이어야 하는지, 고쳤는지.
"""
from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
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


def _measure_box(state: dict, si: int, m: int, chord: bool = False) -> tuple[int, int, int, int]:
    sys_ = state["systems"][si]
    st, il = sys_.staff, state["il"]
    e = _measure_edges(state, si)
    x0, x1 = (e[m - 1], e[m]) if 1 <= m < len(e) else (e[0], e[-1])
    y0 = sys_.chord_band[0] if chord else st.top - 1.5 * il
    y1 = st.top if chord else st.bottom + 1.5 * il
    return _to_original(state, (x0, y0, x1, y1))


# MARK: 1. 안 움직인 음표

def unmoved_heads(state: dict) -> list[tuple[int, float, float, bool]]:
    """(오선, x, y, 속빔): 원본과 결과 모두에 같은 자리 같은 모양으로 남은 머리. 오선을 뺀 '둘 다 검은' 잉크에서 머리 검출을 돌린다."""
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
        found = [h for h in inplace.find_hole_heads(same, sys_, il, heads, min_x, t_line)
                 if not any(abs(h.x - o.x) < 0.7 * il and abs(h.y - o.y) < 0.7 * il for o in heads)]
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
            if hx < min_x or not _straight_stem(orig, hx, hy, il):
                continue
            # 16분음표의 짧은 둘째 빔 토막: 직사각형이라 폭을 다 채우는 줄이 높이의 30% 넘는다. 타원 머리는 가운데 몇 줄뿐
            comp = (lab_[by:by + bh, bx:bx + bw] == i)
            full_rows = int(((comp.sum(axis=1)) >= 0.92 * bw).sum())   # 폭을 거의 다 채우는 줄: 직사각형(빔)은 많고 타원(머리)은 적다
            if full_rows >= 0.3 * bh:
                LOG.append(("unmoved-beam", si, round(hx), round(hy)))
                continue
            h = notes.Notehead(hx, hy, int(bw), int(bh))
            h.evidence = notes.duration_evidence(page.binary, h, il)
            h.evidence.hollow = False
            found.append(h)
        for h in found:
            if any(abs(h.x - mx) < 0.7 * il and abs(h.y - my) < 0.7 * il for mx, my in moved):
                LOG.append(("unmoved-skip", si, round(h.x), round(h.y)))
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

PROMPT = """The image {image} has two strips. The top strip (ORIGINAL) is one line of a lead sheet. The bottom strip (TRANSPOSED) is the same line after a program moved every note, the key signature and every chord symbol __DIR__ __SEMI__ semitones, from __OLD__ major to __NEW__ major, directly on the picture. Every note head must move by exactly __STEPS__ letter names __DIR__, with sharps or flats as the new key requires. Everything else (lyrics, rests, barlines, ties, slurs, text) should be unchanged apart from slurs and ties moving with their notes.

Check the bottom strip against the top strip and list every mistake the program made. Kinds of mistakes:
- CHORD: a chord symbol that is not the original chord moved __DIR__ __SEMI__ semitones (still the old chord, a wrong letter or quality), or is garbled, doubled or overlapping something.
- NOTE_NOT_MOVED: a note head that is still at its original position.
- NOTE_WRONG: a note head moved to the wrong line or space.
- NOT_A_NOTE: a rest or other symbol that was turned into a note head, or a note head where the original has none.
- ACCIDENTAL: a sharp, flat or natural that should not be there, or one that is missing.
- KEY: the key signature is wrong or has leftover marks.
- MARK: leftover pieces, smudges or erased parts of other symbols.

Count measures from the left of this line, starting at 1. Write one line per mistake, exactly in this form, and nothing else:
<measure> | <kind> | <what the bottom strip shows> | <what it should be>
If there are no mistakes, write exactly: NONE"""

KINDS = {"CHORD": "chord", "NOTE_NOT_MOVED": "note_not_moved", "NOTE_WRONG": "note_wrong", "NOT_A_NOTE": "not_a_note",
         "ACCIDENTAL": "accidental", "KEY": "key", "MARK": "mark"}


def pair_image(state: dict, si: int, orig: np.ndarray, res: np.ndarray) -> np.ndarray:
    """오선 한 줄: 위 원본, 아래 결과. 사용자가 보는 **원본 크기의 결과 파일**에서 잘라 키운다 (폭 2000px 안으로).
    작업 크기(흐린 캡처를 4–5배 키운 것)를 보내면 원본의 흐린 기둥은 옅고 새로 그린 머리·조표만 진해서, AI가 '기둥이 지워졌다',
    '조표가 뭉쳤다'고 잘못 본다."""
    page, il = state["page"], state["il"]
    k = page.scale
    sys_ = state["systems"][si]
    y0 = max(0, int((sys_.chord_band[0] - 0.3 * il) / k))
    y1 = min(orig.shape[0], int((sys_.staff.bottom + 1.8 * il) / k))
    a, b = orig[y0:y1], res[y0:y1]
    f = min(3.0, 2000 / a.shape[1])
    a = cv2.resize(a, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC if f > 1 else cv2.INTER_AREA)
    b = cv2.resize(b, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC if f > 1 else cv2.INTER_AREA)

    def lab(img, t):
        img = cv2.copyMakeBorder(img, 40, 0, 0, 0, cv2.BORDER_CONSTANT, value=255)
        return cv2.putText(img, t, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, 0, 2)
    return np.vstack([lab(a, "ORIGINAL"), np.zeros((12, a.shape[1]), np.uint8), lab(b, "TRANSPOSED")])


def ai_review(state: dict, provider: str, work: Path, old_fifths: int, new_fifths: int, steps: int, out: Path) -> list[tuple]:
    """[(오선, 마디, 종류, 보이는 것, 있어야 할 것)]. 같은 그림이면 답을 캐시에서 쓴다."""
    from . import llm
    semi = state["semitones"]
    prompt = (PROMPT.replace("__DIR__", "up" if semi > 0 else "down").replace("__SEMI__", str(abs(semi)))
              .replace("__OLD__", MAJOR_BY_FIFTHS[old_fifths + 6]).replace("__NEW__", MAJOR_BY_FIFTHS[new_fifths + 6])
              .replace("__STEPS__", str(abs(steps))))
    out_dir = work / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)

    orig = cv2.imdecode(np.fromfile(str(state["src"]), np.uint8), cv2.IMREAD_GRAYSCALE)
    res = cv2.imdecode(np.fromfile(str(out), np.uint8), cv2.IMREAD_GRAYSCALE)
    if orig is None or res is None or orig.shape != res.shape:
        LOG.append(("ai-skip", "images differ in size"))
        return []

    def ask(si: int) -> tuple[int, str]:
        img = pair_image(state, si, orig, res)
        ok, buf = cv2.imencode(".png", img)
        key = hashlib.sha1(buf.tobytes() + prompt.encode()).hexdigest()[:16]
        png, ans = out_dir / f"{key}.png", out_dir / f"{key}.txt"
        if ans.exists():
            return si, ans.read_text()
        buf.tofile(str(png))
        try:
            text_ = llm.ask(provider, prompt, png, timeout=600)
        except Exception as e:                                 # 한 줄이 실패해도 나머지는 쓴다
            LOG.append(("ai-fail", si, str(e)[:120]))
            return si, ""
        ans.write_text(text_)
        return si, text_

    with ThreadPoolExecutor(max_workers=4) as pool:
        answers = list(pool.map(ask, range(len(state["systems"]))))
    found = []
    for si, ans in answers:
        for line in ans.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 4 or parts[1].upper() not in KINDS:
                continue
            m = int(re.match(r"\d+", parts[0]).group(0)) if re.match(r"\d+", parts[0]) else 0
            found.append((si, m, KINDS[parts[1].upper()], parts[2], parts[3]))
    return found


# MARK: 수정 정하기

def check(state: dict, rep, provider: str = "none", work: Path | None = None, out: Path | None = None) -> tuple[list[Issue], dict]:
    """첫 결과를 검사해 (문제 목록, 다시 돌릴 때 줄 fixes)."""
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

    # 2. 코드 다시 읽기
    wrong, stale = reread_chords(state)
    for si, box, seen, want in wrong:
        issues.append(Issue("chord", si + 1, _measure_of(state, si, box[0]), _to_original(state, box), seen, want, "check"))
    for si, box, old in stale:
        fixes["add_chords"].append((si, *[int(v) for v in box], old))
        issues.append(Issue("chord", si + 1, _measure_of(state, si, box[0]), _to_original(state, box),
                            f"{old} (not transposed)", "transposed", "check", "fixed"))

    # 3. AI 대조
    if provider != "none" and work is not None and out is not None:
        from . import llm
        label = llm.LABELS.get(provider, provider)
        for si, m, kind, seen, want in ai_review(state, provider, work, rep.old_fifths, rep.new_fifths, rep.steps, out):
            status = "flagged"
            if kind == "not_a_note" and m:
                e = _measure_edges(state, si)
                if m < len(e):
                    sys_, plans = state["plans_all"][si]
                    loose = [p.head for p in plans if e[m - 1] <= p.head.x < e[m]
                             and not (p.head.evidence and (p.head.evidence.stem or p.head.evidence.hollow))]
                    if len(loose) == 1:                     # AI + 기둥 없는 머리 하나: 둘 다 맞으면 뺀다
                        fixes["drop_heads"].append((si, loose[0].x, loose[0].y))
                        status = "fixed"
            if kind == "note_not_moved" and any(i.kind == "note_not_moved" and i.staff == si + 1 and i.measure == m for i in issues):
                continue                                    # 로컬 검사가 이미 찾아 고쳤다
            issues.append(Issue(kind, si + 1, m, _measure_box(state, si, m, chord=kind == "chord"), seen, want, label, status))
    return issues, fixes


def run(src: Path, semitones: int, out: Path, llm_name: str = "none", debug_dir: Path | None = None, work: Path | None = None):
    """이조 → 검수 → (고칠 것이 있으면) 고쳐서 한 번 더. (마지막 Report, 문제 목록)."""
    from . import llm
    work = work or out.parent / ".singtogether-work"
    rep = inplace.transpose_in_place(src, semitones, out, llm_name=llm_name, debug_dir=debug_dir, work=work)
    if not rep.steps and semitones % 12 == 0:
        return rep, []
    provider = llm.pick(llm_name) if llm_name != "none" else "none"
    if rep.state.get("ai") is None:                       # 첫 판에서 AI가 안 됐으면(한도·로그인) 검수도 AI 없이
        provider = "none"
    issues, fixes = check(rep.state, rep, provider, work, out)
    if any(fixes.values()):
        rep = inplace.transpose_in_place(src, semitones, out, llm_name=llm_name, debug_dir=debug_dir, work=work, fixes=fixes)
    return rep, issues
