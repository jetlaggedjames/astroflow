# AstroFlow architecture

This document explains how AstroFlow is put together, why, and where to extend it.

## The choice in one paragraph

AstroFlow is a **local engine written in Python** with a **user interface made of plain HTML/CSS/JavaScript**
that the engine serves to an app-style Edge/Chrome window (or to pywebview when installed). Python was chosen
because the hard parts — reading huge SER/FITS files piece by piece, image registration, stacking, plate
solving, astronomy maths, talking to the Player One SDK through `ctypes` and to the mount over UDP/serial/ASCOM —
are best served by mature open-source libraries (NumPy, SciPy, OpenCV, Astropy) and can use an NVIDIA GPU
through CuPy. Serving the interface over HTTP from the same process gives a desktop program on the telescope PC
**and**, with no second installation, the same program in the browser of any other computer in the house, which
can upload files and use every non-hardware tab. Hardware control is refused for any request that does not come
from the PC itself. Everything is offline: no CDN, no build step, no web fonts; the browser only ever talks to
`127.0.0.1` (or to the main PC on the home network when the user allows it).

## Layout

```
run.py                     launcher: starts the engine, opens the window, keeps jobs alive after closing
astroflow/
  config.py                settings (JSON in %APPDATA%\AstroFlow), folders, memory/thread budgets
  logsetup.py, jobs.py     log file; background jobs (progress, cancel, ETA, one heavy job at a time)
  gpu.py                   CuPy-or-NumPy switch ("run this with the GPU if possible, otherwise CPU")
  project.py               projects (.afproj JSON + .bak): sources, analysis, stack, edit steps, history
  export.py                formats, ICC profiles (sRGB, Adobe RGB), sizes/DPI, watermark, caption bar
  io/                      SER (crash-safe writer, tolerant reader, repair), FITS/TIFF/PNG/JPEG/WebP/XISF/RAW,
                           debayering, FrameSource = "a sequence of frames" over a video or a list of files
  imaging/                 quality scoring, star detection, registration (triangle matching + RANSAC; similarity,
                           affine, homography, polynomial), lucky imaging with alignment points and de-rotation,
                           calibration masters + library, integration with rejection, deep-sky pipeline,
                           live stacking, mosaics, background modelling
  process/                 editing tools (ops.py registry with parameter specs), masks, cached renderer,
                           Auto recipes, presets
  astro/                   coordinates (precession, refraction, alt/az), ephemeris (offline, via Astropy),
                           catalogues (OpenNGC, HYG, GeoNames cities), tonight planner, field rotation
  solve/                   WCS solutions, ASTAP integration, built-in wide-field solver, Astrometry.net client,
                           annotation (objects in field, compass, scale bar, label placement)
  hardware/
    camera/                Camera interface; Player One (ctypes over PlayerOneCamera.dll), ASCOM, simulator
    mount/                 Mount interface; SynScan motor protocol (UDP/serial), SynScan hand-controller
                           protocol, ASCOM COM + Alpaca, simulator; controller.py = all safety rules
    capture.py             CaptureManager: acquisition thread, writer thread with bounded queue, recorders,
                           stats/histogram/focus, calibration assistant, live stack, recovery of cut-off files
  library/                 SQLite index (incremental scan of headers only), thumbnails, clean-up/offload plans
  sim/                     simulated planets (turbulence, rotation), real-sky star fields, sensor read-out
  server/                  FastAPI app, access guard (local vs. other computers), routes per area
web/                       index.html, css/app.css, js/ (app shell, api, ui toolkit, viewer, charts, wizard,
                           one module per tab)
data/                      compressed catalogues and the city list (built by tools/build_data.py)
windows/                   installer (PowerShell), launchers, icon, Inno Setup script
tools/                     data builder, portable-folder builder, large-file memory test
tests/                     ~130 automated tests (pytest)
```

## How a request flows

1. The window calls the JSON API (`/api/...`). Quick calls answer directly (status, settings, previews).
2. Anything slow (scoring, stacking, solving, full-resolution processing, exports, clean-up, downloads) is
   submitted to `jobs.JobManager` and returns a job id at once. The UI polls `/api/jobs/{id}` and shows
   progress, an ETA and a Cancel button; heavy jobs run one at a time so two stacks never fight for memory.
3. The live camera view uses a WebSocket (`/api/ws/preview`) that sends a status message plus a JPEG frame
   at up to 12 fps; recording never waits for the preview.

## Large files and memory

* **Frames are read one at a time** (`seek` + `readinto`, no memory mapping) through `FrameSource`; scoring
  works on a reduced copy of each frame and keeps only numbers. Measured: a 6.4 GB SER used 31 MB of extra
  memory to score (25 MB for 1 GB).
* **Lucky stacking** caches decoded frames only when they fit in 60 % of the memory budget, otherwise it reads
  them again; accumulation buffers are sized from the budget.
* **Deep-sky integration** writes registered frames to a memory-mapped temporary store on disk (float16 when
  space is short) and combines them in horizontal stripes on several threads.
* The memory budget defaults to a safe share of RAM and is adjustable in Settings; Stack shows the expected
  time, memory and temporary disk use before starting.

## Recording safety

The acquisition thread only grabs frames; a separate writer thread drains a bounded queue to disk. The SER
writer updates the frame count in the header every second (with `fsync`) and writes timestamps to a side file
that becomes the standard trailer on close, so a crash loses at most one second; the next start finishes such
files automatically (`recover_interrupted_recordings`). Disk space is checked continuously (warning below 5 GB,
clean stop below 1 GB). A camera that disappears ends the recording cleanly and a reconnect loop starts.

## Mount safety (hardware/mount/controller.py)

Every mount driver sits behind `MountController`, which enforces: commands only on user request; GoTos below
the altitude limit refused; the Sun blocked; moves larger than a threshold (30°) return a one-time
confirmation token that the UI must send back; Park needs confirmation; hand moves need a heartbeat from the
UI at least every 1.5 s or the watchdog stops them; STOP is always accepted and never raises; every command is
logged with its source. The SynScan motor driver adds a runaway guard (a GoTo that moves away from the target
is aborted) and tolerates lost UDP packets with retries. Note the physical limit: if the link is lost during a
continuous hand move, no stop command can reach the mount; GoTos use the motor controller's own goto, which
stops by itself.

## Non-destructive processing

Raw recordings are opened read-only. Stacks are new files. Editing is a list of steps
`{op, params, enabled, mask, opacity}` plus mask definitions, stored in the project with a full undo history.
The renderer caches each intermediate result keyed by a hash of everything before it, so changing step 5
recomputes only steps 5 onward, on a preview-sized copy; parameters measured in pixels are scaled for the
preview so it matches the full-resolution result. "Save result" renders at full resolution in a background job.

## Second computer

`server/app.py` classifies each request as local (loopback) or remote. Remote requests are refused unless
network access is enabled, and then need the access code (cookie or header, constant-time comparison).
Hardware routes depend on `require_local`; remote users cannot change camera/mount/network/folder/solver
settings, can only reach files inside the library folders and uploads (`safe_path`), and cannot offload or shut
down. Uploads stream to disk in 8 MB chunks.

## Extension points

* **New camera:** implement `hardware/camera/base.py: Camera` (connect, controls, ROI, stream/expose, info)
  and add it to `CaptureManager.connect` and `/api/capture/cameras`. A **guide camera** would be a second
  `CaptureManager`-like loop feeding a guiding module (centroid → mount pulse/rate corrections through the
  `Mount.move`/`set_tracking` interface); the camera and mount interfaces were kept separate for that reason.
* **New mount driver:** implement `hardware/mount/base.py: Mount` and register it in `controller.DRIVERS`;
  the controller's safety rules then apply automatically.
* **Equatorial wedge:** `equipment.mount_type = "eq"` already turns off field-rotation advice; an EQ driver
  would track in RA only and the registration model can stay "similarity".
* **New editing tool:** add a function with `@op(...)` and `Param` specs in `process/ops.py`; the UI builds
  its controls from the specs, and masks, opacity, caching, presets and history work automatically.
* **New file format:** extend `io/images.py` (`read_image`, `read_header`, writers) and `library/index.py`.

## Testing

`python -m pytest` runs unit tests for the maths (scoring, star detection, registration, stacking, rejection,
wavelets, deconvolution, plate-solve parsing, WCS), the file formats, the library and clean-up safety, the
hardware simulators (SynScan protocol, HC protocol, Alpaca mock, capture with unplugging), the HTTP API end
to end, and a 1.2 GB memory test. The simulators (`sim/`, `hardware/*/simulator.py`) make the complete
workflow usable without a telescope; `HARDWARE_CHECKLIST.md` lists what must still be confirmed on real
equipment.
