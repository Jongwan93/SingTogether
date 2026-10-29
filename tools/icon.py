"""앱 아이콘 만들기: 둥근 네모 그라데이션 위에 흰 ♪ → Resources/AppIcon.icns (한 번만 돌리고 결과를 저장소에 넣는다).

    tools/ocr/.venv/bin/python tools/icon.py
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FONT = "/System/Library/Fonts/Apple Symbols.ttf"


def tile(size: int) -> Image.Image:
    s = size * 4                                            # 크게 그려서 줄이면 테두리가 매끈하다
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    # macOS 아이콘 격자: 전체의 약 80%를 차지하는 둥근 네모
    pad = int(s * 0.10)
    box = (pad, pad, s - pad, s - pad)
    grad = Image.new("RGBA", (s, s))
    top, bottom = (88, 86, 214), (142, 68, 173)             # 남보라 → 보라
    for y in range(s):
        t = y / (s - 1)
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,)
        ImageDraw.Draw(grad).line([(0, y), (s, y)], fill=c)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=int(s * 0.18), fill=255)
    img.paste(grad, (0, 0), mask)
    # 흰 음표
    font = ImageFont.truetype(FONT, int(s * 0.62))
    d = ImageDraw.Draw(img)
    glyph = "♪"
    bb = d.textbbox((0, 0), glyph, font=font)
    w, h = bb[2] - bb[0], bb[3] - bb[1]
    d.text(((s - w) / 2 - bb[0], (s - h) / 2 - bb[1] - s * 0.02), glyph, font=font, fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    out = ROOT / "Resources" / "AppIcon.icns"
    out.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        iconset = Path(d) / "AppIcon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            tile(size).save(iconset / f"icon_{size}x{size}.png")
            tile(size * 2).save(iconset / f"icon_{size}x{size}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
        tile(256).save(ROOT / "Resources" / "AppIcon-256.png")      # README 등에 쓰는 미리보기
    print(f"→ {out}")


if __name__ == "__main__":
    main()
