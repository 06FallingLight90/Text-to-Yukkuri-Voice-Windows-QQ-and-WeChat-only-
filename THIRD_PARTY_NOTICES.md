# Third-Party Notices

YouKuLiChaSpeak is distributed under the MIT License (see `LICENSE`). It builds
on the work listed below; every component keeps its own copyright and license.

Full license texts for the components whose terms require them to be shipped
alongside are in [`licenses/`](licenses/):

| File | Covers |
| --- | --- |
| [`licenses/AEVEC-wechat-tts-voice-bubble.txt`](licenses/AEVEC-wechat-tts-voice-bubble.txt) | the derived work in `app.pyw` |
| [`licenses/XIAYM-gh-yukkuri-mandarin-js.txt`](licenses/XIAYM-gh-yukkuri-mandarin-js.txt) | the vendored Chinese front-end in `synth/vendor/` |
| [`licenses/Apache-2.0.txt`](licenses/Apache-2.0.txt) | the Material Symbols icons in `assets/material_symbols/` |

Components consumed as npm dependencies are **not** redistributed here; their
license identifiers are listed below and their texts ship with the packages
themselves.

## Derived work

| Component | License | How it is used |
| --- | --- | --- |
| [AEVEC/wechat-tts-voice-bubble](https://github.com/AEVEC/wechat-tts-voice-bubble) | MIT | `app.pyw` is adapted from this project's widget; `audio.py` / `wechat.py` reimplement its device-selection, VB-CABLE playback and WeChat UI-automation mechanisms; and `assets/app-icon.ico` / `app-icon.png` are taken from it verbatim. Its HTTP TTS call was replaced with the local engine. |

## Synthesis engine and text front-ends

| Component | Version | License | Notes |
| --- | --- | --- | --- |
| [y52en/aquestalk.js](https://github.com/y52en/aquestalk.js) | 1.0.7 | MIT | Runs the AquesTalk Win32 engine inside a v86 x86 emulator compiled to WebAssembly. Declared as an npm dependency; **not** vendored or copied into this repository. |
| [y52en/AqKanji2Koe-OpenJTalk-WASM](https://github.com/y52en/AqKanji2Koe-OpenJTalk-WASM) | 0.1.0 | MIT | Published as `kanji2koe-openjtalk`. Independent Rust/WebAssembly reimplementation of AQUEST's AqKanji2Koe front-end: converts kanji/kana Japanese into AquesTalk phonetic notation. Uses [jpreprocess](https://github.com/jpreprocess/jpreprocess) and NAIST-JDic. |
| [XIAYM-gh/yukkuri-mandarin-js](https://github.com/XIAYM-gh/yukkuri-mandarin-js) | — | MIT | Vendored under `synth/vendor/yukkuri-mandarin/`. JavaScript port of [wubzbz/Yukkuri-Mandarin](https://github.com/wubzbz/Yukkuri-Mandarin) (MIT): converts Mandarin pinyin plus tone into kana with Japanese pitch-accent marks. |
| [pinyin-pro](https://github.com/zh-lx/pinyin-pro) | 3.29.4 | MIT | Hanzi to pinyin with tone numbers. |
| [v86](https://github.com/copy/v86) | 0.5.460 | BSD-2-Clause | x86 emulator; transitive dependency of `aquestalk.js`. |
| [jszip](https://github.com/Stuk/jszip) | 3.10.2 | MIT or GPL-3.0-or-later | Reads the voice-bank archives; transitive dependency of `aquestalk.js`. |
| [encoding-japanese](https://github.com/polygonplanet/encoding-japanese) | 2.3.0 | MIT | Shift_JIS handling; transitive dependency of `aquestalk.js`. |

## ⚠️ AQUEST engine licensing — read before redistributing

The yukkuri voices themselves are **not** open source. `aquestalk.js` embeds the
AQUEST Win32 AquesTalk engine and its voice banks, and npm delivers them to
`node_modules/` when you install. This repository therefore declares
`aquestalk.js` as a dependency and does **not** commit or bundle any AQUEST
binary or voice data.

AQUEST's own terms ([licence](https://www.a-quest.com/licence.html),
[products](https://www.a-quest.com/products/aquestalk.html)) state, among other
things:

- Non-commercial personal use is free of charge under certain conditions.
- Audio **you generate** may be published or sold for commercial or
  non-commercial purposes.
- **Serving synthesis over the internet is not permitted** without a separate
  server licence (use inside a closed network is allowed).
- Copying beyond the licensed count, redistribution, modification — including
  renaming files — and reverse engineering are prohibited.
- Distributing an application that embeds the library requires a distribution
  licence (頒布ライセンス) from AQUEST.

**Practical consequence for this project:** keep it a local, single-machine tool.
Do not turn it into a hosted service, and do not repackage AQUEST binaries or
voice banks into your own release archive. If you intend to ship this to other
people, either have them install the dependencies themselves (as this repository
does) or obtain the appropriate licence from AQUEST.

## Bundled assets

| Component | License |
| --- | --- |
| `assets/material_symbols/*.svg` — Material Symbols Rounded icons from [google/material-design-icons](https://github.com/google/material-design-icons) | Apache-2.0 |
| `assets/app-icon.ico`, `assets/app-icon.png` — a flat green speech bubble with a waveform, taken from AEVEC/wechat-tts-voice-bubble | MIT (see `licenses/AEVEC-wechat-tts-voice-bubble.txt`) |

The app icon is a generic geometric graphic: no third-party illustration,
character art or photographic material is involved.

## Not included

**VB-CABLE** by VB-Audio is required at runtime but is not part of this
repository or its release archive. Users install it separately from
[VB-Audio](https://vb-audio.com/Cable/) and it remains subject to VB-Audio's own
terms.

## Python dependencies

| Component | License |
| --- | --- |
| customtkinter | MIT |
| affine | BSD-3-Clause |
| NumPy | BSD-3-Clause |
| psutil | BSD-3-Clause |
| PyAutoGUI | BSD-3-Clause |
| resvg Python bindings | MIT |
| python-sounddevice | MIT |
| python-soundfile | BSD-3-Clause |
| Pillow | MIT-CMU |

Transitive dependencies and bundled native libraries retain their own terms;
refer to their upstream distributions for the complete license texts.
