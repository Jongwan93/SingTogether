"""7단계 렌더링: MusicXML → SVG (Verovio) → PDF (macOS AppKit로 SVG를 PDF 컨텍스트에 그림)."""
from __future__ import annotations

from pathlib import Path

OPTIONS = {"pageWidth": 2100, "pageHeight": 2970, "scale": 40, "adjustPageHeight": False, "footer": "none",
           "lyricSize": 4.5}


def render(musicxml: Path, out_prefix: Path, pdf: bool = True) -> list[Path]:
    import verovio
    tk = verovio.toolkit()
    tk.setOptions(OPTIONS)
    if not tk.loadFile(str(musicxml)):
        raise RuntimeError(f"Verovio가 읽지 못했어요: {musicxml}")
    svgs = []
    for i in range(1, tk.getPageCount() + 1):
        p = out_prefix.with_name(f"{out_prefix.name}-{i}.svg")
        p.write_text(_plain_accidentals(tk.renderToSVG(i)))
        svgs.append(p)
    if pdf:
        try:
            svgs.append(svg_to_pdf(svgs, out_prefix.with_suffix(".pdf")))
        except Exception as e:                       # PDF는 덤. SVG만 있어도 된다
            print(f"PDF 변환 실패 (SVG는 있음): {e}")
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


def svg_to_pdf(svgs: list[Path], out: Path) -> Path:
    """rsvg-convert(librsvg, brew install librsvg)로 여러 쪽 SVG를 한 PDF로. 앱에서는 WKWebView로 대신할 수 있다."""
    import shutil
    import subprocess

    tool = shutil.which("rsvg-convert")
    if not tool:
        raise RuntimeError("rsvg-convert가 없어요 (brew install librsvg)")
    subprocess.run([tool, "-f", "pdf", "-o", str(out), *map(str, svgs)], check=True, capture_output=True)
    return out
