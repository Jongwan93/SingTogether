# SingTogether

노래 키가 너무 높거나 낮은 사람을 위한 맥 메뉴바 앱. 악보 파일을 원하는 키로 옮긴 **새 파일**을 만들어 준다. 원본은 건드리지 않는다.

Finder에서 악보 파일을 드래그하는 중에 **Shift**(또는 ⌘⇧)를 누르면 커서 주위에 −5…+5 키 링이 뜬다.
원하는 칸에 놓으면 변환된 파일이 원본과 같은 폴더에 생긴다.

```
Song.txt       →  +2 칸에 놓기  →  Song (+2, A).txt
Song.musicxml  →  −3 칸에 놓기  →  Song (-3, E).musicxml
Song.png       →  −2 칸에 놓기  →  Song (-2, F).pdf   + 확인 창
```

## 지원 형식

| 형식 | 바뀌는 것 | 정확도 |
|---|---|---|
| `.txt` `.cho` `.chordpro` `.crd` (코드 악보) | 가사 위 코드 줄, `[G]가사` 인라인 코드, `Bb9-Dm7-Eb-F` 진행 | 계산이라 100% |
| `.musicxml` `.xml` `.mxl` | 모든 음표, 조표, 임시표, 코드 기호 | 계산이라 100% (212파일 검증) |
| `.png` `.jpg` `.webp` `.pdf` 오선 악보 | 인식 → 이조 → 새 PDF | 인식에 달림. 아래 "이미지 악보" |
| `.png` `.jpg` `.webp` `.pdf` 코드 차트 (코드 + 가사만) | 인식 → 이조 → `.txt` | 코드 97~100% (Claude/Gemini CLI 있을 때) |

## 설치

```bash
xcode-select --install          # 처음 한 번 (Command Line Tools)
./build.sh                      # build/SingTogether.app, build/singtogether-transpose
open build/SingTogether.app     # 메뉴 막대에 ♪ 아이콘
```

이미지·PDF 악보까지 쓰려면 인식 도구(Python)를 한 번 설치한다.

```bash
cd tools/ocr && uv sync         # Python 3.13 가상환경 + homr(OMR) + Verovio
brew install librsvg            # PDF 출력 (rsvg-convert)
```

- Claude Code CLI(`claude`)가 로그인돼 있으면 코드·가사 글자를 그걸로 읽어 정확도가 크게 오른다. 없으면 로컬(Apple Vision)로 돈다.
- 앱은 `tools/ocr/.venv/bin/singtogether-ocr`를 저장소 안에서 찾는다. 다른 곳에 있으면 환경 변수 `SINGTOGETHER_OCR`로 알려 준다.
- 로그인할 때 자동으로 켜려면: 시스템 설정 → 일반 → 로그인 항목 → SingTogether 추가.
- Xcode에서 편집하려면 `open Package.swift`.

## 이미지 악보: 인식 → 이조 → 확인

이미지를 링에 놓으면 링에 "읽는 중"이 뜨고(10초~1분), 끝나면 원본 옆에 PDF가 생기면서 **확인 창**이 열린다.

- 왼쪽: 원본 위에 의심스러운 곳 표시 (음표 빨강, 코드 주황, 가사 보라, 마디 번호와 읽은 음).
- 오른쪽: 이조된 악보. 의심스러운 음표는 빨간색.
- 왼쪽 그림에서 **음표를 클릭**하면 한 칸 위·아래, ♯/♭, 길이, 쉼표, 삭제를 바로 고칠 수 있고 PDF가 다시 만들어진다 (초록 = 고친 것).
- 더 큰 수정은 "텍스트 고치기"(압축 텍스트를 편집기로 열기) → "고친 텍스트로 다시 만들기".
- 같은 악보는 다시 인식하지 않는다 (`~/Library/Caches/SingTogether/ocr/`에 내용 해시로 캐시). 중간 파일은 `~/Library/Caches/SingTogether/work/<이름>/`.

드래그 없이 바로 시험: `open build/SingTogether.app --args --sheet 악보.png -2`

### 지금 정확도 (정답 3곡 기준, 2026-10)

| 입력 | 음높이 | 음높이+리듬 | 코드 | 가사 |
|---|---|---|---|---|
| 웹에서 저장한 캡처 (600~800px) | 64~82% | 38~74% | 98~100% | 77~82% |
| 깨끗한 PDF·고해상도 스캔 | 93~100% | 86~100% | – | – |

- 교회 찬양팀이 쓰는 악보는 대부분 구글 이미지 캡처라 음높이 100%는 안 된다. 그래서 확인 창이 있다. 확인 표시는 실제 오류의 72~97%를 잡고,
  표시한 것의 39~74%가 실제 오류다 (곡마다 다름).
- 깨끗한 입력이면 엔진(homr)이 100% 읽는다. 사이트에서 받은 원본 PDF가 있으면 그걸 쓰는 게 가장 좋다.
- 코드 차트(오선 없이 코드 줄 + 가사 줄)는 Claude나 Gemini CLI가 있으면 사실상 다 맞고, 로컬(Vision)만으로는 80~96%.

## 명령줄 도구

```bash
.build/release/singtogether-transpose Song.musicxml -2          # 어떤 형식이든 앱과 같은 규칙으로 변환
.build/release/singtogether-transpose --selftest                # 텍스트 이조·클릭 수정 자체 검사

tools/ocr/.venv/bin/singtogether-ocr score.jpg                  # → score.musicxml, score.txt, score.review.json, score-p1.review.png
tools/ocr/.venv/bin/singtogether-ocr score.jpg --engine hybrid --transpose -2 --render   # + 이조본, PDF
tools/ocr/.venv/bin/singtogether-ocr chart.jpg --llm claude --transpose 2               # 코드 차트 → .txt
tools/ocr/.venv/bin/singtogether-ocr score.txt --transpose -2 --render                  # 고친 텍스트 → MusicXML → 이조 → PDF
```

`singtogether-ocr` 주요 옵션: `--engine homr|hybrid|claude|auto` (hybrid = 음표는 homr + 오선 위치, 글자는 Claude CLI; auto = claude가 있으면 hybrid),
`--llm claude|gemini|none|auto` (코드 차트 글자), `--debug DIR` (단계별 오버레이), `--no-mask` (엔진에 주기 전 글자 지우기 생략),
`--no-cache`, `--json` (앱 연동용 결과 한 줄). 모델은 `SINGTOGETHER_CLAUDE_MODEL` (기본 sonnet).

인식 파이프라인: 보정·확대·이진화 → 오선·코드 띠·가사 띠 찾기 → 코드·가사 글자를 지운 이미지를 homr에 → 음표 머리·마디줄 좌표로 마디 배정과
음높이 보정 → Vision(+Claude) 코드·가사 → 코드로 조표 검산 → MusicXML → Swift 이조 → Verovio 렌더링.

### 압축 텍스트 형식

인식 결과 `.txt`, 정답 파일, 클릭 수정이 모두 이 형식이다. 사람이 읽고 고치기 쉽다.

```
title: 주님의 임재 앞에서
key: G major
time: 4/4
1: r/8 [G] G4/8 G4/8 G4/8 [D/F#] F#4/8 F#4/8 F#4/8 E4/8~ | 주 님 의 임 재 앞 에 _
2: [C2] E4/8 D4/8 D4/2. | _ 서 _
```

음표 `<음이름><#|b><옥타브>/<길이>[.][~]` (C4 = 가온 다, `~` = 붙임줄), 쉼표 `r/8`, 코드 `[G]`, 도돌이 `{: :}`, 괄호 `{1 {2`,
`|` 뒤에 절별 가사 (쉼표를 뺀 음표마다 한 음절, `_` = 새 음절 없음).

## 검증

```bash
tools/ocr/.venv/bin/python tests/transpose_check.py --quick   # MusicXML 이조를 music21과 대조 (왕복 + 오라클). 전체는 --quick 없이
.build/release/singtogether-transpose --selftest              # 텍스트 이조·클릭 수정
cd tools/ocr
.venv/bin/singtogether-eval --engines homr hybrid             # 정답(truth/*.txt)이 있는 악보로 인식 정확도 → out/report.md
.venv/bin/singtogether-eval --rendered                        # 정답을 Verovio로 그려 넣는 상한·조립 단계 회귀 검사
.venv/bin/singtogether-eval --chordsheets --llm none          # 코드 차트 (truth/chordsheets)
```

악보 샘플은 `music_scores/korean`, `music_scores/english`에 두고(저작물이라 저장소에는 없음), 정답은 `tools/ocr/truth/<악보 이름>.txt`.

## 폴더 구조

```
Sources/
  App/                     macOS 앱
    main.swift               진입점
    AppDelegate.swift        메뉴바 아이콘, 드래그 감지, 드롭 처리, 인식 진행
    Ring.swift               커서 주위 키 링
    ReviewWindow.swift       인식 결과 확인 창
    NoteEditor.swift         그림 위 음표 클릭 → 수정 팝오버
  Transpose/               이조 로직 (UI와 무관, 앱·CLI 공용)
    Converter.swift          형식 판별, 원래 키 읽기, 새 파일 만들기
    ChordText.swift          텍스트 코드 악보 이조
    MusicXML.swift           MusicXML 음표·조표·코드 이조
    SheetOCR.swift           이미지·PDF: tools/ocr 호출, 결과 PDF를 원본 옆에
    CompactText.swift        압축 텍스트를 마디·음표 단위로 고치기
  TransposeCLI/            singtogether-transpose
tools/ocr/                 악보 인식 (Python, uv)
  singtogether_ocr/          ingest → layout → engines(homr/claude) → notes → text·lyrics → assemble·key → review → musicxml_out·render
  truth/                     정답 파일
tests/transpose_check.py   MusicXML 이조 검증
build.sh                   build/SingTogether.app 만들기
```

## 알려진 한계 · 다음 할 일

- 이미지 악보 인식은 입력 화질에 달렸다. 업스케일링(Real-ESRGAN)은 시험 결과 도움이 안 됐다. 붙임줄이 붙은 2분음표 머리는 자주 놓친다.
- 온음표(기둥 없음), 셋잇단, 여러 성부, 피아노 2단 악보는 지원하지 않는다. 단선 멜로디 + 코드 + 가사가 대상이다.
- 앱은 저장소 안의 Python 환경과 brew의 `rsvg-convert`에 기댄다. 다른 맥에 나눠 주려면 이를 앱에 묶는 패키징이 필요하다.
- 확인 창에서 코드·가사는 아직 텍스트 편집으로만 고친다.

## 참고

- 링 가운데(원래 키 표시)에 놓으면 취소된다.
- ♯/♭ 표기는 새 키에 맞게 고른다 (F → B♭, A → E). F♯/G♭처럼 둘 다 되는 조는 원래 악보가 쓰던 쪽.
- 텍스트 악보의 원래 키는 곡 전체 코드로 판별한다 (C G D Em → G).
- 데스크톱·문서 폴더의 파일은 처음 변환할 때 macOS가 폴더 접근 허용을 물어볼 수 있다.
