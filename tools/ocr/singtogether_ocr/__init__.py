"""악보 이미지/PDF → MusicXML.

파이프라인: ingest(입력 처리) → layout(영역 분리) → engines(멜로디 인식)
         → text(코드·가사 OCR) → assemble(MusicXML 조립)
이조와 렌더링은 각각 Swift CLI(singtogether-transpose)와 verovio가 맡는다.
"""
