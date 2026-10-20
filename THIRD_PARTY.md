# Third-party components and licences

AstroFlow is built on open-source software and open data. This file lists everything it uses, what for, and
under which licence. Nothing here requires an internet connection while observing.

## 1. Installed with AstroFlow (Python packages)

The installer downloads these from the Python Package Index (PyPI) into AstroFlow's own folder.

| Package | Used for | Licence |
|---|---|---|
| [Python](https://www.python.org) (embeddable distribution) | runs AstroFlow | PSF License 2.0 |
| [NumPy](https://numpy.org) | arrays, all image maths | BSD-3-Clause |
| [SciPy](https://scipy.org) | filters, interpolation, fitting | BSD-3-Clause |
| [OpenCV](https://opencv.org) (`opencv-python-headless`) | debayering, warping, registration helpers, image I/O | Apache-2.0 (OpenCV); MIT (Python packaging) |
| [Astropy](https://www.astropy.org) (+ `pyerfa`, `astropy-iers-data`) | FITS files, WCS, offline planet/Moon positions | BSD-3-Clause (ERFA: BSD-3-Clause) |
| [FastAPI](https://fastapi.tiangolo.com), [Starlette](https://www.starlette.io), [Pydantic](https://docs.pydantic.dev) | the local web API | MIT, BSD-3-Clause, MIT |
| [Uvicorn](https://www.uvicorn.org) with `websockets`, `httptools`, `watchfiles` | the local web server | BSD-3-Clause, BSD-3-Clause, MIT, MIT |
| [python-multipart](https://github.com/Kludex/python-multipart) | file uploads from a second computer | Apache-2.0 |
| [Pillow](https://python-pillow.org) | PNG/JPEG/WebP, ICC profiles, watermark and caption drawing | MIT-CMU (HPND) |
| [tifffile](https://github.com/cgohlke/tifffile), [imagecodecs](https://github.com/cgohlke/imagecodecs) | 16/32-bit TIFF read/write, compressed TIFFs | BSD-3-Clause |
| [psutil](https://github.com/giampaolo/psutil) | memory and disk-space checks | BSD-3-Clause |
| [timezonefinder](https://github.com/jannikmi/timezonefinder) (+ `h3`) | time zone from latitude/longitude, offline | MIT (h3: Apache-2.0) |
| [tzdata](https://github.com/python/tzdata) | time-zone database on Windows | Apache-2.0 |
| [Send2Trash](https://github.com/arsenetar/send2trash) | moving files to the Recycle Bin instead of deleting | BSD-3-Clause |
| [pySerial](https://github.com/pyserial/pyserial) | USB/serial connection to the mount or hand controller | BSD-3-Clause |
| [rawpy](https://github.com/letmaik/rawpy) (LibRaw) | reading DSLR/mirrorless RAW files | MIT (rawpy); LibRaw: LGPL-2.1 or CDDL-1.0 |
| [pywin32](https://github.com/mhammond/pywin32) (Windows only) | ASCOM drivers through COM | PSF-2.0 |

Optional (only when you choose them):

| Package | Used for | Licence |
|---|---|---|
| [CuPy](https://cupy.dev) (`cupy-cuda12x`) | NVIDIA GPU acceleration | MIT (the CUDA libraries it loads are under NVIDIA's licence) |
| [pywebview](https://pywebview.flowrl.com) | native desktop window instead of the Edge app window | BSD-3-Clause |
| [lz4](https://github.com/python-lz4/python-lz4) | XISF files with LZ4 compression | BSD-3-Clause |

For development only: pytest (MIT), httpx (BSD-3-Clause), Playwright (Apache-2.0).

## 2. Data shipped with AstroFlow (`data/`)

Built with `tools/build_data.py` from these sources. The share-alike licences apply to the data files
themselves.

| Data | Source | Licence / credit |
|---|---|---|
| `dso.csv.gz` — 13,372 galaxies, nebulae and clusters (NGC, IC, Messier, Caldwell, common names) | [OpenNGC](https://github.com/mattiaverga/OpenNGC) by Mattia Verga (incl. its addendum); Caldwell numbers from the HYG project's `dso.csv` | CC BY-SA 4.0 |
| `stars.csv.gz` — 83,479 stars to magnitude 9, 431 proper names | [HYG Database](https://github.com/astronexus/HYG-Database) v4.1 by David Nash (astronexus) | CC BY-SA 4.0 |
| `cities.csv.gz` — 171,094 towns and cities for the offline location search | [GeoNames](https://www.geonames.org) (cities with more than 1000 inhabitants) via `lutangar/cities.json` | CC BY 4.0 — "Contains data from GeoNames.org" |

## 3. Downloaded only when you ask (never bundled)

| Component | What | Licence | How AstroFlow uses it |
|---|---|---|---|
| [ASTAP](https://www.hnsky.org/astap.htm) command-line solver by Han Kleijn ([SourceForge](https://sourceforge.net/projects/astap-program/)) | offline plate solving | Mozilla Public License 2.0 (program); star databases free to use | downloaded by the setup wizard/Settings from SourceForge into AstroFlow's data folder and run as a separate program |
| ASTAP star databases (D05/D20/D50/D80, G05, W08) | star catalogues for solving | free to use; derived from ESA Gaia data | "This work has made use of data from the European Space Agency (ESA) mission Gaia (https://www.cosmos.esa.int/gaia), processed by the Gaia Data Processing and Analysis Consortium (DPAC, https://www.cosmos.esa.int/web/gaia/dpac/consortium)." |
| Player One Camera SDK (`PlayerOneCamera.dll`) | controlling Player One cameras | proprietary, by Player One Astronomy — **not redistributed** | found where SharpCap/N.I.N.A./FireCapture installed it, or installed by you from Player One's free SDK zip |
| ASCOM Platform and drivers (e.g. Sky-Watcher's SynScan App driver) | optional camera/mount drivers on Windows | their own licences — **not redistributed** | used through COM when you choose an ASCOM device |
| Sky-Watcher SynScan app | optional mount connection route | proprietary, by Sky-Watcher — **not redistributed** | AstroFlow talks to it over the network |
| [Astrometry.net](https://nova.astrometry.net) web service | optional online plate solving | service terms of Astrometry.net | off by default; only the image being solved is uploaded, marked private |
| Microsoft Edge / WebView | shows AstroFlow's window | part of Windows | started in "app" mode with its own profile |

## 4. Not included

AstroFlow does **not** include, call or require AutoStakkert!, RegiStax, SharpCap, PixInsight, DeepSkyStacker,
Siril or any other closed-source or separately licensed astronomy program. Techniques that those programs made
popular — lucky imaging with alignment points, wavelet sharpening, sigma-clipped integration — are long
published methods and are implemented independently in AstroFlow's own code.

## 5. Published references used while building AstroFlow

* Sky-Watcher motor controller command set (protocol description):
  https://inter-static.skywatcher.com/downloads/skywatcher_motor_controller_command_set.pdf and
  https://github.com/skywatcher-pacific/skywatcher_open
* pysynscan (open-source implementation of the SynScan UDP protocol, reference only, no code copied):
  https://github.com/nachoplus/pysynscan
* SynScan app network port 11882 (community findings):
  https://support.simulationcurriculum.com/hc/en-us/community/posts/115009332408
* Player One SDK structure layout (C# declarations used to check the `ctypes` structures):
  https://svn.micro-manager.org/3rdpartypublic/PlayerOne/CameraSDK/include/PlayerOneCameraDLL.cs
* SER file format specification (Grischa Hahn / Heiko Wilkens, version 3).
* XISF specification (Pleiades Astrophoto): http://www.pixinsight.com/xisf
