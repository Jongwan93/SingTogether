# SingTogether

A macOS menu bar app that transposes sheet music to the key you can actually sing.
Drag a score, pick a key, and a transposed copy appears next to the original. The original is never touched.

```
Song.txt       →  drop on +2  →  Song (+2, A).txt
Song.musicxml  →  drop on −3  →  Song (-3, E).musicxml
Song.png       →  drop on −2  →  Song (-2, F).pdf   (+ a review window)
Chart.jpg      →  drop on +2  →  Chart (+2, A).jpg   (same image, new chords)
```

## How to use

1. Launch SingTogether. A ♪ icon appears in the menu bar.
2. In Finder, start dragging a score file and hold **Shift** (or ⌘⇧).
3. A ring with −5 … +5 appears around the cursor. For text and MusicXML files it also shows the key each slot gives you.
4. Drop the file on a slot. The transposed copy is created in the same folder. Drop on the center to cancel.

For images and PDFs the app first has to *read* the score, which takes 10 seconds to a minute. The ring shows progress, and when it is done a review window opens (see below).

## Supported files

| File | What gets transposed | Accuracy |
|---|---|---|
| `.txt` `.cho` `.chordpro` `.crd` (chord sheets) | chord lines above lyrics, inline `[G]` chords, runs like `Bb9-Dm7-Eb-F`; spelling follows the new key (E major's `B/D#` becomes `C#/E#` in F♯) | exact |
| `.musicxml` `.xml` `.mxl` | every note, key signature, accidentals, chord symbols | exact (verified against music21 on 212 files) |
| `.png` `.jpg` `.webp` `.pdf` with staff notation | recognized, transposed, rendered to a new PDF | depends on image quality, see below |
| `.png` `.jpg` `.webp` `.pdf` chord charts (chords + lyrics only) | chords rewritten in place on the original image | 97–100% with an AI CLI, 80–96% without |

## Install

**From the disk image** (no developer tools needed, Apple Silicon Macs):

1. Open `SingTogether-1.0.dmg` and drag SingTogether to Applications.
2. The first time, right-click the app and choose **Open** (the build is not notarized, so macOS asks once).
3. A ♪ icon appears in the menu bar. Everything needed to read image scores is inside the app.

**From source:**

```bash
xcode-select --install          # once, if you don't have the Command Line Tools
./build.sh                      # builds build/SingTogether.app and build/singtogether-transpose
open build/SingTogether.app
```

A source build reads image and PDF scores through the Python package in `tools/ocr`. Install it once:

```bash
cd tools/ocr && uv sync         # Python 3.13 environment with homr (OMR) and Verovio
```

- Chords and lyrics are read much more accurately with an AI command-line tool that is installed and logged in on the Mac:
  [Claude Code](https://claude.com/claude-code) (`claude`) or Google [Antigravity](https://antigravity.google) (`agy`).
  When the app starts it asks *Which AI should read your scores?* with a button for each, then checks that the one you picked is signed in.
  If it is not, the app tells you how to sign in and uses Apple Vision alone until you do. Change the choice later with **Reading with … — Change…** in the ♪ menu.
  If the AI answers with something that does not match the image (a different song, made-up chords), the answer is discarded and Apple Vision is used for that score.
- A source build looks for `tools/ocr/.venv/bin/singtogether-ocr` inside this repository. If it lives elsewhere, point the `SINGTOGETHER_OCR` environment variable at it.
- macOS may ask once for permission to access the Desktop or Documents folder.

## Settings

♪ menu → **Settings…** (⌘,):

- **Open SingTogether at login.**
- **Ask which AI to use when the app starts.** Turn it off once you have chosen.
- **Open the review window after reading an image score**, and **play a sound** when a file is created.
- **Reading image scores:** Claude, Gemini, or Apple Vision only, with the sign-in status and a **Check sign-in** button.
- **Recognition cache:** how much space the remembered results take, and a button to clear it.

## Image scores: recognize, transpose, review

Recognition is never perfect, so every image score opens a review window:

- **Left:** the original with suspicious spots marked. Red boxes are notes, orange are chords, purple are lyric syllables. Each note is labeled with its measure number and what was read.
- **Right:** the transposed score. Notes that need checking are drawn in red.
- **Click a note** on the left to fix it on the spot: a step up or down, ♯/♭, length, turn into a rest, delete, or type the chord that sits over it. The PDF is rebuilt right away. Fixed items get a green outline.
- **Click a chord or a lyric syllable** (the orange and purple boxes, but any chord or syllable works) to retype it or remove it.
- **Undo** (⌘Z or the button) takes back the last click-fix.
- For bigger changes use **Edit Text…**, which opens the score as plain text (format below), then **Rebuild from Text**.
- A score is recognized only once. Results are cached by file content in `~/Library/Caches/SingTogether/`, so dropping the same image again is instant.

To try it without dragging: `open build/SingTogether.app --args --sheet score.png -2`

### Chord charts

Sheets that only have chords above lyrics (no staff) are not redrawn. The app keeps the original image and rewrites just the chord symbols:
it erases each chord, then writes the new one in the same place, colour and size, in the closest matching system font.
Title, lyrics, layout and watermarks stay exactly as they were. Hyphenated runs like `Bb9-Dm7-Eb-F` are rewritten as a whole.
If a chord could not be found in the image, the review window says so and marks the spot.
The font is guessed from the original letters, which works for sans-serif charts but often misses serif ones (Times, Georgia):
pick the right one from the **Font** menu in the review window and the image is redrawn. The same window's **Edit Text…** and
**Rebuild from Text** work for chord charts too, rewriting the chords on the image.

### What to expect

Measured on three Korean worship lead sheets with hand-checked ground truth:

| Input | Pitch | Pitch + rhythm | Chords | Lyrics |
|---|---|---|---|---|
| Image saved from a web search (600–800 px) | 64–82% | 38–74% | 98–100% | 77–82% |
| Clean PDF or high-resolution scan | 93–100% | 86–100% | – | – |

Most church teams use web images, so expect to fix some notes. The review marks catch 72–97% of the real errors.
If you can get the original PDF from the publisher, use that: the engine reads clean input almost perfectly.

### The text format

Recognized scores are also written as a `.txt` you can read and edit. Chord charts use the same idea with chords above lyrics.

```
title: Amazing Grace
key: G major
time: 3/4
1: D4/4 | A
2: [G] G4/2 B4/8 G4/8 | maz ing _
3: B4/2 A4/4 | grace how
4: [C] G4/2 E4/4 | sweet the
5: [G] D4/2 D4/4 | sound that
```

Notes are `<name><#|b><octave>/<length>[.][~]` (C4 is middle C, `~` ties to the next note), rests are `r/8`, chords are `[G]`,
repeats are `{: :}`, endings are `{1 {2`. After `|` come the lyrics, one syllable per note (rests skipped), `_` meaning no new syllable.
Edit the file, then drop it on the ring or run `singtogether-ocr fixed.txt --transpose -2 --render`.

## Command line

```bash
.build/release/singtogether-transpose Song.musicxml -2            # same rules as the app, any supported file
tools/ocr/.venv/bin/singtogether-ocr score.jpg                     # → score.musicxml, score.txt, score.review.json, score-p1.review.png
tools/ocr/.venv/bin/singtogether-ocr score.jpg --transpose -2 --render     # + transposed copy and PDFs
tools/ocr/.venv/bin/singtogether-ocr chart.jpg --llm claude --transpose 2  # chord chart → image with new chords + .txt
```

Useful `singtogether-ocr` options: `--engine homr|hybrid|auto` (hybrid reads notes locally and text with the AI CLI; auto picks hybrid when one is available),
`--llm claude|gemini|none|auto` to choose the AI for chords and lyrics, `--check-llm claude|gemini` to report whether it is installed and signed in,
`--source IMAGE` with a chord-chart `.txt` to rewrite the chords on that image, `--chart-font NAME` to force the font,
`--debug DIR` to save an overlay image per pipeline stage, `--no-cache`.

## Limits

- Single-line melody with chords and lyrics is the target. Whole notes, tuplets, multiple voices and two-staff piano scores are not handled.
- Accuracy follows image quality. Upscaling the image does not help (tested); a better source file does.
- The disk image is built for Apple Silicon only. It is signed ad hoc, not notarized, so the first launch needs right-click → Open.

## Development

```
Sources/App/        menu bar app: ring, drag detection, review window, note editor
Sources/Transpose/  transposition logic shared by app and CLI, plus the bridge to the recognizer
Sources/TransposeCLI/  singtogether-transpose
tools/ocr/          recognizer (Python): ingest → layout → OMR → chords/lyrics OCR → assemble → review → MusicXML → render
tools/ocr/truth/    hand-checked ground truth for the evaluation
```

```bash
./dist.sh                                               # self-contained dist/SingTogether.app and dist/SingTogether-1.0.dmg (needs uv)
.build/release/singtogether-transpose --selftest        # text transposition and note editing checks
cd tools/ocr
.venv/bin/singtogether-eval --engines homr hybrid       # recognition accuracy against truth/ → out/report.md
.venv/bin/singtogether-eval --rendered                  # clean-render regression test for the assembly stage
.venv/bin/singtogether-eval --chordsheets --llm none    # chord charts
```

Sample scores go in `music_scores/korean` and `music_scores/english` (not in the repository, they are copyrighted).

`dist.sh` copies a relocatable Python 3.13 (uv's managed build) into the app, installs `tools/ocr` into it, bundles homr's model files,
and signs everything. With `DEVELOPER_ID="Developer ID Application: …"` and `NOTARY_PROFILE=<notarytool keychain profile>` set,
it signs with that identity and notarizes the disk image. SVG to PDF rendering uses WebKit through `singtogether-transpose --svg-to-pdf`,
so no Homebrew packages are needed at run time.
