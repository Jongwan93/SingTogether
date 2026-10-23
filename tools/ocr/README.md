# singtogether-ocr

악보 이미지/PDF → MusicXML. 앱과 분리된 Python 패키지 + CLI. 이조는 Swift CLI(`singtogether-transpose`), 렌더링은 Verovio.

```
PDF/이미지 → 1 ingest(보정·확대·이진화) → 2 layout(오선·코드띠·가사띠) → 3 engines(멜로디 OMR)
          → notes(음표 머리·마디줄 좌표) → 4 text(Apple Vision 코드 OCR) → 5 assemble → MusicXML
          → 6 singtogether-transpose(Swift) → 7 render(Verovio SVG → rsvg-convert PDF)
```

## 설치

```bash
cd tools/ocr
uv sync                                  # Python 3.13, homr 포함
uv venv --python 3.11 .venv-oemer        # (선택) oemer는 별도 환경
uv pip install --python .venv-oemer/bin/python --no-deps oemer
uv pip install --python .venv-oemer/bin/python onnxruntime "opencv-python-headless<5" matplotlib pillow scipy scikit-learn typing-extensions
brew install librsvg                     # PDF 출력용 rsvg-convert
(cd ../.. && ./build.sh)                 # singtogether-transpose
```

## 사용

```bash
.venv/bin/singtogether-ocr score.jpg --out score.musicxml
.venv/bin/singtogether-ocr score.pdf --engine homr --debug dbg/ --transpose -2 --render
```

- `--engine homr|oemer|audiveris|claude` (기본 homr). audiveris·claude는 비교용.
  - audiveris: `SINGTOGETHER_AUDIVERIS=/path/Audiveris.app`, 가사 OCR은 `TESSDATA_PREFIX`
  - claude: 로그인된 Claude Code CLI 사용. 모델은 `SINGTOGETHER_CLAUDE_MODEL` (기본 sonnet)
- `--debug DIR`: 단계별 오버레이 `<이름>-1-ingest.png`, `-2-layout`, `-3-symbols`, `-4-chords`, `-5-assembled`
  (오선=파랑, 음표=초록, 코드=주황, 가사=보라, 영역=회색)
- `--interline PX`: 확대 목표 오선 간격 (기본 24. homr는 폭 ≈1920px 입력에서 가장 좋았다)
- 출력: `<이름>.musicxml`과 사람이 읽고 고칠 수 있는 `<이름>.txt` (압축 텍스트 형식, 아래)

## 압축 텍스트 형식 (정답 파일 · 인식 결과 캐시)

```
title: 주님의 임재 앞에서
key: G major
time: 4/4
1: r/8 [G] G4/8 G4/8 G4/8 [D/F#] F#4/8 F#4/8 F#4/8 E4/8~ | 주 님 의 임 재 앞 에 _
```

음표 `<음이름><#|b><옥타브>/<길이>[.][~]`(C4 = 가온 다, `~` 붙임줄), 쉼표 `r/8`, 코드 `[G]`, 도돌이 `{: :}`, 괄호 `{1 {2`,
`|` 뒤에 절별 가사(쉼표를 뺀 음표마다 한 음절, `_` = 새 음절 없음).

## 평가

```bash
.venv/bin/singtogether-eval                         # truth/*.txt 와 이름이 같은 music_scores/ 악보 전부
.venv/bin/singtogether-eval --engines homr claude --samples "주님의 임재 앞에서"
.venv/bin/singtogether-eval --cached                # out/ 에 있는 결과만 다시 채점
```

`out/report.md`에 엔진 × 샘플 표. notes = 음높이·길이 둘 다 맞은 비율, pitch = 음높이만, 마디 경계가 틀려도 순서가 맞으면 인정.
정답은 `truth/<악보 이름>.txt`.
