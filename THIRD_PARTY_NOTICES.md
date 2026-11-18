# Third-party notices

TG MCP itself is MIT-licensed (see [LICENSE](LICENSE)). The Windows executable from Releases bundles the
Python runtime and the libraries below. Versions are the ones pinned in [requirements.txt](requirements.txt);
license identifiers are taken from the installed packages' metadata.

## Direct dependencies

| Package | Version | License |
|---------|---------|---------|
| [Telethon](https://github.com/LonamiWebs/Telethon) | 1.45.0 | MIT |
| [cryptg](https://pypi.org/project/cryptg/) | 0.6.1 | CC0-1.0 |
| [python-socks](https://github.com/romis2012/python-socks) | 3.2.1 | Apache-2.0 |
| [mcp](https://github.com/modelcontextprotocol/python-sdk) (MCP Python SDK) | 1.30.0 | MIT |
| [uvicorn](https://github.com/encode/uvicorn) | 0.54.0 | BSD-3-Clause |
| [PySide6-Essentials / shiboken6](https://doc.qt.io/qtforpython-6/) (Qt for Python) | 6.12.0 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| [Pillow](https://github.com/python-pillow/Pillow) | 13.0.0 | MIT-CMU (HPND) |
| [qrcode](https://github.com/lincolnloop/python-qrcode) | 8.2 | BSD |
| [keyring](https://github.com/jaraco/keyring) | 25.7.0 | MIT |
| [pywin32](https://github.com/mhammond/pywin32) | 312 | PSF |

## Notable transitive dependencies

starlette (BSD-3-Clause), sse-starlette (BSD-3-Clause), httpx (BSD-3-Clause), httpx-sse (MIT), anyio (MIT),
h11 (MIT), pydantic / pydantic-core / pydantic-settings (MIT), jsonschema (MIT), PyJWT (MIT),
python-multipart (Apache-2.0), cryptography (Apache-2.0 OR BSD-3-Clause), cffi (MIT-0), pycparser (BSD-3-Clause),
pyaes (MIT), rsa (Apache-2.0), pyasn1 (BSD-2-Clause), pywin32-ctypes (BSD-3-Clause), jaraco.* and more-itertools
(MIT), certifi (MPL-2.0), idna (BSD-3-Clause), click (BSD-3-Clause), python-dotenv (BSD-3-Clause),
typing_extensions (PSF-2.0), opentelemetry-api (Apache-2.0), truststore (MIT), colorama (BSD).

Build tool only (not a runtime dependency): [PyInstaller](https://pyinstaller.org) 6.22.3 — GPL-2.0-or-later
with the bootloader exception, which explicitly allows distributing the produced executables under any license.

The CPython runtime included in the executable is distributed under the PSF License.

## Qt / PySide6 (LGPL-3.0)

The executable uses Qt 6 and Qt for Python (PySide6) under the GNU Lesser General Public License v3.0.
Qt is used unmodified, as dynamically loaded libraries: the one-file executable unpacks the Qt DLLs into a
temporary folder at start-up and loads them from there. To use the application with a different (for example
self-built) Qt / PySide6, build it from source with your own PySide6 installation — the full source code and the
build script [build_exe.ps1](build_exe.ps1) are in this repository. Qt source code is available at
<https://download.qt.io/official_releases/qt/> and PySide6 source at <https://code.qt.io/cgit/pyside/pyside-setup.git/>.
License texts: <https://www.gnu.org/licenses/lgpl-3.0.html>, <https://www.gnu.org/licenses/gpl-3.0.html>.

Full license texts of the bundled Python packages ship inside the respective wheels (their `*.dist-info`
folders) and are reproduced by their upstream projects at the links above.
