# singtogether-ocr

악보 이미지/PDF → MusicXML. 앱과 분리된 Python 패키지 + CLI. 이조는 Swift CLI(`singtogether-transpose`), 렌더링은 Verovio.

```
PDF/이미지 → 1 ingest(보정·확대·이진화) → 2 layout(오선·코드띠·가사띠) → 3 engines(멜로디 OMR)
          → notes(음표 머리·마디줄 좌표) → 4 text(Apple Vision 코드 OCR) + lyrics(한글 가사 OCR·정렬·제목)
          → 5 assemble(마디 배정, 오선 위치로 음높이 보정) + key(코드로 조표·박자 추론) → MusicXML
          → 6 singtogether-transpose(Swift) → 7 render(Verovio SVG → rsvg-convert PDF)
```

M2(가사): 가사 띠를 Vision(ko+en)으로 읽어 글자 단위 좌표를 얻고, 한글은 글자마다·영어는 붙임표 조각마다 한 음절로 삼아
x좌표 순서를 지키는 DP로 음표에 짝짓는다. `-`/`_` 멜리스마 표시는 자리 표시자로 음표 하나를 차지한다.
글줄은 y로 묶어 절(1절, 2절, 영어 줄)을 나누고, 다음 오선의 코드 줄·잡음 줄은 걸러낸다. 제목·작곡자는 첫 오선 위에서 읽는다.

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

- `--engine homr|oemer|audiveris|claude|hybrid` (기본 homr). audiveris·claude는 비교용.
  - audiveris: `SINGTOGETHER_AUDIVERIS=/path/Audiveris.app`, 가사 OCR은 `TESSDATA_PREFIX`
  - claude: 로그인된 Claude Code CLI 사용. 모델은 `SINGTOGETHER_CLAUDE_MODEL` (기본 sonnet)
  - **hybrid (권장)**: 음표·음높이·위치는 homr + 오선 위치, 코드·가사·제목 글자는 Claude CLI.
    Vision이 찾은 코드/음절 *자리*와 Claude가 받아 적은 *글자* 열을 글자 일치를 앵커로 DP 정렬해 합친다 (`hybrid.py`).
    Claude 결과는 `<work>/claude/<이름>-p1.txt`에 캐시되어 같은 작업 폴더면 다시 묻지 않는다.
- `--debug DIR`: 단계별 오버레이 `<이름>-1-ingest.png`, `-2-layout`, `-3-symbols`, `-4-chords`, `-4-lyrics`, `-5-assembled`
  (오선=파랑, 음표=초록, 코드=주황, 가사=보라, 영역=회색)
- `--no-chords` / `--no-lyrics`: 코드 / 가사 OCR 생략
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

## 해 본 것 (효과 없음)

- **초해상도 전처리** (2026-10): Real-ESRGAN x4(anime_6B · x4plus · general-x4v3)로 키운 뒤 같은 오선 간격(24px)으로 맞춰 homr에 넣어 봤다.
  획 가장자리만 또렷해지고 뭉개진 16분음표 꼬리·붙임점은 그대로라, 음표는 같거나 나빠지고(주님 notes 75→62%, 부요함 pitch 78→35%(x4plus)),
  코드 OCR은 크게 나빠졌다(부요함 67→26%). 가사만 올랐다(부요함 77→92%, anime_6B). homr는 원본의 자연스러운 번짐에 맞춰 학습돼 있어
  과하게 날카로운 입력이 오히려 해롭다. 가사 띠에만 쓰는 건 가능성이 남아 있다. 지금 Lanczos 확대(`ingest.py`)가 기준.

## 평가

```bash
.venv/bin/singtogether-eval                         # truth/*.txt 와 이름이 같은 music_scores/ 악보 전부
.venv/bin/singtogether-eval --engines homr claude --samples "주님의 임재 앞에서"
.venv/bin/singtogether-eval --cached                # out/ 에 있는 결과만 다시 채점
```

`out/report.md`에 엔진 × 샘플 표. notes = 음높이·길이 둘 다 맞은 비율, pitch = 음높이만, 마디 경계가 틀려도 순서가 맞으면 인정.
정답은 `truth/<악보 이름>.txt`.
