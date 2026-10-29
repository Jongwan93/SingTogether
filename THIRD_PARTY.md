# Third-party software

SingTogether itself is licensed under the GNU Affero General Public License v3.0 (see [LICENSE](LICENSE)).
The distributed app (`./dist.sh`) bundles the following packages. Their license texts ship inside the app under
`Contents/Resources/python/lib/python3.13/site-packages/*.dist-info/`.

| Package | Used for | License |
|---|---|---|
| [homr](https://github.com/liebharc/homr) and its model files | staff-score recognition (OMR) | AGPL-3.0 |
| [PyMuPDF](https://pymupdf.readthedocs.io/) | reading PDF pages | AGPL-3.0 |
| [Verovio](https://www.verovio.org/) | drawing the transposed score | LGPL-3.0 |
| [ONNX Runtime](https://onnxruntime.ai/) | running the homr models (CoreML) | MIT |
| [OpenCV](https://opencv.org/) (opencv-python-headless) | image processing | Apache-2.0 |
| [NumPy](https://numpy.org/) | arrays | BSD-3-Clause |
| [Pillow](https://python-pillow.org/) | image files | MIT-CMU |
| [PyObjC](https://pyobjc.readthedocs.io/) (Vision, Quartz) | Apple Vision text recognition, PDF rendering | MIT |
| [Python](https://www.python.org/) 3.13 (uv managed build) | runs the recognizer | PSF-2.0 |

Not bundled: the AI readers (Claude Code, Antigravity, Gemini CLI) are installed and signed in by the user;
Audiveris (AGPL-3.0) is only used by the evaluation script for comparison.
