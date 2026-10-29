"""7단계 렌더링: MusicXML → SVG (Verovio) → PDF (Swift CLI가 WebKit으로 그림; 없으면 rsvg-convert)."""
from __future__ import annotations

from pathlib import Path

OPTIONS = {"pageWidth": 2100, "pageHeight": 2970, "scale": 40, "adjustPageHeight": False, "footer": "none",
           "lyricSize": 4.5}


def render(musicxml: Path, out_prefix: Path, pdf: bool = True) -> list[Path]:
    import verovio
    tk = verovio.toolkit()
    tk.setOptions(OPTIONS)
    if not tk.loadFile(str(musicxml)):
        raise RuntimeError(f"Verovio could not read {musicxml}")
    svgs = []
    for i in range(1, tk.getPageCount() + 1):
        p = out_prefix.with_name(f"{out_prefix.name}-{i}.svg")
        p.write_text(_plain_accidentals(tk.renderToSVG(i)))
        svgs.append(p)
    if pdf:
        try:
            svgs.append(svg_to_pdf(svgs, out_prefix.with_suffix(".pdf")))
        except Exception as e:                       # PDF는 덤. SVG만 있어도 된다
            print(f"PDF conversion failed (SVG is there): {e}")
    return svgs


# Verovio가 코드 기호 글자에 쓰는 SMuFL 문자 (figured-bass 계열 EA64–EA66, 일반 임시표 E260–E262)
_SMUFL = {"": "♭", "": "♯", "": "♮", "": "♭", "": "♯", "": "♮"}


def _plain_accidentals(svg: str) -> str:
    """코드 기호 글자 속 ♭/♯는 악보 글꼴(Leipzig)의 전용 문자로 나온다. 글꼴이 없는 변환기가 거대한 □로 그리므로
    일반 유니코드 문자로 바꾼다."""
    import re

    def fix(m: re.Match) -> str:
        ch = _SMUFL.get(m.group(2))
        if not ch:
            return m.group(0)
        return f'<tspan font-size="{int(int(m.group(1)) * 0.56)}px">{ch}</tspan>'

    return re.sub(r'<tspan font-family="Leipzig" font-size="(\d+)px">(.)</tspan>', fix, svg)


def find_transpose() -> str | None:
    """Swift CLI singtogether-transpose: 환경 변수(앱이 번들 안의 것을 준다) → 저장소 빌드 결과."""
    import os
    from .cli import REPO
    if (env := os.environ.get("SINGTOGETHER_TRANSPOSE")) and Path(env).exists():
        return env
    return next((str(p) for p in (REPO / ".build/release/singtogether-transpose", REPO / "build/singtogether-transpose")
                 if p.exists()), None)


def svg_to_pdf(svgs: list[Path], out: Path) -> Path:
    """여러 쪽 SVG를 한 PDF로. Swift CLI(WebKit, 설치 불필요)를 쓰고, 없으면 rsvg-convert(brew install librsvg)."""
    import shutil
    import subprocess

    if cli := find_transpose():
        p = subprocess.run([cli, "--svg-to-pdf", str(out), *map(str, svgs)], capture_output=True, text=True)
        if p.returncode == 0 and out.exists():
            return out
        print(f"WebKit conversion failed ({p.stderr.strip()[-200:]}), trying rsvg-convert")
    tool = shutil.which("rsvg-convert") or next((t for t in ("/opt/homebrew/bin/rsvg-convert", "/usr/local/bin/rsvg-convert") if Path(t).exists()), None)
    if not tool:
        raise RuntimeError("no SVG to PDF converter: build singtogether-transpose (./build.sh) or brew install librsvg")
    subprocess.run([tool, "-f", "pdf", "-o", str(out), *map(str, svgs)], check=True, capture_output=True)
    return out
