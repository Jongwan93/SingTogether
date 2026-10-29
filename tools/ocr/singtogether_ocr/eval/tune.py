"""검토 표시 가중치 튜닝: 정답이 있는 곡의 hybrid 결과(out/hybrid/<이름>.review.json의 signals)로 review.WEIGHTS를 찾는다.

    singtogether-eval --tune            # 먼저 singtogether-eval --engines hybrid 로 결과를 만들어 둔다

좌표 하강: 신호 하나씩 격자의 값을 바꿔 보며 평균 F1(표시 기준 0.6)이 오르면 받아들이고, 안 오르면 다음 신호로.
여러 시작점에서 돌려 가장 좋은 것을 고른다. 결과는 review.py에 붙여 넣을 수 있는 dict와 곡별 P/R/top 정밀도.
"""
from __future__ import annotations

import json
import random
import unicodedata
from pathlib import Path

from ..review import FLAG, TOP, WEIGHTS, confidence, effective
from ..textfmt import from_text
from .score import wrong_notes

PENALTY_GRID = [0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 1.0]
BOOST_GRID = [1.0, 1.2, 1.4, 1.6, 2.0]
BOOSTS = {"ai_same", "ai_same_weak"}


def load(out_dir: Path, truth_dir: Path) -> list[tuple[str, list[list[str]], set[int]]]:
    """곡마다 (이름, 음표별 신호 목록(전체 순서), 틀린 음표 번호)."""
    nfc = lambda s: unicodedata.normalize("NFC", s)
    songs = []
    for truth_file in sorted(truth_dir.glob("*.txt")):
        name = nfc(truth_file.stem)
        rev = next((p for p in out_dir.glob("*.review.json") if nfc(p.name) == f"{name}.review.json"), None)
        txt = next((p for p in out_dir.glob("*.txt") if nfc(p.name) == f"{name}.txt"), None)
        if not rev or not txt:
            continue
        t = from_text(txt.read_text())
        flat = {}
        k = 0
        for m in t.measures:
            for i in range(len(m.notes)):
                flat[(m.number, i)] = k
                k += 1
        signals = [[] for _ in range(k)]
        data = json.loads(rev.read_text())
        for it in data["notes"]:
            if (j := flat.get((it["measure"], it["index"]))) is not None:
                signals[j] = effective(it.get("signals", []), data.get("claude_match"))
        songs.append((name, signals, wrong_notes(t, from_text(truth_file.read_text()))))
    return songs


def evaluate(weights: dict[str, float], songs) -> tuple[float, list[dict]]:
    """평균 F1과 곡별 (P, R, F1, top 정밀도)."""
    rows = []
    for name, signals, wrong in songs:
        conf = [confidence(s, weights) for s in signals]
        flagged = {i for i, c in enumerate(conf) if c < FLAG}
        tp = len(flagged & wrong)
        p = tp / len(flagged) if flagged else 0.0
        r = tp / len(wrong) if wrong else 0.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        order = sorted(range(len(conf)), key=lambda i: (conf[i], -len(signals[i])))[:TOP]
        top = sum(1 for i in order if i in wrong) / max(1, len(order))
        rows.append({"name": name, "P": p, "R": r, "F1": f1, "top": top, "flagged": len(flagged), "wrong": len(wrong)})
    return sum(r["F1"] for r in rows) / max(1, len(rows)), rows


def search(songs, start: dict[str, float], rng: random.Random) -> tuple[float, dict[str, float]]:
    w = dict(start)
    best, _ = evaluate(w, songs)
    improved = True
    while improved:
        improved = False
        keys = list(w)
        rng.shuffle(keys)
        for k in keys:
            grid = BOOST_GRID if k in BOOSTS else PENALTY_GRID
            for v in grid:
                if v == w[k]:
                    continue
                trial = {**w, k: v}
                f, _ = evaluate(trial, songs)
                if f > best + 1e-9:
                    best, w, improved = f, trial, True
    return best, w


def main(out_dir: Path, truth_dir: Path, restarts: int = 12) -> None:
    songs = load(out_dir, truth_dir)
    if not songs:
        raise SystemExit(f"No hybrid results with signals in {out_dir} (run singtogether-eval --engines hybrid first)")
    rng = random.Random(0)
    base_f1, base_rows = evaluate(WEIGHTS, songs)
    print(f"current WEIGHTS: mean F1 {base_f1:.3f}")
    for r in base_rows:
        print(f"   {r['name']}: P {r['P']:.0%} R {r['R']:.0%} F1 {r['F1']:.2f} top{TOP} {r['top']:.0%} ({r['flagged']} flagged/{r['wrong']} wrong)")
    best, best_w = search(songs, WEIGHTS, rng)
    for _ in range(restarts):
        start = {k: rng.choice(BOOST_GRID if k in BOOSTS else PENALTY_GRID) for k in WEIGHTS}
        f, w = search(songs, start, rng)
        if f > best + 1e-9:
            best, best_w = f, w
    _, rows = evaluate(best_w, songs)
    print(f"\nbest: mean F1 {best:.3f}")
    for r in rows:
        print(f"   {r['name']}: P {r['P']:.0%} R {r['R']:.0%} F1 {r['F1']:.2f} top{TOP} {r['top']:.0%} ({r['flagged']} flagged/{r['wrong']} wrong)")
    print("\nWEIGHTS = {")
    for k, v in best_w.items():
        print(f'    "{k}": {v},')
    print("}")
