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
- 엔진에는 코드·가사 글줄을 흰색으로 지운 쪽을 준다 (`layout.clean_staff_image`, 오선 ±1.5il은 보존). `--no-mask`로 생략.
  조립 단계를 고친 뒤 재니 웹 캡처 notes +0/+4/+4%p, 마디 수도 맞아져 기본으로 켰다 (2026-10-28).
- 결과 캐시: 파일 내용 해시 + 설정으로 `~/Library/Caches/SingTogether/ocr/<키>/`에 결과를 두고, 같은 악보(다른 이름·폴더여도)는
  다시 인식하지 않는다. `--no-cache`로 끔, `SINGTOGETHER_CACHE`로 위치 변경. 인식 논리가 바뀌면 `cli.PIPELINE_VERSION`을 올린다.
- 코드 OCR 후보정(`sanitize.py`): 문법에 안 맞는 읽기(`8b2`, `F4m`, `A/C4`, `AIC#`, `Cim`)를 자리별 혼동표로 살린다.
  유효한 읽기가 하나도 없는 글 덩어리에만 쓴다. 조성의 흔한 코드 사전(`common_chords`)도 있지만 유효한 읽기를 바꾸는 데는 쓰지 않는다
  (조 추정이 틀리면 맞는 코드를 망쳤다). 지금 샘플 7장에서는 ±0.
- `--interline PX`: 확대 목표 오선 간격 (기본 24. homr는 폭 ≈1920px 입력에서 가장 좋았다)
- 출력: `<이름>.musicxml`과 사람이 읽고 고칠 수 있는 `<이름>.txt` (압축 텍스트 형식, 아래)
- **코드 차트** (오선 없이 코드 줄 + 가사 줄): 오선이 하나도 안 잡히면 자동으로 이 경로. 글자를 `--llm claude|gemini|none|auto`로
  읽어 앱의 텍스트 이조가 그대로 읽는 `<이름>.txt`를 만든다 (`--transpose N`이면 `<이름> (+N, 키).txt`까지).
  auto는 Claude Code CLI → agy → Apple Vision 순으로 있는 것을 쓴다. 결과는 `<work>/chordsheet/`에 캐시.

## 압축 텍스트 형식 (정답 파일 · 인식 결과 캐시)

```
title: 주님의 임재 앞에서
key: G major
time: 4/4
1: r/8 [G] G4/8 G4/8 G4/8 [D/F#] F#4/8 F#4/8 F#4/8 E4/8~ | 주 님 의 임 재 앞 에 _
```

음표 `<음이름><#|b><옥타브>/<길이>[.][~]`(C4 = 가온 다, `~` 붙임줄), 쉼표 `r/8`, 코드 `[G]`, 도돌이 `{: :}`, 괄호 `{1 {2`,
`|` 뒤에 절별 가사(쉼표를 뺀 음표마다 한 음절, `_` = 새 음절 없음).

## 코드 차트 (오선 없이 코드 줄 + 가사 줄)

`music_scores/english/`의 힐송·찬송가 코드 차트 6장(387~768px 웹 이미지)으로 확인 (2026-10). 손으로 적은 정답 코드와 비교한 재현율:

| 방법 | 10,000 Reasons | Blessed Assurance | Emmanuel | Here I Am to Worship |
|---|---|---|---|---|
| Vision 한 번 (×3) | 45% | 77% | 95% | 77% |
| Vision 여러 배율 합집합 + 못 읽은 잉크 덩어리 재OCR (`--llm none`) | 81% | 96% | 80% | 94% |
| Claude CLI(sonnet) 전사 | **100%** | **100%** | 97% | 98% |
| Gemini(agy) 전사 | **100%** | **100%** | 97% | 98% |

- Vision이 놓치는 건 거의 전부 외따로 선 한 글자 코드(C, G, D, B). 글자 자체는 맞게 읽는다.
- Claude와 Gemini(agy, 프롬프트에 "셸 명령은 쓰지 말고 이미지만 봐라"를 넣어야 헤드리스로 돈다)가 토큰 단위로 완전히 같은 결과. 오선 악보와 달리 코드 차트는 어느 LLM이든 된다.
- Claude 전사는 "코드 줄 위, 가사 줄 아래" 텍스트 그대로 나오므로 앱의 `.txt` 이조(`ChordText`)에 바로 넣으면 된다.
  네 장 모두 `singtogether-transpose` +2로 옮겨 확인했고, 그 과정에서 붙임표로 이은 진행(`Bb9-Dm7-Eb-F`)이
  안 옮겨지던 것과 조 판별(첫 코드만 보던 것)을 고쳤다. 한 장에 약 20~30초, $0.03.
- CLI에 들어 있다 (`chordsheet.py`): 오선이 안 잡히는 입력 → `--llm`으로 전사 → `.txt` → `--transpose`. Vision 경로가 놓치는 건
  외따로 선 한 글자 코드와 굵은 색 글씨의 F 같은 것이라, LLM CLI가 없을 때의 대비책으로 본다.

## 확인·수정 단계 (M3)

인식은 100%가 안 되므로(웹 캡처 음높이 64~82%) 사용자가 의심스러운 곳만 보고 고치게 한다. 매 실행에 세 가지가 더 나온다.

- `<이름>.review.json`: 음표·코드·음절마다 신뢰도(0–1)와 근거. `threshold`(0.6) 아래가 확인 목록. 앱이 색으로 보여 줄 재료.
- `<이름>-p1.review.png`: 원본 위에 확인할 곳 표시 (음표 빨강, 코드 주황, 가사 보라, 마디 번호와 읽은 음).
- MusicXML의 `color`: 의심스러운 음표·코드·가사가 빨갛게 렌더링된다 (Verovio가 그대로 그린다).

근거는 조립 단계가 남긴다 (`review.py`가 신뢰도로 바꾼다): 머리 못 찾음, 마디 머리 수 불일치, 박자 합 안 맞음,
오선 위치로 고침, 하이브리드일 때 Claude 전사와 다름/없음 (반음 이동은 허용해 맞춘다). 가중치는 정답 3곡으로 격자 탐색한 값.

고치는 흐름: `<이름>.txt`(압축 텍스트)를 review.png를 보며 고친 뒤 그대로 넣는다.

```bash
.venv/bin/singtogether-ocr score.jpg --engine hybrid          # → score.txt, score.review.json, score-p1.review.png
#  score.txt 를 고친다 (마디 번호로 찾는다)
.venv/bin/singtogether-ocr score.txt --transpose -2 --render  # 고친 텍스트 → MusicXML → 이조 → PDF
```

표시 품질 (`singtogether-eval`의 review 열, 신뢰도 < 0.6을 표시로 봄): 정밀도 = 표시한 것 중 실제 오류, 재현율 = 실제 오류 중 표시된 것.

| 샘플 | hybrid 표시/오류 | 정밀도 | 재현율 | homr(로컬) 정밀도 / 재현율 |
|---|---|---|---|---|
| 주님의 임재 앞에서 | 31 / 29 | 74% | 79% | 64% / 24% |
| 이 세상의 부요함보다 | 140 / 75 | 39% | 72% | 44% / 41% |
| 모든 능력과 모든 권세 | 135 / 93 | 67% | 97% | 66% / 61% |

- Claude 전사와의 비교가 가장 강한 근거라 hybrid에서 재현율이 높다. 로컬 전용(homr)은 근거가 적어 절반쯤 놓친다.
- 부요함은 오류가 고르게 퍼져 있어 표시가 많다(음표의 61%). 목록은 신뢰도 낮은 순이라 위에서부터 보면 된다.
- 오류 기준은 음높이·길이 둘 다. 리듬만 틀린 것도 오류로 세므로 음높이만 보면 더 적다.

## 상한 측정 (깨끗한 입력이면 어디까지 되나)

정답 악보 3곡을 Verovio로 렌더링해(폭 2480/1240/620px) 같은 파이프라인에 넣은 결과 (2026-10):

| 입력 | homr 원본 notes/pitch | 최종 notes/pitch |
|---|---|---|
| 웹 캡처 (620~800px) | 27~76% / 39~79% | 38~74% / 64~82% |
| 깨끗한 렌더링 1240·2480px | **100% / 100%** | 86~100% / 93~100% (고치기 전 11~88%) |
| 깨끗한 렌더링 620px | 100% / 100% | 80~100% / 80~100% |

- 웹 캡처는 homr 자체가 잘 못 읽고(특히 리듬), 조립 단계(오선 위치로 음높이 보정, 마디 재배정)가 그걸 57~84%까지 끌어올린다.
  (처음 적었던 "7~34%"는 세 곡의 결과 파일을 한 폴더에 섞어 둔 채점 실수였다.)
- 반대로 깨끗한 입력은 homr가 완벽한데 **조립 단계가 12~41%p를 깎는다**. 음높이 보정이 한 칸씩 밀리고(G G G F# → G G G G), 붙임줄·괄호가 빠진다.
  부요함 1240px는 코드 OCR이 ♭를 놓쳐 추론한 조가 E장조로 바뀌면서 14%까지 떨어졌다 (조표는 homr가 맞게 읽었는데 코드 기반 추론이 덮어씀).
- 2026-10-28에 조립 단계를 고쳤다 (`singtogether-eval --rendered`가 회귀 검사):
  - 음높이 덮어쓰기는 마디 안 머리 수와 음표 수가 같은 '확실한 짝'과 기대 자리에 가까운 짝에만. 확실한 짝에서 엔진과 오선 위치가
    93% 넘게 일치하면 엔진을 믿고(깨끗한 입력), 아니면 예전처럼 수가 비슷한 한 전부 덮어쓴다(웹 캡처).
  - 코드로 추론한 조는 엔진 조표와 5도권 2칸 안이거나, 엔진이 읽은 음들이 그 조에 더 잘 맞을 때만 쓴다 (A♭→E장조 사고 방지).
  - 첫 마디의 x 범위는 음자리표·조표 자리를 뺀 곳부터 (기대 x가 왼쪽으로 쏠려 한 머리씩 어긋나던 것).
  - 마디줄 판정 창을 오선에 붙여 붙임줄이 지나가는 마디줄을 버리지 않게. 세로로 긴 속 빈 후보(8분음표 꼬리)는 버리고,
    점이 붙어 넓어진 2분음표는 왼쪽 머리 부분만 본다.
  - 남은 것: 붙임줄이 붙은 2분음표 머리를 놓친다(기둥 끝 탐지는 가짜가 늘어 뺐다). 620px 렌더링은 80%대.

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
.venv/bin/singtogether-eval --chordsheets --llm none  # 코드 차트 (music_scores/english, truth/chordsheets) → out/chordsheets.md
.venv/bin/singtogether-eval --rendered               # 정답을 Verovio로 그려 넣는 상한·조립 회귀 검사 → out/rendered.md
```

`out/report.md`에 엔진 × 샘플 표. notes = 음높이·길이 둘 다 맞은 비율, pitch = 음높이만, 마디 경계가 틀려도 순서가 맞으면 인정.
정답은 `truth/<악보 이름>.txt`.
