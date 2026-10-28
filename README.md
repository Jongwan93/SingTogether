# SingTogether

Finder에서 악보 파일을 드래그하는 중에 **Shift**(또는 ⌘⇧)를 누르면 커서 주위에 −5…+5 키 링이 뜹니다.
원하는 칸에 놓으면 변환된 파일이 원본과 같은 폴더에 생깁니다. 원본은 그대로 남아요.

```
Song.txt  →  +2 칸에 놓기  →  Song (+2, A).txt
```

## 빌드 / 실행

```bash
xcode-select --install     # 처음 한 번 (Command Line Tools가 없으면)
./build.sh
open build/SingTogether.app     # 메뉴 막대에 ♪ 아이콘이 생김
```

Xcode에서 편집하려면 `open Package.swift`.

로그인할 때 자동으로 켜려면: 시스템 설정 → 일반 → 로그인 항목 → SingTogether 추가.

## 폴더 구조

```
Sources/
  App/                  macOS 앱 부분
    main.swift            진입점
    AppDelegate.swift     메뉴바 아이콘, 드래그 감지, 드롭 처리
    Ring.swift            커서 주위 키 링 (그리기 + 드롭 받기)
  Transpose/            이조 로직 (UI와 무관)
    Converter.swift       형식 판별, 원래 키 읽기, 새 파일 만들기
    ChordText.swift       텍스트 코드 악보 이조
    MusicXML.swift        MusicXML 음표·조표·코드 이조
    SheetOCR.swift        이미지·PDF: tools/ocr 인식 도구 호출, 결과 PDF를 원본 옆에
    CompactText.swift     압축 텍스트 악보를 마디·음표 단위로 고치기 (클릭 수정)
  App/ReviewWindow.swift  인식 결과 확인 창
  App/NoteEditor.swift    그림 위 음표 클릭 → 수정 팝오버
tools/ocr/              악보 인식 (Python, 별도 README)
Info.plist              앱 정보 (메뉴바 전용 앱 설정)
build.sh                build/SingTogether.app 만들기
Package.swift           Xcode / SwiftPM용
```

## 이조 정확도 검증

MusicXML 경로는 계산이라 100%여야 한다. `tests/transpose_check.py`가 music21을 기준으로 확인한다.

```bash
./build.sh                                                  # singtogether-transpose
tools/ocr/.venv/bin/python tests/transpose_check.py --quick  # 조표·코드·가사 케이스 + 코퍼스 일부
tools/ocr/.venv/bin/python tests/transpose_check.py          # 전체 (Lilypond MusicXML 테스트 스위트 142 + 코퍼스 60 + OCR 출력)
.build/release/singtogether-transpose --selftest             # 텍스트 이조·클릭 수정 자체 검사 (XCTest 없이)
```

- 왕복: +n 뒤 −n 하면 원본과 음·코드가 같다
- 오라클: music21이 같은 조로 이조한 결과와 음·철자·조표·코드 근음/베이스가 같고, 가사·리듬은 원본 그대로다
- 결과는 `tests/transpose_report.md`. 하드 실패(음이 틀리거나 내용이 빠짐)가 있으면 종료 코드 1.

## 지원 형식

| 형식 | 바뀌는 것 |
|---|---|
| `.txt` `.cho` `.chordpro` `.crd` | 가사 위 코드 줄, `[G]가사` 인라인 코드, `Bb9-Dm7-Eb-F`처럼 붙임표로 이은 진행 |
| `.musicxml` `.xml` `.mxl` | 모든 음표, 조표, 임시표, 코드 기호 |
| `.png` `.jpg` `.webp` `.pdf` (이미지 악보) | 인식 → 이조 → 새 PDF. 코드 차트(코드+가사)는 `.txt` |

이미지·PDF 악보는 `tools/ocr`의 인식 도구가 있어야 합니다 (아래). 인식은 100%가 아니라서, 결과와 함께
**확인 창**이 뜹니다. 왼쪽은 원본 위에 의심스러운 곳 표시, 오른쪽은 이조된 악보(빨간 음표 = 확인 필요).
왼쪽 그림에서 음표를 **클릭하면** 한 칸 위·아래, ♯/♭, 길이, 쉼표, 삭제를 바로 고칠 수 있고 PDF가 다시 만들어집니다(초록 테두리 = 고친 것).
더 큰 수정은 "텍스트 고치기"로 압축 텍스트를 편집한 뒤 "고친 텍스트로 다시 만들기".

```bash
cd tools/ocr && uv sync            # 인식 도구 설치 (처음 한 번). Claude Code CLI가 있으면 글자 정확도가 올라갑니다
open build/SingTogether.app --args --sheet 악보.png -2    # 드래그 없이 바로 시험
```

앱은 `tools/ocr/.venv/bin/singtogether-ocr`를 저장소 안에서 찾습니다. 다른 곳에 있으면 `SINGTOGETHER_OCR` 환경 변수로 알려 주세요.

## 참고

- 링 가운데(원래 키 표시)에 놓으면 취소됩니다.
- ♯/♭ 표기는 새 키에 맞게 자동으로 고릅니다 (예: F → B♭, A → E). F♯/G♭처럼 둘 다 되는 조는 원래 악보가 쓰던 쪽을 따릅니다.
- 텍스트 악보의 원래 키는 첫 코드가 아니라 곡 전체 코드로 판별합니다 (C G D Em → G).
- 데스크톱·문서 폴더의 파일은 처음 변환할 때 macOS가 폴더 접근 허용을 물어볼 수 있어요.
