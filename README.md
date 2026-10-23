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
Info.plist              앱 정보 (메뉴바 전용 앱 설정)
build.sh                build/SingTogether.app 만들기
Package.swift           Xcode / SwiftPM용
```

## 지원 형식

| 형식 | 바뀌는 것 |
|---|---|
| `.txt` `.cho` `.chordpro` `.crd` | 가사 위 코드 줄, `[G]가사` 인라인 코드 |
| `.musicxml` `.xml` `.mxl` | 모든 음표, 조표, 임시표, 코드 기호 |

PDF·이미지 악보는 음표를 읽을 수 없어서 지원하지 않습니다.
MuseScore에서 MusicXML로 내보내면 쓸 수 있어요.

## 참고

- 링 가운데(원래 키 표시)에 놓으면 취소됩니다.
- ♯/♭ 표기는 새 키에 맞게 자동으로 고릅니다 (예: F → B♭, A → E).
- 데스크톱·문서 폴더의 파일은 처음 변환할 때 macOS가 폴더 접근 허용을 물어볼 수 있어요.
