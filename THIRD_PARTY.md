# Third-party software

SingTogether itself is © Jongwan Hur, licensed under the PolyForm Strict License 1.0.0 (see [LICENSE](LICENSE)).
The distributed app (`./dist.sh`) bundles the following packages. This file and LICENSE are in the app under
`Contents/Resources/`; the packages' own license texts are under
`Contents/Resources/python/lib/python3.13/site-packages/` (`*.dist-info/` and `cv2/`).

| Package | Used for | License |
|---|---|---|
| [OpenCV](https://opencv.org/) 4.14, built from source (see below) | image processing, reading and writing image files | Apache-2.0 |
| [NumPy](https://numpy.org/) | arrays | BSD-3-Clause |
| [Pillow](https://python-pillow.org/) | drawing chord letters in system fonts | MIT-CMU |
| [PyObjC](https://pyobjc.readthedocs.io/) (Vision, Quartz, Cocoa, CoreML) | Apple Vision text recognition; reading and writing PDF pages with macOS Quartz | MIT |
| [Python](https://www.python.org/) 3.13 (uv managed build) | runs the score reader | PSF-2.0 |

Nothing bundled is under the GPL, LGPL or AGPL.

## OpenCV

The OpenCV wheels on PyPI bundle FFmpeg built with `--enable-gpl` (with x264, x265 and other GPL libraries), so the app
does not use them. `tools/ocr/pyproject.toml` builds the same OpenCV version from its source with only the modules
core, imgproc and imgcodecs (plus features2d, flann and calib3d, which its build scripts need), without FFmpeg or any
other video code. `tools/check-opencv.py` verifies the build. Compiled into it, from the copies in the OpenCV source:

| Library | License |
|---|---|
| [libjpeg-turbo](https://libjpeg-turbo.org/) | IJG, BSD-3-Clause, Zlib |
| [libpng](http://www.libpng.org/) | libpng-2.0 |
| [libtiff](http://www.simplesystems.org/libtiff/) | libtiff |
| [libwebp](https://developers.google.com/speed/webp) | BSD-3-Clause |
| [zlib](https://zlib.net/) | Zlib |
| carotene (in the OpenCV source, NVIDIA) | BSD-3-Clause, notice below |
| [KleidiCV](https://gitlab.arm.com/kleidi/kleidicv) (Arm) | Apache-2.0 |

`cv2/LICENSE-3RD-PARTY.txt` comes from the opencv-python source package and also describes libraries that are in the
PyPI wheels but not in this build (FFmpeg, Qt, OpenEXR and others).

## Pillow

The Pillow wheel contains FreeType (FreeType License), HarfBuzz, libjpeg-turbo, libpng, libtiff, libwebp, libavif with
AOM and dav1d, OpenJPEG, Little CMS, Brotli, XZ, zlib-ng and libxcb, all under permissive licenses; their texts are in
`pillow-*.dist-info/licenses/LICENSE`.

## Acknowledgements and notices

This software is based in part on the work of the Independent JPEG Group.

Portions of this software are copyright © 2026 The FreeType Project (www.freetype.org). All rights reserved.

carotene:

```
Copyright (C) 2014-2015, NVIDIA Corporation, all rights reserved.
Third party copyrights are property of their respective owners.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

  * Redistributions of source code must retain the above copyright notice,
    this list of conditions and the following disclaimer.

  * Redistributions in binary form must reproduce the above copyright notice,
    this list of conditions and the following disclaimer in the documentation
    and/or other materials provided with the distribution.

  * Neither the names of the copyright holders nor the names of the contributors
    may be used to endorse or promote products derived from this software
    without specific prior written permission.

This software is provided by the copyright holders and contributors "as is" and
any express or implied warranties, including, but not limited to, the implied
warranties of merchantability and fitness for a particular purpose are disclaimed.
In no event shall copyright holders or contributors be liable for any direct,
indirect, incidental, special, exemplary, or consequential damages
(including, but not limited to, procurement of substitute goods or services;
loss of use, data, or profits; or business interruption) however caused
and on any theory of liability, whether in contract, strict liability,
or tort (including negligence or otherwise) arising in any way out of
the use of this software, even if advised of the possibility of such damage.
```

Not bundled: the AI readers (Claude Code, Codex CLI, Antigravity, Gemini CLI) are installed and signed in by the user.
macOS frameworks (Vision, Quartz, PDFKit, Accelerate) are part of the system.
