# AstroFlow

**One program for the whole night: capture → analyze → stack → plate-solve → post-process → export.**

AstroFlow replaces the usual chain of separate programs (a capture program, a file browser, a stacker,
a plate solver and an editor) with one application whose tabs follow the way you actually work. It was built
around a Sky-Watcher Flextube 300P SynScan GoTo Dobsonian with a Player One Uranus-C camera, but works with
any similar telescope, camera and SynScan-type mount.

* **Planets and the Moon:** record fast videos, keep the sharpest moments ("lucky imaging"), stack them with
  multi-point alignment, sharpen with wavelets and deconvolution.
* **Deep sky:** short exposures that suit an alt-az mount, live stacking while you watch, calibration frames,
  rotation-aware stacking, plate solving and object labels.
* **Everything stays on your computer.** No account, no cloud, no internet needed at the telescope.
* **Nothing is ever destroyed.** Your recordings are never changed; every edit can be undone; deleting files
  always asks first and goes to the Recycle Bin.

---

## Contents

1. [What you need](#1-what-you-need)
2. [Installing on Windows](#2-installing-on-windows)
3. [First start: the setup wizard](#3-first-start-the-setup-wizard)
4. [Your first night, step by step](#4-your-first-night-step-by-step)
5. [The tabs in detail](#5-the-tabs-in-detail)
6. [Using a second computer](#6-using-a-second-computer)
7. [Connecting the mount (research findings and recommendations)](#7-connecting-the-mount)
8. [Field rotation on an alt-az mount](#8-field-rotation-on-an-alt-az-mount)
9. [Your files, safety and backups](#9-your-files-safety-and-backups)
10. [Keyboard shortcuts](#10-keyboard-shortcuts)
11. [Troubleshooting](#11-troubleshooting)
12. [What has been tested, and what still needs your telescope](#12-what-has-been-tested-and-what-still-needs-your-telescope)
13. [For developers](#13-for-developers)

---

## 1. What you need

* A 64-bit Windows 10 or 11 PC. 8 GB of memory works; 16 GB or more is comfortable. Your desktop
  (Ryzen 3 4100, GTX 1660 Super, 64 GB) is more than enough; a laptop works too, just more slowly.
* Free disk space for recordings. Planetary videos are big: a 2-minute Jupiter video at full speed can
  be 10–20 GB. An SSD is best for recording.
* Internet **once**, while installing (about 400 MB). After that AstroFlow works fully offline.
* Optional, downloaded when you ask for it in the setup wizard or Settings:
  * the **ASTAP** plate solver and a star database (100 MB – 1.3 GB, depending on your field of view);
  * the **Player One camera library** (comes with SharpCap, N.I.N.A., FireCapture or Player One's own
    software — or AstroFlow can install it from Player One's free SDK zip);
  * **GPU acceleration** for NVIDIA graphics cards (about 1 GB).

No camera or telescope yet? AstroFlow has a built-in **simulator** (a pretend camera and mount) so you can
learn the whole workflow at your desk.

---

## 2. Installing on Windows

### The simple way

1. Copy the AstroFlow folder to your PC (for example to your Desktop) and open it.
2. Double-click **`Start AstroFlow.bat`** (it installs AstroFlow the first time, then just starts it - use it every time). `Install AstroFlow.bat` does only the installation.
   A black window opens and shows progress. It downloads Python (from python.org) and the open-source
   packages AstroFlow uses into AstroFlow's own folder — it does not change anything else on your PC and
   needs no administrator rights. This takes a few minutes.
3. When it says **"AstroFlow is installed"**, press a key to close the window.
4. Start AstroFlow with the new **AstroFlow** icon on your Desktop (or in the Start menu).

AstroFlow is installed in `%LOCALAPPDATA%\Programs\AstroFlow`. Running the installer again repairs or
updates it and keeps all your settings and pictures.

> If Windows shows "Windows protected your PC", click **More info → Run anyway**. This appears for any
> script that is not from the Microsoft Store.

### A portable copy (USB stick, laptop without internet)

On a PC with internet, open PowerShell in the AstroFlow folder and run:

```
powershell -ExecutionPolicy Bypass -File tools\build_portable.ps1
```

This creates `dist\AstroFlow-Portable` (and a `.zip` of it). Copy that folder to any 64-bit Windows PC and
double-click **`AstroFlow.vbs`** inside it — no installation, no internet needed.

### A classic Setup.exe

After building the portable folder, install the free [Inno Setup](https://jrsoftware.org/isinfo.php), open
`windows\AstroFlow.iss` and press **Compile**. You get `dist\AstroFlow-Setup-1.0.0.exe` with the usual
installer, shortcuts and uninstaller. (The uninstaller never removes your pictures or settings.)

### Linux or macOS (for developers or a headless server PC)

```
./start.sh            # creates a Python environment the first time, then starts AstroFlow
```

Camera control on Linux/macOS uses the Player One library for those systems if installed; the ASCOM options
are Windows-only.

### How AstroFlow runs (in one paragraph)

AstroFlow is a desktop program made of two halves: an **engine** written in Python that does all the work
(talking to the camera and mount, reading huge files piece by piece, stacking, processing), and a **window**
that shows the controls. The window is an ordinary web page shown in an app-style Edge (or Chrome) window,
so it looks and behaves like a normal program and works with no internet. This design was chosen because it
gives one program that runs the same on the telescope PC and — when you allow it — in the web browser of any
other computer or tablet in your house, with no second installation. The camera and mount are only ever
controlled from the PC they are plugged into; other computers get the Library, Analyze, Stack, Post-process
and Export tabs and can send files to the main PC.

---

## 3. First start: the setup wizard

The first time AstroFlow starts, a short wizard asks a few questions. You can skip any step, and run the
wizard again later from **Settings → Support → Run the setup wizard again**.

1. **Telescope & camera** — pre-filled for the Flextube 300P (305 mm, 1500 mm focal length), the SVBONY 3×
   Barlow, the Uranus-C (3856 × 2180 pixels of 2.9 µm) and the UV/IR-cut filter. Change anything that
   differs. The wizard shows what this means: about **0.40″ per pixel** and a **25.6′ × 14.5′** field of
   view without the Barlow (Jupiter is about 113 pixels across); with the 3× Barlow, 0.13″ per pixel.
2. **What you photograph** — planets/Moon, deep sky, or both. This only changes which tab opens first and
   a few starting values.
3. **Camera driver** — checks whether the Player One library is on the PC. If not, download the "Camera
   SDK" zip for Windows from [player-one-astronomy.com](https://player-one-astronomy.com/service/software/)
   and give the zip to AstroFlow with the button; it copies the right file into place.
4. **Mount** — choose how the mount is connected (see [section 7](#7-connecting-the-mount)). You can also
   decide later.
5. **Plate solving** — *Plate solving matches the pattern of stars in your photo against a star catalogue to
   work out exactly where the telescope was pointing. It needs a star database sized for your field of view;
   it is downloaded once and then works offline.* The wizard calculates your field of view, recommends a
   database and shows the download size **before** anything is downloaded. For the 300P with the Uranus-C
   the field is narrow (0.43° × 0.24°), so the recommendation is ASTAP's **D80** database (about 1.3 GB) —
   **D50** (about 900 MB) usually works too. You can also allow an *online* fallback (Astrometry.net) — off
   by default; when on, only the picture being solved is sent.
6. **Location** — type your town (works offline, about 170,000 places) or enter latitude/longitude. Used
   for "what's up tonight", the mount and field-rotation advice.
7. **Folders** — where recordings, projects and exports go, with the free space shown. Put **Captures** on
   your biggest, fastest drive.
8. **Finish** — choose **Beginner** (fewer controls, plain-language hints) or **Advanced** (every setting),
   and the display theme. Both can be switched any time at the top of the window.

---

## 4. Your first night, step by step

The tabs follow the workflow from left to right. In Beginner mode every tab has a blue box telling you what
to do next.

### Planet or Moon

1. **Mount tab** (optional): connect the mount, pick *Jupiter* (buttons at the top of *Find a target*) and
   press **GoTo**. A big move always asks first and tells you where the tube will point. The red **STOP**
   button in the top-right corner (or the **Esc** key) halts the mount at any time.
2. **Capture tab:** choose your camera and press **Connect**. Press **Quick setup → Planet** (a small frame in
   the middle, short exposure, higher gain). Centre and focus — the *focus meter* graph at the bottom goes
   up when the focus gets better.
3. Adjust **Exposure** and **Gain** until the brightest part of the planet sits around 50–70 % in the
   histogram and the chip below it says *No clipping*.
4. Type the target name under **Record**, choose a length (60–120 s for Jupiter because it rotates quickly)
   and press **● Record** (or the **R** key). The panel shows frames, speed, data written, free disk space
   and any dropped frames.
5. When it finishes, press **Analyze it →**. Every frame gets a score; the suggestion (e.g. *keep the best
   25 %*) is usually right. Press **Stack these frames →**.
6. **Stack tab:** the defaults are good. Look at the time/memory estimate and press **Stack**.
7. Press **Post-process →**, then **Auto**. Adjust any step you like — e.g. *Wavelet sharpening* — and
   compare with **Before / after** (key **B**). Nothing is permanent.
8. **Export →** choose JPEG and press **Export**. Done.

Want it even simpler? In the Library (or right after recording) press **Quick process**: it scores, stacks
and prepares an Auto edit in one go, and opens the result in Post-process.

### Deep sky (galaxy, nebula, cluster)

1. **Mount tab:** pick a target from **Tonight's best targets** and press **GoTo**. The target card shows the
   **longest sub-exposure** that will not smear the corners because of alt-az field rotation (often 10–30 s).
2. **Capture tab:** **Quick setup → Deep sky**, then set the exposure under *Long exposures*.
3. Press **Start live stack** (Capture → *Live stack*): the picture gets cleaner with every exposure.
4. To keep the individual frames for a better stack later, **Record** with format *FITS image sequence*.
5. **Analyze** finds clouds, satellite trails and blurred frames; **Stack** aligns the frames (including
   rotation), removes trails and hot pixels and uses your darks/flats automatically if you made them.
6. **Solve** to identify every object in the picture; **Post-process → Auto** (background extraction, colour
   calibration, stretch); **Export** — with object labels if you like.

---

## 5. The tabs in detail

### Capture

| Area | What it does |
|---|---|
| **Camera** | Choose the camera (Player One, ASCOM on Windows, or the simulator) and connect. Shows sensor size, pixel size and temperature. |
| **Quick setup** | Starting points for Planet, Moon, Deep sky and Find & focus. Nothing changes until you press one. |
| **Exposure and gain** | Video mode (short exposures, many frames per second) or Long exposures (seconds). Exposure, gain, offset; in Advanced mode also USB bandwidth and frame-rate limit. Hover over any label for an explanation. |
| **Frame size** | Region of interest (smaller = faster), **Draw frame on image**, binning (1×1, 2×2) and 8/16-bit. Shows pixel scale and field of view. |
| **Record** | SER video or FITS sequence, unlimited or stop after N frames / N seconds. Recordings are written continuously and safely (see [section 9](#9-your-files-safety-and-backups)). |
| **Snapshot** | Saves the current frame as FITS (key **S**). |
| **Histogram** | Linear or log scale, with a clear clipping warning. |
| **Focus meter** | Live graph: star size (FWHM, smaller is better) for stars, sharpness (higher is better) for planets/Moon. **Measure in a box** limits it to one star or crater. |
| **Live stack** | Stacks deep-sky exposures as they arrive, correcting the rotation of an alt-az mount. Saves the result as FITS. |
| **Calibration frames** (Advanced) | Captures darks, flats, flat-darks or bias with instructions, checks flat brightness, builds a master and files it in the **calibration library** by camera, gain, temperature and exposure — Stack then picks matching masters automatically. |
| **Collimation helper** | Defocus a bright star into a ring; AstroFlow draws circles on the outer ring and the dark centre and tells you how far off-centre it is. |
| **Where am I pointing?** | Plate-solves the live frame, lists what is in view and offers to **sync the mount** to it. |
| **Crosshair / display** | Crosshair for centring; screen brightness options (these never change recorded data). |

### Mount

* **Connect** with the method you chose (see [section 7](#7-connecting-the-mount)). Connections marked
  *untested on real hardware* show a warning.
* **Pointing** shows RA/Dec, altitude/azimuth, tracking, and how many alignment (sync) points the mount has.
* **Move by hand**: hold an arrow button (or an arrow key) — the mount moves only while held. Speed from 1×
  to 800× the sky's motion.
* **Find a target**: search Messier, Caldwell, NGC, IC, named stars, planets and the Moon (offline).
* **Tonight's best targets**: ranked list with altitude curves, best time, hours above the horizon and a
  field-rotation note.
* **Target card**: current position, size, brightness and field-rotation advice, with **GoTo**, **Sync (I'm
  centred on it)**, **Plate-solve & centre** (takes exposures and nudges the mount until the target is
  centred) and **Capture this →**.
* **Tracking** (stars / Moon / Sun rate), **Park** (asks first), **Set home** and **Reset alignment**.
* **Command log** (Advanced): every command sent, and why.

Safety rules that are always on: AstroFlow never moves the telescope unless you press a button; moves larger
than 30° (adjustable) and Park always ask first and say where the tube will point; targets below your
altitude limit (10° by default) are refused; GoTo to the Sun is blocked; STOP is always visible while a
mount is connected; hand moves stop by themselves if the button-release message is lost.

### Library

Everything you record, stack, process or export is indexed automatically. **Add folder** indexes older
pictures without moving them; **Rescan** re-checks folders quickly (only file sizes, dates and headers are
read). Supported: SER, AVI, FITS, XISF, TIFF, PNG, JPEG and camera RAW files.

* Filter by **night/session** (grouped automatically by observing night), target, type, status
  (raw / stacked / processed / exported), frame type, camera, tags and rating; search names, targets, tags
  and notes; sort by date, size, quality, exposure, gain and more.
* Thumbnails; videos show any frame with a slider (only that frame is read, so even 18 GB files are instant).
* Details: all header information, processing history, plate solution; edit target, tags, rating and notes.
* Select files and **Analyze**, **Stack**, **Post-process**, **Plate solve**, **Export** or **Quick process**.
* **Free up space / clean up…** shows where the space goes, finds **duplicate files** (by checksum) and
  **failed or low-quality captures**, and selects them for you — nothing is removed at that point.
* **Move to Recycle Bin…** and **Offload to another drive…** always show a confirmation that lists **every
  file and the total size**, and you must tick *"I have checked this list"*. Raw captures are protected:
  they are never included unless you explicitly agree. **Offload** copies, verifies every copy with a
  SHA-256 checksum, remembers where the copies are, and only then offers to remove the originals (to the
  Recycle Bin). See [section 9](#9-your-files-safety-and-backups).

### Analyze

Scores every frame (about 20,000 frames per minute for a 800 × 600 video on a 4-core PC).

* **Planets/Moon:** sharpness, contrast, brightness, noise, drift, clipping; detects clouds, the object
  partly leaving the frame, focus drift, dew and over-exposure.
* **Deep sky:** stars, star size (FWHM), elongation, background, signal-to-noise; detects clouds, satellite
  and plane trails, wind/tracking blur and focus drift.
* The graph shows every frame (blue = kept, red = problem). Change the metric, sort best-first, **drag the
  dashed line** to set a quality threshold, or keep the best N % / N frames. Click a bar to see that frame;
  **Keep** / **Throw out** override the choice (keys **K** / **X**, arrows to step).
* The report suggests how many frames to keep and how much stacking will reduce noise.

### Stack

* **Planet / Moon video (lucky imaging):** keeps the best frames, aligns the whole disc and then up to
  hundreds of small **alignment points** separately (the air distorts each part of the planet differently),
  uses each point's own sharpest moments, and optionally **de-rotates** Jupiter/Saturn/Mars for long
  recordings, upsamples or drizzles. In tests on simulated turbulence the multi-point result was 20–40 %
  closer to the true image than whole-frame alignment.
* **Deep-sky frames:** dark, flat, flat-dark and bias calibration (automatic from the calibration library or
  your own files), hot-pixel removal, debayering, star-based registration that corrects **rotation** (alt-az
  field rotation) and optionally distortion, per-frame gradient removal, normalisation, quality or noise
  weighting, and rejection of outliers (sigma, winsorized, linear-fit, percentile, min/max). **Add another
  night** combines several sessions, each with its own calibration.
* **Moon mosaic:** stitches overlapping stacked panels into one large picture.
* **Before you start** shows the estimated time, memory and temporary disk space. Every stack shows
  progress and can be cancelled. Files are read piece by piece, so the size of the recording does not
  matter (tested with multi-gigabyte videos — see [section 12](#12-what-has-been-tested-and-what-still-needs-your-telescope)).
* Output: a 32-bit FITS (for further processing anywhere) and a 16-bit TIFF, with the full processing
  history written into the file.

### Solve

Plate-solves an image with, in order: **ASTAP** (offline, recommended), the **built-in solver** (offline,
only for wide fields of about 2–3° or more) and — only if you allowed it — **Astrometry.net** online. A hint
(target name or RA/Dec) makes it faster; AstroFlow also reads the target and mount position stored in the
file. The solution is saved next to the image and in the library. The image is then labelled with every
galaxy, nebula, cluster and named star (toggle labels, compass and scale bar); **Export with labels →**
puts them on an exported copy.

### Post-process

A list of editing **steps** applied on top of your stacked image — the stack itself never changes.

* **Auto** builds a starting recipe for the target type (planet, Moon or deep sky). It only runs when you
  press it.
* Tools (left), grouped as Geometry (crop, rotate, flip, resize), Stretch & tone (auto stretch, levels,
  curves, arcsinh, generalised hyperbolic stretch, gamma, HDR compression, local contrast), Colour (colour
  calibration — photometric with a plate solution, otherwise from the background and stars — white
  balance, saturation/vibrance, green-noise removal, RGB channel alignment for atmospheric dispersion,
  lunar mineral colour, IR/luminance combine), Detail (RegiStax-style **wavelets** with per-layer control,
  Richardson–Lucy **deconvolution** with automatic blur measurement, unsharp mask), Noise (wavelet and
  non-local-means noise reduction), Deep-sky (background extraction, star reduction) and Planetary
  (planet-edge clean-up).
* Each step can be switched off, reordered, removed, faded (strength) and limited to a **mask**: by
  brightness, stars, the planet/Moon disc (or just its edge), or painted with a brush. **Show** displays a
  mask in red.
* **Undo/Redo** (Ctrl+Z / Ctrl+Y) and a **History** list of every change, saved in the project — close
  AstroFlow and continue tomorrow exactly where you stopped.
* **Presets**: built-in recipes (Jupiter, Saturn, Mars, Moon, Mineral Moon, Deep-sky, Galaxy, Nebula) and
  your own.
* The preview is computed at screen resolution for speed; **Full-res detail** shows the visible area at
  100 %. **Save result** processes the full image and saves a 32-bit FITS + 16-bit TIFF master in the
  project.
* Uses the NVIDIA graphics card for the heavy tools when GPU acceleration is installed, otherwise the
  processor — automatically.

### Export

* Formats: **JPEG** (quality), **PNG** (8/16-bit), **TIFF** (8/16/32-bit), **WebP**, **FITS** (16/32-bit).
* Colour profile **sRGB** (screens, web) or **Adobe RGB** (wide-gamut printing).
* Size: original, percentage, custom pixels (fit, fill or stretch), social media presets, HD/4K wallpaper
  and print sizes at 300 DPI (A4, A3, 4×6", 8×10"); custom DPI.
* **Watermark**: text or image/logo, position, size, opacity, margin, colour — save it for reuse.
* **Caption bar**: target, date, location, telescope, camera, Barlow, filter, exposure, frames — plus your
  own text.
* **Labels, compass, scale bar** for plate-solved images.
* Several images at once (batch) with the same settings; **Save to** any folder ("save a copy").
  Everything stays on your computer.

### Settings

Look and feel (mode, theme, large touch controls, red pictures in Night mode), telescope and camera (with
field of view), location, folders, plate solving (downloads, solver choice, online fallback), camera driver,
mount safety (altitude limit, confirmation threshold), second computer (network access and access code),
graphics card and performance (memory and processor threads for stacking), and Support (view the log, save
a bug-report file, run the setup wizard again).

---

## 6. Using a second computer

You can work on your pictures from another computer, laptop or tablet in the house — in its web browser,
nothing to install:

1. On the **main computer** (the one with the camera and mount): **Settings → Second computer → Allow other
   computers on my network**, then restart AstroFlow. Windows may ask about the firewall — allow
   **Private networks** only.
2. Settings shows the address (e.g. `http://192.168.1.20:8765`) and a 6-digit **access code**.
3. On the other device, open that address in a web browser and type the code.

The other device gets the Library, Analyze, Stack, Solve, Post-process and Export tabs. The camera and mount
tabs are greyed out — they can only be used on the main computer. Files you **Upload** or drag onto the
window are copied to the main computer's Uploads folder and appear in the Library; **Download** copies
results back. The other device can only see the main computer's AstroFlow folders, nothing else. Make a new
access code at any time in Settings.

---

## 7. Connecting the mount

### Research findings

The Flextube 300P SynScan GoTo is driven by Sky-Watcher's motor controller. There are four practical ways
for a PC to command it:

1. **Through Sky-Watcher's SynScan app (SynScan Pro) — most reliable.** The SynScan app connects to the
   mount's WiFi adapter and handles alignment; other programs talk to the app. On Windows the app comes with
   an **ASCOM driver** ("SynScan App Driver"), and the app also accepts the hand-controller protocol over UDP
   on **port 11882** (this is how SkySafari connects to it; users report that 11882 on the device running the
   app works where the printed 11880 does not)
   ([SkySafari support thread](https://support.simulationcurriculum.com/hc/en-us/community/posts/115009332408-Why-Is-There-No-Connection-Between-Skywatcher-Synscan-App-and-SkySafari-5-Pro-Solved-use-Port-11882-not-11880n-as-on-the-printed-instructions-)).
   In AstroFlow choose **SynScan app on this PC/phone (network)** or **ASCOM driver**.
2. **USB cable to the SynScan hand controller — very reliable, beginner-friendly.** The hand controller
   keeps your star alignment; AstroFlow uses the documented hand-controller commands. Choose **SynScan hand
   controller (USB cable)**. (Setting the hand controller to *PC-Direct* mode and choosing **SynScan - USB/serial
   cable … PC-Direct** instead lets AstroFlow drive the motors directly.)
3. **Direct WiFi to the mount — simplest, no extra software, but untested on real hardware.** The WiFi
   adapter (SynScan WiFi / the mount's built-in WiFi) creates its own network (`SynScan_xxxx`) and listens
   at **192.168.4.1, UDP port 11880** for Sky-Watcher's *motor controller* protocol, which Sky-Watcher has
   published ([command set PDF](https://inter-static.skywatcher.com/downloads/skywatcher_motor_controller_command_set.pdf),
   [Sky-Watcher application development page](https://www.skywatcher.com/download/manual/application-development/),
   [skywatcher_open on GitHub](https://github.com/skywatcher-pacific/skywatcher_open)) and which open-source
   projects implement over UDP (e.g. [pysynscan](https://github.com/nachoplus/pysynscan),
   [synscan on PyPI](https://pypi.org/project/synscan/)). AstroFlow implements this protocol itself
   (choose **SynScan WiFi - direct**): it keeps its own alignment model (sync on 2–3 stars), does GoTo with the
   motor controller's own goto (which stops by itself at the target), tracking, hand moves, park and a
   runaway guard that stops the mount if it moves the wrong way. It works perfectly with the built-in
   simulator, which speaks the same protocol, including lost packets — **but it has not yet been run on
   the real mount.** Use it the first time in daylight with a hand on the power switch (see
   `HARDWARE_CHECKLIST.md`).
4. **ASCOM Alpaca** (network version of ASCOM) — works with any Alpaca server (e.g. ASCOM Remote, or an
   Alpaca-capable driver). Choose **ASCOM Alpaca**.
5. **INDI** (the Linux standard, `indi_skywatcherAltAzMount`) is not used: it does not run on Windows. Linux
   users can reach INDI mounts through an Alpaca bridge.

**Ranking — reliability:** SynScan app/ASCOM ≈ USB to hand controller > Alpaca > direct WiFi (untested).
**Ranking — beginner ease:** USB to hand controller > SynScan app > direct WiFi > ASCOM/Alpaca.
**Recommendation:** start with the **SynScan app (or its ASCOM driver)** or the **USB cable to the hand
controller**; try **direct WiFi** once the checklist has confirmed it works with your mount.

### The WiFi catch: no internet while connected

When your PC joins the mount's WiFi network it loses the internet (the mount's network has none). AstroFlow
needs no internet at the telescope, so this is fine. If you want to stay online, connect the PC to your home
network with an **Ethernet cable** or add a cheap **second USB WiFi adapter**, and join the mount's network
with one adapter and your home network with the other. Close the SynScan phone app before connecting
AstroFlow directly — only one program can talk to the mount at a time.

### Important safety note

If the connection **drops while the mount is moving by hand**, AstroFlow can no longer send "stop" and the
motors keep turning until you stop them with the hand controller or the mount's power switch. AstroFlow shows
a red *Lost contact* warning when this happens. GoTos are safer: the motor controller stops by itself at the
target. Keep the mount within reach while it moves, especially over WiFi.

---

## 8. Field rotation on an alt-az mount

A Dobsonian moves up/down and left/right, so the camera stays level with the horizon while the sky slowly
turns. Long exposures therefore smear stars into small arcs — worst in the corners, fastest for targets
high in the south. AstroFlow handles this in four ways:

* the Mount and Capture tabs show the **longest exposure that stays sharp to the corners** for your target,
  right now (rate = 15.04″/s × cos(latitude) × cos(azimuth) / cos(altitude));
* **live stacking** and **deep-sky stacking** measure and correct the rotation between frames;
* stacking rejects frames whose stars are stretched (wind, tracking, rotation);
* *Tonight's best targets* notes when a target will be in a fast-rotation part of the sky.

Short exposures (5–30 s) with many frames work very well with the 300P. An equatorial wedge removes field
rotation entirely — AstroFlow already has a *Mount type: Equatorial* setting for that day.

---

## 9. Your files, safety and backups

| What | Where |
|---|---|
| Recordings | `Documents\AstroFlow\Captures\<night>\<target>\` — e.g. `Jupiter_231502.ser`, or `lights_231502\M31_0001.fits` |
| Projects (stacks, edits, history) | `Documents\AstroFlow\Projects\<name>\project.afproj` and the stacked/processed files |
| Exports | `Documents\AstroFlow\Exports\` |
| Settings, library index, calibration library, logs | `%APPDATA%\AstroFlow\` |

* **Raw recordings are never modified.** Stacking, processing and exporting always write new files.
* **Recording is crash-safe.** SER videos are written continuously and the frame count is updated every
  second, so a power cut or a frozen laptop loses at most the last second; AstroFlow repairs an interrupted
  file automatically. If the camera is unplugged, everything recorded so far is kept and AstroFlow keeps
  trying to reconnect. Recording stops cleanly (and saves) before the disk is full: a warning appears under
  5 GB free and recording stops at 1 GB free.
* **Projects save themselves** after every change (with a backup copy), so nothing is lost if AstroFlow
  closes unexpectedly.
* **Deleting** always goes through a confirmation listing every file and the total size, moves files to the
  **Recycle Bin**, and protects raw captures. **Offloading** copies to another drive, verifies each copy with
  a SHA-256 checksum, records the new location and only then offers to remove the originals.
* **Closing the window** while recording or stacking keeps the job running in the background until it is
  finished; re-open AstroFlow to watch it.
* **Logs:** `%APPDATA%\AstroFlow\logs\astroflow.log`. *Settings → Support → Save a bug report file* zips the
  log and your settings (with the access code and API keys removed) for an email.

---

## 10. Keyboard shortcuts

| Key | Action |
|---|---|
| Alt+1 … Alt+9 | Switch tabs |
| Esc | **STOP the mount** (when connected) |
| R / S | Capture: start/stop recording / snapshot |
| Arrow keys | Mount: move while held · Analyze: previous/next frame |
| K / X | Analyze: keep / throw out the frame |
| Ctrl+Z / Ctrl+Y | Post-process: undo / redo |
| B | Post-process: before/after split |
| F / 0 | Images: fit to window / 100 % |
| Ctrl+A, Delete | Library: select all, move selection to the Recycle Bin (asks first) |
| ? | Help |

Hover over any dotted word or setting for a plain-language explanation.

---

## 11. Troubleshooting

**The AstroFlow window does not open.** Start *AstroFlow (with log window).bat* in the AstroFlow folder — it
shows messages. AstroFlow uses Microsoft Edge (installed on every Windows 10/11) for its window; if Edge has
been removed it opens your default browser. You can also open `http://127.0.0.1:8765` in any browser.

**"Player One library not found".** Install SharpCap, N.I.N.A. or Player One's software, or download Player
One's Camera SDK zip and use *Settings → Camera driver → Give AstroFlow the Player One SDK zip*. Then press
*Search again* on the Capture tab.

**Camera connects but frames are dropped.** Use a USB 3 port directly on the PC (not a hub), a smaller frame
(ROI), 8-bit, or lower *USB bandwidth* (Advanced). Record to an SSD. The Record panel shows dropped frames.

**Mount: "No answer from the mount".** Check the PC is joined to the `SynScan_xxxx` WiFi, close the SynScan
phone app, check the address (192.168.4.1) and port (11880 direct, 11882 via the SynScan app). Try the USB
cable route.

**GoTo is off by a few degrees.** Normal before syncing. Centre a bright star by hand and press **Sync**,
or use **Plate-solve & centre**. Two or three syncs spread across the sky make GoTos accurate.

**The mount moves the wrong way.** Press STOP. Report it (see the checklist) — the direction settings for the
direct protocol must be confirmed on the real mount.

**Plate solving fails.** Install ASTAP with the recommended database (Settings → Plate solving). Remove the
3× Barlow for solving — with it the field (0.14° × 0.08°) is too small for any star database. Use stacked
or longer exposures so enough stars show, give a target hint, and check focal length and pixel size in
Settings. The built-in solver only works for fields of about 2–3° or more.

**Stacking says "no frame had enough stars".** The frames are out of focus, clouded, too short, or these are
not deep-sky frames — check them in Analyze.

**Processing is slow.** Install GPU acceleration (Settings → Graphics card) on the NVIDIA PC; on a laptop use
the reduced-size preview and save the full-size result once at the end.

**The second computer cannot connect.** Make sure network access is on and AstroFlow was restarted, that
both are on the same network, and that the firewall allows AstroFlow on Private networks.

**Something else.** Save a bug report (Settings → Support) and send it with a description of what you did.

---

## 12. What has been tested, and what still needs your telescope

This version was built and tested **without** the real camera, mount and Windows PC, using simulators and
automated tests (130 tests). In detail:

**Verified with simulators and automated tests (on Linux):**
* the complete workflow through the real user interface, in a browser: connect camera → live view → record
  SER → analyze → stack → Auto process → export, and mount GoTo with confirmation → deep-sky live stack →
  "where am I pointing" → record FITS → stack (rotation corrected) → plate solve → object labels → export;
* SER writing/reading, crash recovery of interrupted files, unplugging the camera mid-recording (all frames
  kept), disk-space protection;
* frame scoring (correlates 0.96 with the true blur), lucky stacking (multi-point beats whole-frame
  alignment), de-rotation, deep-sky stacking with calibration and rotation, live stacking, mosaics;
* every post-processing tool, masks, presets, undo history, every export format and colour profile;
* plate solving with ASTAP and the built-in solver on simulated real-sky images; object labelling;
* the SynScan motor protocol against a simulator (GoTo accuracy after one sync within about 1′, tracking,
  lost WiFi packets, runaway guard, watchdog), the hand-controller protocol, Alpaca, plate-solve-and-centre;
* clean-up safety (confirmation, raw protection, single-use plans, checksum-verified offload);
* second-computer access rules (access code, no hardware, no files outside the library);
* **large files:** a 6.4 GB SER video (6,710 frames) — preview used 0 MB extra memory, scoring 31 MB extra
  (25 MB for a 1 GB file: flat), stacking stayed within its memory budget.

**Needs your real equipment (step-by-step list in `HARDWARE_CHECKLIST.md`):**
* the Player One Uranus-C through its SDK (written against Player One's published SDK; untested on the real
  camera), frame rates and dropped frames on your PC;
* every real mount connection — **direct WiFi is marked "untested on real hardware"** — including movement
  directions, GoTo accuracy, tracking and park;
* the Windows installer, the Edge app window, ASCOM drivers and the Recycle Bin on Windows (the program was
  developed on Linux; the Windows-specific parts are written for Windows but have not been run there yet);
* GPU acceleration on the GTX 1660 Super.

---

## 13. For developers

```
pip install -r requirements-dev.txt
python -m pytest                    # 130 tests, about 2 minutes (set ASTROFLOW_SKIP_BIG=1 to skip the 1.2 GB file test)
python run.py --browser             # run with your normal browser
python tools/memtest_big_ser.py --gb 6   # large-file memory test
```

The program structure, design decisions and extension points (guide camera, equatorial wedge, new cameras
and mounts) are described in **ARCHITECTURE.md**. Third-party components and their licences are listed in
**THIRD_PARTY.md**. AstroFlow does not include or require AutoStakkert!, RegiStax or any other closed-source
program; the techniques they made popular (lucky imaging with alignment points, wavelet sharpening) are
implemented independently.
