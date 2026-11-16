<p align="center">
  <a href="https://github.com/Jongwan93/SingTogether/releases/latest"><img src="Resources/banner.svg" width="100%" alt="SingTogether — transpose any score to the key you can actually sing"></a>
</p>

<p align="center">
  <a href="https://github.com/Jongwan93/SingTogether/releases/latest"><img src="https://img.shields.io/github/v/release/Jongwan93/SingTogether?label=download&color=7c3aed&style=flat-square" alt="Download the latest release"></a>
  <img src="https://img.shields.io/badge/macOS%2014%2B-Apple%20Silicon-1e1b4b?style=flat-square&logo=apple&logoColor=white" alt="macOS 14 or later, Apple Silicon">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-PolyForm%20Strict%201.0.0-4f46e5?style=flat-square" alt="PolyForm Strict License 1.0.0"></a>
</p>

A macOS menu bar app that transposes sheet music to the key you can actually sing.
Drag a score, pick a key, and a transposed copy appears next to the original. The original is never touched.
Text chord sheets, MusicXML, and pictures of scores (web images, scans, PDFs) all work.

![Hold Shift, drag a score onto the ring, drop it on −2: Song (-2, F).png appears next to the original](docs/ring.gif)

```
Song.txt       →  drop on +2  →  Song (+2, A).txt
Song.musicxml  →  drop on −3  →  Song (-3, E).musicxml
Song.png       →  drop on −2  →  Song (-2, F).png   (notes, key signature and chords moved on the picture)
Song.pdf       →  drop on −2  →  Song (-2, F).png
Chart.jpg      →  drop on +2  →  Chart (+2, A).jpg   (same image, new chords)
```

## Install

**You need a Mac with Apple Silicon (M1 or later) and macOS 14 (Sonoma) or later.**
To see what you have, open the Apple menu and choose **About This Mac**. On macOS 12 or 13 the app does not open: macOS says it
needs a newer version. Every Apple Silicon Mac can be updated for free in **System Settings → General → Software Update**.
Intel Macs are not supported.

1. Download `SingTogether-<version>.dmg` from the [latest release](https://github.com/Jongwan93/SingTogether/releases/latest).
2. Open it and drag SingTogether to Applications.
3. The first time, macOS blocks the app because it is not notarized. Allow it once:
   - macOS 15 or later: double-click the app, click **Done**, then open **System Settings → Privacy & Security**,
     scroll down to *"SingTogether" was blocked* and click **Open Anyway**.
   - macOS 14: right-click the app and choose **Open**, then **Open** again.
4. A ♪ icon appears in the menu bar. Everything needed to read image scores is inside the app.
   If the app was opened from Downloads or the disk image, it offers to move itself to Applications.

macOS may ask once for permission to access the Desktop or Documents folder.

## How to use

1. Click the ♪ icon to check it is running.
2. In Finder, start dragging a score file and hold **Shift** (or ⌘⇧).
3. A ring with −5 … +5 appears around the cursor. For text and MusicXML files it also shows the key each slot gives you,
   and the center shows the change (`G → A`) while you hover.
4. Drop the file on a slot. The transposed copy is created in the same folder. Drop on the center to cancel.

For images and PDFs the app first has to find the notes and read the chords, which takes 10 seconds to a minute. The ring shows progress, and when it is done a review window opens (see below).

## Supported files

| File | What gets transposed | Accuracy |
|---|---|---|
| `.txt` `.cho` `.chordpro` `.crd` (chord sheets) | chord lines above lyrics, inline `[G]` chords, runs like `Bb9-Dm7-Eb-F`; spelling follows the new key (E major's `B/D#` becomes `C#/E#` in F♯) | exact |
| `.musicxml` `.xml` `.mxl` | every note, key signature, accidentals, chord symbols | exact (verified against music21 on 212 files) |
| `.png` `.jpg` `.webp` `.pdf` with staff notation | noteheads, key signature, accidentals and chord symbols moved on the original picture, everything else untouched (see [Image scores: moved in place](#image-scores-moved-in-place)) | chords 100% with an AI reader on 13 test scores; notes depend on image quality |
| `.png` `.jpg` `.webp` `.pdf` chord charts (chords + lyrics only) | chords rewritten in place on the original image | 97–100% with an AI reader, 80–96% without |

Nothing to try it on? [`samples/`](samples/) has three public-domain hymns as clean score images: Amazing Grace, Silent Night and Holy, Holy, Holy. Drop one on the ring.

## AI reader for chords and lyrics (optional)

Notes are always found on your Mac. Chords (and the lyrics of chord charts) are read much more accurately when an AI command-line tool is installed and signed in:

- **Claude:** [Claude Code](https://claude.com/claude-code) (`claude`), signed in with a Claude account.
- **ChatGPT:** OpenAI [Codex CLI](https://developers.openai.com/codex/cli) (`codex`), signed in with a ChatGPT account (`codex login`).
- **Gemini:** Google [Antigravity](https://antigravity.google) (`agy`), or the Gemini CLI (`gemini`) on a paid plan.

When the app starts it asks *Which AI should read your scores?* with a button for each, then checks that the one you picked is signed in.
If it is not, the app tells you how to sign in and uses Apple Vision alone until you do. Change the choice later with **Reading with … — Change…** in the ♪ menu or in Settings.
If the AI answers with something that does not match the image (a different song, made-up chords), the answer is discarded and Apple Vision is used for that score.

**Privacy:** with an AI reader, the score image is sent to that service (Anthropic, OpenAI or Google) under your own account.
With **Apple Vision only**, nothing leaves your Mac except the once-a-day version check described under [Updates](#updates-and-problems), which you can turn off.

## Settings

♪ menu → **Settings…** (⌘,):

- **Open SingTogether at login.**
- **Ask which AI to use when the app starts.** Turn it off once you have chosen.
- **Open the review window after reading an image score**, and **play a sound** when a file is created.
- **Check for updates when the app starts.**
- **Transposing:** when the new key can be written both ways (F♯ or G♭), follow the original (a ♯ score gets F♯, a ♭ score gets G♭), always sharps, or always flats. And whether the result of an image score is checked against the original (see below).
- **Reading image scores:** Claude, ChatGPT, Gemini, or Apple Vision only (see [AI reader](#ai-reader-for-chords-and-lyrics-optional)), with the sign-in status and a **Check sign-in** button.
- **Recognition cache:** how much space the remembered AI readings and chord charts take, and a button to clear it.

## Updates and problems

- **Updates:** once a day at start, the app asks GitHub for the latest release. If there is a newer one, an **Update to SingTogether …** item appears at the top of the ♪ menu and opens the download page; the app tells you once per new version. **Check for Updates…** in the ♪ menu asks right away. Nothing but the version request is sent.
- **Report a Problem…** in the ♪ menu opens a new GitHub issue with the app and macOS versions and the last recognizer log filled in. You see the exact text first, your scores are never included, and the same text is copied to the clipboard in case the page cuts it short. The log is also at `~/Library/Logs/SingTogether/recognizer.log`.

## Image scores: moved in place

For a picture of a single-line score (melody with chords and lyrics) the app does what you would do with correction fluid:
it finds every notehead on the staff and moves it up or down by the number of staff steps the new key needs, lengthens or
shortens the stem to match, replaces the key signature, recomputes the accidentals, and rewrites the chord symbols in the
same font. Slurs and ties move with their notes. Lyrics, repeat signs, the title and everything else stay exactly as they were. No rhythm is read,
so nothing can go wrong with measures or note lengths, and a mistake affects one note, not the whole score. The chord
symbols are read by the AI reader when one is signed in (10–20 seconds; Apple Vision alone takes a few seconds but misreads
superscript flats and numbers on blurry captures, A♭ as A6). On a small, blurry capture the key signature is too smudged to
count, so the app takes the key from the chord symbols, then from the AI's reading.

**Room is made where the new key needs it.** A longer key signature (one sharp becoming five) does not fit before the first
note, so that line is pushed a little to the right. Notes that rise into the chord symbols would run into them, so all the
chords on that line are lifted by the same amount, together with anything written above them (*al Coda*, ending brackets),
and blank rows are added above the line when it would touch the lyrics of the line before. Those rows are taken from the
bottom margin when there is one; otherwise the picture gets a little taller. In every other case the result has the same
size as the original.

**The result is checked before you see it.** The app compares the result with the original: a note still sitting at its
old position is a note the detector missed, so it is added and the score is moved again. Places it fixed get a thin green
box in the review window. The check takes a few seconds. Turn it off in Settings → Transposing → **Check the result
against the original and fix mistakes**.

**You can mark what is still wrong.** Click a place on the result in the review window, or drag a box over several notes,
and say what is wrong there: type the chord it should be, move the notes up or down a step, say they were not moved, or say
that something moved as a note is a rest or a symbol. Or choose **Comment…** and write it in your own words ("this note
should be one step higher"); with an AI reader signed in, the AI looks at that place on the original and the result and
turns the comment into the change. Marks are blue circles or boxes; click one to remove it. **Fix N Marked Places** moves
the original again with your marks and replaces the result. The marks then disappear and the corrections are kept: mark
more places and fix again, and the earlier corrections are applied with the new ones. **Clear Marks** removes the marks
you have not fixed yet. To start over without any corrections, transpose the original again.

Measured on 13 Korean worship lead sheets (web captures and scans) with hand-checked chord positions: with Claude every
one of the 478 chord symbols is rewritten right and no other symbol is lost or added; with Apple Vision alone 327 of 478.
Notes the detector misses stay where they were (the check and the marks above are there for them). Limits: treble clef,
one melody line.

To try it without dragging, quit SingTogether and run `open -a SingTogether --args --sheet score.png -2`.

## Chord charts

Sheets that only have chords above lyrics (no staff) are not redrawn. The app keeps the original image and rewrites just the chord symbols:
it erases each chord, then writes the new one in the same place, colour and size, in the closest matching system font.
Title, lyrics, layout and watermarks stay exactly as they were. Hyphenated runs like `Bb9-Dm7-Eb-F` are rewritten as a whole.
If a chord could not be found in the image, the review window says so and marks the spot.
The font is guessed from the original letters, which works for sans-serif charts but often misses serif ones (Times, Georgia):
pick the right one from the **Font** menu in the review window and the image is redrawn. The same window's **Edit Text…** and
**Rebuild from Text** rewrite the chords on the image from the corrected text (chords above lyrics, as in a `.txt` chord sheet).
**Save as Truth…** saves the corrected chart as a text file.
A chart is read only once: results are cached by file content in `~/Library/Caches/SingTogether/`, so dropping the same image again is instant.

## Command line

Both tools ship inside the app:

```bash
APP=/Applications/SingTogether.app/Contents
$APP/MacOS/singtogether-transpose Song.musicxml -2                     # same rules as the app, any supported file
$APP/MacOS/singtogether-transpose Song.txt 4 --flats                   # Gb rather than F# when both fit (--sharps; or SINGTOGETHER_ENHARMONIC=flats)
$APP/Resources/ocr/bin/singtogether-ocr score.jpg --transpose -2           # notes and chords moved on the picture → score (-2, F).jpg
$APP/Resources/ocr/bin/singtogether-ocr chart.jpg --llm claude --transpose 2  # chord chart → image with new chords + .txt
```

Useful `singtogether-ocr` options:
`--llm claude|chatgpt|gemini|none|auto` to choose the AI for chords, `--no-audit` to skip the result check, `--fixes FILE` to apply marks (a JSON list of `{"kind": "chord|not_moved|not_a_note|shift|comment", "x", "y", "box", "text", "steps"}` in pixels of the original), `--check-llm claude|chatgpt|gemini` to report whether it is installed and signed in,
`--source IMAGE` with a chord-chart `.txt` to rewrite the chords on that image, `--chart-font NAME` to force the font,
`--debug DIR` to save an overlay image per pipeline stage, `--no-cache`.

## Limits

- Single-line melody (treble clef) with chords and lyrics is the target. Multiple voices and two-staff piano scores are not handled.
- Accuracy follows image quality. Upscaling the image does not help (tested); a better source file does.
- The disk image is built for Apple Silicon only. It is signed ad hoc, not notarized, so the first launch has to be allowed once (see Install).

## License

© 2026 Jongwan Hur. SingTogether is licensed under the [PolyForm Strict License 1.0.0](LICENSE): you may download and use it
free of charge for any noncommercial purpose, which includes personal use, worship and use by churches, schools and other
charitable organizations. You may not redistribute it, change it or build on it. For anything else, such as commercial use,
ask for permission by [opening an issue](https://github.com/Jongwan93/SingTogether/issues).

See [THIRD_PARTY.md](THIRD_PARTY.md) for the software by others that the app ships with, and their licenses.
