"""singtogether-inplace-check: 제자리 이조 결과를 정답(truth/inplace/<이름>.json)과 견준다. 코드와 기호를 본다.

    singtogether-inplace-check                       # 정답이 있는 곡 전부 (AI 읽기는 truth/inplace/work/의 캐시)
    singtogether-inplace-check --songs "보혈을 지나"
    singtogether-inplace-check --llm none            # Apple Vision만 (AI 없는 경로)
    singtogether-inplace-check --sheets              # 틀린 곳 조각 그림: out/inplace/<이름>-*.png
    singtogether-inplace-check --init "보혈을 지나"   # 지금 결과로 정답 초안과 검수 그림을 만든다 (손으로 고쳐 쓴다)

1. 코드: 정답 코드마다 같은 자리(오선, 상자의 가로 절반 넘게 겹침)에 그린 코드가 있고, 읽은 이름이 같은가.
   ok / wrong(다른 이름으로 읽음) / missed(안 바뀜) / extra(코드가 없는 데 씀) / partial(옛 글자 잉크를 다 못 지움).
2. 기호: 코드 띠(오선 위)에서 정답 코드 글자 밖의 원본 잉크가 지워졌거나(lost) 새 잉크가 생겼으면(gained) 센다.
   옮긴 음표(머리·덧줄·기둥·임시표)의 자리와 정답의 allow 상자는 뺀다. Coda·괄호 번호·D.C.·늘임표를 지우던 일을 잡는다.

결과는 out/inplace/report.json에 남고, 다음 실행은 그것과 견주어 새로 생긴 문제와 고쳐진 문제를 따로 보인다.
정답 파일(작업 배율 좌표; scale이 바뀌면 비율로 맞춘다):
    {"semitones": 2, "scale": 4.0, "chords": [{"system": 0, "box": [x0, y0, x1, y1], "name": "Ab/C"}], "allow": [[x0, y0, x1, y1]]}
저작권 있는 곡이라 truth/inplace/는 git에 넣지 않는다 (.gitignore의 tools/ocr/truth/*).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

import cv2
import numpy as np

from .compare import OCR_ROOT, _nfc, score_images

TRUTH = OCR_ROOT / "truth" / "inplace"
OUT = OCR_ROOT / "out" / "inplace"


def _run(img: Path, semitones: int, llm: str, work: Path) -> dict:
    """제자리 이조를 돌려 검사에 쓸 것을 모은다 (작업 배율)."""
    from .. import chordoverlay, inplace
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        rep = inplace.transpose_in_place(img, semitones, OUT / "work-out" / f"{_nfc(img.stem)}.png", llm_name=llm, work=work)
    st = rep.state
    res = st["result"]
    if res.ndim == 3:
        res = cv2.cvtColor(res, cv2.COLOR_BGR2GRAY)
    return {"page": st["page"], "systems": st["systems"], "il": st["il"], "plans": st["plans_all"], "keysigs": st["keysigs"], "arcs": st.get("moved_arcs", []), "result": res,
            "drawn": list(st.get("drawn_chords", [])), "erased": list(getattr(chordoverlay.draw, "erased", [])),
            "source": rep.chord_source}


def _scaled(box, f: float) -> tuple[int, int, int, int]:
    return tuple(int(round(v * f)) for v in box)


def _overlap(a, b) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0]))


def check_chords(truth: list[dict], r: dict, f: float) -> list[dict]:
    """정답 코드와 그린 코드를 짝짓는다. 결과 항목: kind, system, box, name, read."""
    from ..text import normalize_chord
    drawn = [(int(d[0]), (d[1], d[2], d[3], d[4]), d[5]) for d in r["drawn"]]
    used: set[int] = set()
    ink = r["page"].binary < 128
    erased = np.zeros(ink.shape, bool)
    for x0, y0, x1, y1 in r["erased"]:
        erased[max(0, y0):y1, max(0, x0):x1] = True
    out = []
    for t in truth:
        tb = _scaled(t["box"], f)
        cands = [(k, _overlap(tb, b)) for k, (si, b, _) in enumerate(drawn) if si == t["system"] and k not in used]
        cands = [(k, o) for k, o in cands if o >= 0.5 * min(tb[2] - tb[0], drawn[k][1][2] - drawn[k][1][0])]
        item = {"system": t["system"], "box": list(tb), "name": t["name"]}
        if not cands:
            out.append({**item, "kind": "missed", "read": ""})
            continue
        k = max(cands, key=lambda c: c[1])[0]
        used.add(k)
        read = drawn[k][2]
        if normalize_chord(read) != normalize_chord(t["name"]):
            out.append({**item, "kind": "wrong", "read": read})
            continue
        letters = ink[tb[1]:tb[3], tb[0]:tb[2]]
        covered = (letters & erased[tb[1]:tb[3], tb[0]:tb[2]]).sum() / max(1, letters.sum())
        out.append({**item, "kind": "ok" if covered >= 0.9 else "partial", "read": read})
    for k, (si, b, name) in enumerate(drawn):
        if k not in used:
            out.append({"system": si, "box": [int(v) for v in b], "name": "", "read": name, "kind": "extra"})
    return out


def check_symbols(truth: list[dict], allow: list, r: dict, f: float) -> list[dict]:
    """코드 띠에서 정답 코드 글자·옮긴 음표·allow 밖의 잉크 변화 덩어리."""
    il, page, res = r["il"], r["page"], r["result"]
    orig = page.binary < 128
    now = res < 128
    H, W = orig.shape
    region = np.zeros((H, W), bool)
    for s in r["systems"]:
        top = max(0, int(s.chord_band[0] - 1.5 * il))
        region[top:max(top, int(s.staff.top - 0.25 * il)), max(0, int(s.staff.left - il)):int(s.staff.right + il)] = True
    skip = np.zeros((H, W), bool)                     # 잃은 잉크·생긴 잉크 둘 다 안 보는 곳
    new_text = np.zeros((H, W), bool)                 # 생긴 잉크만 안 보는 곳: 새 코드 글자 (옛 것보다 넓고 윗첨자가 높기도 하다)
    pad = int(0.25 * il)
    for t in truth:
        x0, y0, x1, y1 = _scaled(t["box"], f)
        skip[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad] = True
        new_text[max(0, y0 - int(0.6 * il)):y1 + pad, max(0, x0 - int(1.0 * il)):x1 + int(2.5 * il)] = True
    for box in allow:
        x0, y0, x1, y1 = _scaled(box, f)
        skip[max(0, y0):y1, max(0, x0):x1] = True
    for (s, plans), ks in zip(r["plans"], r["keysigs"]):   # 조표·박자표를 다시 쓰는 자리: 오선 왼쪽 끝 ~ 첫 음표 앞 (♯·♭이 오선 위로 나온다)
        first = min((p.head.x for p in plans), default=ks[3] + 2 * il)
        end = min(max(ks[3] + 1.5 * il, first - 1.0 * il), ks[3] + 5 * il)   # 새 조표가 더 길고 박자표도 다시 쓴다; 쉼표로 시작하는 줄에서 멀리 가지 않게
        rows = slice(max(0, int(s.chord_band[0] - 1.5 * il)), int(s.staff.top))
        skip[rows, max(0, int(s.staff.left - il)):int(end)] = True
    for x0, y0, x1, y1, dy in r["arcs"]:              # 옮긴 이음줄·붙임줄: 옛 자리와 새 자리
        skip[max(0, min(y0, y0 + dy) - pad):max(y1, y1 + dy) + pad, max(0, x0 - pad):x1 + pad] = True
    for s, plans in r["plans"]:                       # 옮긴 음표: 옛·새 머리 사이와 오선까지의 덧줄·기둥, 임시표, 점
        for p in plans:
            h = p.head
            y_new = s.staff.bottom - p.step_new * il / 2
            ya, yb = min(h.y, y_new) - 0.8 * il, max(h.y, y_new) + 0.8 * il
            if ya < s.staff.top:
                yb = max(yb, s.staff.top)
            skip[max(0, int(ya)):int(yb), max(0, int(h.x - 1.1 * il)):int(h.x + 1.1 * il)] = True
            for b in (p.acc_old[1] if p.acc_old else None, p.dot):
                if b:
                    skip[max(0, b[1] - pad):b[3] + pad, max(0, b[0] - pad):b[2] + pad] = True
    out = []
    min_area = 0.06 * il * il
    thin = max(2, int(0.15 * il))
    for kind, mask in (("lost", orig & ~now & ~skip), ("gained", now & ~orig & ~skip & ~new_text)):
        m = (mask & region).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))   # 흐린 가장자리 한두 점은 뺀다
        n, _, stt, _ = cv2.connectedComponentsWithStats(cv2.dilate(m, np.ones((5, 5), np.uint8)), connectivity=8)
        for i in range(1, n):
            x, y, w, h, _ = stt[i]
            area = int(m[y:y + h, x:x + w].sum())
            core = m[y:y + h, x:x + w]
            # 빔·줄 가장자리의 실오라기(두께 0.15il 이하)는 흐린 그림을 다시 그리며 생기는 것이다
            if not core.any() or min(np.flatnonzero(core.any(axis=1)).size, np.flatnonzero(core.any(axis=0)).size) <= thin:
                continue
            if area >= min_area:
                out.append({"kind": kind, "box": [int(x), int(y), int(x + w), int(y + h)], "area": area})
    return out


def _key(song: str, item: dict, il: float) -> str:
    """다음 실행과 견줄 열쇠: 자리는 il 단위로 반올림 (작은 흔들림은 같은 문제로)."""
    b = item["box"]
    return f"{song}|{item['kind']}|{item.get('system', '')}|{round(b[0] / il)}|{round(b[1] / il)}|{item.get('name', '')}|{item.get('read', '')}"


def _sheet(song: str, r: dict, items: list[dict], path: Path) -> None:
    """문제 자리마다 원본 | 결과 조각 (붉은 상자)."""
    il, g, res = r["il"], r["page"].gray, r["result"]
    rows = []
    for it in items:
        x0, y0, x1, y1 = it["box"]
        X0, Y0 = max(0, int(x0 - 2 * il)), max(0, int(y0 - 1.5 * il))
        X1, Y1 = min(g.shape[1], int(x1 + 2 * il)), min(g.shape[0], int(y1 + 1.5 * il))
        a = cv2.cvtColor(g[Y0:Y1, X0:X1], cv2.COLOR_GRAY2BGR)
        b = cv2.cvtColor(res[Y0:Y1, X0:X1], cv2.COLOR_GRAY2BGR)
        for im in (a, b):
            cv2.rectangle(im, (x0 - X0, y0 - Y0), (x1 - X0, y1 - Y0), (0, 0, 255), 1)
        c = np.hstack([a, np.full((a.shape[0], 4, 3), 128, np.uint8), b])
        c = cv2.resize(c, None, fx=440 / c.shape[1], fy=440 / c.shape[1])
        label = f"{it['kind']} s{it.get('system', '')} {it.get('name', '')} read:{it.get('read', '')}".encode("ascii", "replace").decode()
        c = np.vstack([np.full((22, c.shape[1], 3), 255, np.uint8), c])
        cv2.putText(c, label, (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 200), 1)
        rows.append(np.pad(c, ((0, 6), (0, 0), (0, 0)), constant_values=200))
    for g_ in range(0, len(rows), 10):
        cv2.imwrite(str(path.with_name(f"{path.stem}-{g_ // 10}.png")), np.vstack(rows[g_:g_ + 10]))


def _overview(song: str, r: dict, truth: list[dict], allow: list, f: float, path: Path) -> None:
    """검수 그림: 오선마다 원본 위에 정답 코드 상자(초록)·이름과 allow(파랑), 옆에 결과."""
    il, g, res = r["il"], r["page"].gray, r["result"]
    for si, s in enumerate(r["systems"]):
        y0, y1 = max(0, int(s.chord_band[0] - 1.5 * il)), int(s.staff.bottom + 0.5 * il)
        a = cv2.cvtColor(g[y0:y1], cv2.COLOR_GRAY2BGR)
        for t in truth:
            if t["system"] == si:
                x0, ty0, x1, ty1 = _scaled(t["box"], f)
                cv2.rectangle(a, (x0, ty0 - y0), (x1, ty1 - y0), (0, 160, 0), 2)
                cv2.putText(a, t["name"].encode("ascii", "replace").decode(), (x0, ty1 - y0 + int(0.9 * il)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.03 * il, (0, 120, 0), 2)
        for box in allow:
            x0, ay0, x1, ay1 = _scaled(box, f)
            if ay1 > y0 and ay0 < y1:
                cv2.rectangle(a, (x0, ay0 - y0), (x1, ay1 - y0), (200, 0, 0), 2)
        b = cv2.cvtColor(res[y0:y1], cv2.COLOR_GRAY2BGR)
        c = np.vstack([a, np.full((6, a.shape[1], 3), 128, np.uint8), b])
        c = cv2.resize(c, None, fx=1800 / c.shape[1], fy=1800 / c.shape[1])
        cv2.imwrite(str(path.with_name(f"{path.stem}-s{si}.png")), c)


def init(song: str, img: Path, llm: str, semitones: int) -> None:
    """지금 결과의 그린 코드로 정답 초안을 쓴다 (잉크 상자로 줄인다). 검수 그림을 보고 손으로 고친다."""
    work = TRUTH / "work" / song
    work.mkdir(parents=True, exist_ok=True)
    r = _run(img, semitones, llm, work)
    ink = r["page"].binary < 128
    chords = []
    for d in r["drawn"]:
        x0, y0, x1, y1 = (int(v) for v in d[1:5])
        sub = ink[y0:y1, x0:x1]
        ys, xs = np.nonzero(sub)
        if xs.size:
            x0, y0, x1, y1 = x0 + int(xs.min()), y0 + int(ys.min()), x0 + int(xs.max()) + 1, y0 + int(ys.max()) + 1
        chords.append({"system": int(d[0]), "box": [x0, y0, x1, y1], "name": d[5]})
    path = TRUTH / f"{song}.json"
    if path.exists():
        sys.exit(f"{path} exists; delete it first to start over")
    data = {"semitones": semitones, "scale": r["page"].scale, "chords": chords, "allow": []}
    path.write_text(_dump(data))
    OUT.mkdir(parents=True, exist_ok=True)
    _overview(song, r, chords, [], 1.0, OUT / f"{song}-review.png")
    print(f"{path}: {len(chords)} chords; review {OUT}/{song}-review-s*.png and fix names, boxes, missing chords")


def _dump(data: dict) -> str:
    """한 코드 한 줄로 (손으로 고치기 쉽게)."""
    lines = ["{", f'  "semitones": {data["semitones"]},', f'  "scale": {data["scale"]},', '  "chords": [']
    lines += [f"    {json.dumps(c, ensure_ascii=False)}," for c in data["chords"]]
    if data["chords"]:
        lines[-1] = lines[-1].rstrip(",")
    lines += ["  ],", '  "allow": [']
    lines += [f"    {json.dumps(b)}," for b in data["allow"]]
    if data["allow"]:
        lines[-1] = lines[-1].rstrip(",")
    lines += ["  ]", "}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--songs", nargs="*")
    ap.add_argument("--llm", default="claude", help="AI reader for chord names (cached in truth/inplace/work), or none")
    ap.add_argument("--sheets", action="store_true", help="write crops of every problem to out/inplace/")
    ap.add_argument("--overview", action="store_true", help="write per-system review pictures with the truth boxes")
    ap.add_argument("--init", metavar="SONG", help="write a draft truth from the current result")
    ap.add_argument("--semitones", type=int, default=2)
    a = ap.parse_args(argv)
    images = score_images()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.init:
        name = _nfc(a.init)
        if name not in images:
            sys.exit(f"no picture named {name}")
        init(name, images[name], a.llm, a.semitones)
        return 0
    wanted = {_nfc(s) for s in a.songs} if a.songs else None
    report_path = OUT / f"report-{a.llm}.json"
    before = json.loads(report_path.read_text()) if report_path.exists() else None
    keys: dict[str, list[str]] = {}
    total = {"ok": 0, "chords": 0, "lost": 0, "gained": 0}
    for path in sorted(TRUTH.glob("*.json")):
        song = _nfc(path.stem)
        if (wanted and song not in wanted) or song not in images:
            continue
        data = json.loads(path.read_text())
        r = _run(images[song], data.get("semitones", 2), a.llm, TRUTH / "work" / song)
        f = r["page"].scale / data.get("scale", r["page"].scale)
        chords = check_chords(data["chords"], r, f)
        symbols = check_symbols(data["chords"], data.get("allow", []), r, f)
        bad = [c for c in chords if c["kind"] != "ok"] + symbols
        count = {k: sum(1 for c in chords if c["kind"] == k) for k in ("ok", "wrong", "missed", "extra", "partial")}
        lost, gained = sum(1 for s in symbols if s["kind"] == "lost"), sum(1 for s in symbols if s["kind"] == "gained")
        print(f"{song:20s} chords {count['ok']:3d}/{len(data['chords']):3d}  wrong {count['wrong']}  missed {count['missed']}  "
              f"extra {count['extra']}  partial {count['partial']}  symbols lost {lost} gained {gained}  ({r['source']})")
        for c in bad:
            where = f"s{c['system']} " if "system" in c else ""
            what = f"{c.get('name', '')} read {c.get('read', '')!r}" if c["kind"] in ("wrong", "missed", "extra", "partial") else f"area {c['area']}"
            print(f"    {c['kind']:8s} {where}x{c['box'][0]} y{c['box'][1]}  {what}")
        keys[song] = [_key(song, c, r["il"]) for c in bad]
        total["ok"] += count["ok"]
        total["chords"] += len(data["chords"])
        total["lost"] += lost
        total["gained"] += gained
        if a.sheets and bad:
            _sheet(song, r, bad, OUT / f"{song}.png")
        if a.overview:
            _overview(song, r, data["chords"], data.get("allow", []), f, OUT / f"{song}-review.png")
    print(f"\ntotal chords {total['ok']}/{total['chords']}  symbols lost {total['lost']} gained {total['gained']}")
    if before:
        old = {k for song, ks in before.items() if song in keys for k in ks}
        new = {k for ks in keys.values() for k in ks}
        for title, items in (("new problems since last run", sorted(new - old)), ("fixed since last run", sorted(old - new))):
            if items:
                print(f"\n{title}:")
                for k in items:
                    print("    " + k.replace("|", "  "))
    merged = dict(before or {})
    merged.update(keys)
    report_path.write_text(json.dumps(merged, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
