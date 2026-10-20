# Hardware checklist

AstroFlow was developed and tested with simulators. This list confirms, step by step, that it works with
**your** PC, Player One Uranus-C and Sky-Watcher Flextube 300P SynScan GoTo. Do the steps in order — each
builds on the one before. Tick each box and note anything unexpected.

**If a step fails:** stop there, press *Settings → Support → Save a bug report file*, and write down the step
number, what you did and what you saw. Most problems are fixed by a settings change, not new code.

---

## Part A — Windows PC and installation (indoors, ~20 min)

- [ ] **A1. Install.** Double-click `Install AstroFlow.bat`.
  *Expected:* the window shows downloads of Python and packages and ends with **"AstroFlow is installed."**
  A Desktop and Start-menu icon called *AstroFlow* appear.
- [ ] **A2. Start.** Double-click the Desktop icon.
  *Expected:* within ~10 s an AstroFlow window (Edge app window, no address bar) opens with the setup wizard.
  If nothing appears, run `AstroFlow (with log window).bat` in `%LOCALAPPDATA%\Programs\AstroFlow` and note
  the messages.
- [ ] **A3. Setup wizard.** Go through all steps; on *Plate solving* press the download button.
  *Expected:* field of view 25.6′ × 14.5′; recommended database D80 (≈1.3 GB). The ASTAP program downloads
  and unpacks; the D80 installer is downloaded and started — accept its defaults. Afterwards *Settings →
  Plate solving* shows ASTAP *installed* and the database name.
- [ ] **A4. Practice run with the simulator.** Capture → *Simulated Uranus-C* → Connect → Quick setup
  *Planet* → Record 400 frames → Analyze it → Stack these frames → Stack → Post-process → Auto → Export.
  *Expected:* every step finishes without an error; a JPEG appears in `Documents\AstroFlow\Exports`.
- [ ] **A5. Recycle Bin.** In the Library select the exported JPEG → *Move to Recycle Bin…* → tick the box →
  confirm. *Expected:* the file is in the Windows Recycle Bin and can be restored.
- [ ] **A6. GPU (desktop PC).** Settings → Graphics card → *Install GPU acceleration*, restart AstroFlow.
  *Expected:* the status bar shows *GPU: NVIDIA GeForce GTX 1660 SUPER*; Post-process with deconvolution
  feels faster. (If the install fails, everything still works on the processor.)

## Part B — Player One Uranus-C (indoors or daytime, ~30 min)

- [ ] **B1. Library found.** Settings → Camera driver.
  *Expected:* "Player One library found: …PlayerOneCamera.dll". If not, use *Give AstroFlow the Player One
  SDK zip…* with the SDK from player-one-astronomy.com.
- [ ] **B2. Connect.** Plug the camera into a USB 3 port (blue) directly on the PC. Capture → choose
  *Uranus-C* → Connect. *Expected:* name, 3856 × 2180, 2.9 µm, colour (RGGB) and a temperature reading;
  live view starts.
- [ ] **B3. Controls.** Point the camera (with a cap or a lens, or on the telescope in daylight — **never at
  the Sun**) at a bright scene. Change Exposure, then Gain. *Expected:* the image and histogram respond within
  a second; Exposure follows the logarithmic slider.
- [ ] **B4. Colour order.** Look at something with clear red and blue. *Expected:* natural colours. If red and
  blue are swapped, report it (the Bayer pattern setting needs changing).
- [ ] **B5. Frame size and speed.** Frame size → 640 × 480, 8-bit, exposure 5 ms.
  *Expected:* the frame rate shown in the toolbar is far higher than at full size. Compare with SharpCap at the
  same settings if you have it — within about 10 % is expected.
- [ ] **B6. Record a video.** Record → SER, stop after 3000 frames.
  *Expected:* Recording panel shows frames climbing, *0 frames waiting* most of the time and **no dropped
  frames**; the file appears under `Documents\AstroFlow\Captures\<date>\<target>\`; the Library shows it with
  the right frame count; the frame slider shows sharp frames.
- [ ] **B7. 16-bit.** Repeat B6 with 16-bit. *Expected:* file twice the size, histogram smoother.
- [ ] **B8. Unplug test.** Start an unlimited recording, then pull the USB cable after ~10 s.
  *Expected:* a red message; the recording stops and the file keeps all frames received (open it in Analyze).
  Plug back in: AstroFlow reconnects (or press Connect).
- [ ] **B9. Long exposures.** *Long exposures* mode, 5 s. *Expected:* a new frame every ~5 s; Snapshot saves
  a FITS whose header (Library → details) lists EXPTIME 5, GAIN, temperature, camera name.
- [ ] **B10. Darks.** Cap the camera. Calibration frames → Darks, 10 frames, 5 s.
  *Expected:* 10 frames captured, then "Master saved" and an entry in Stack → Calibration library.
- [ ] **B11. Big recording.** Full frame, 8-bit, record 2 minutes (several GB).
  *Expected:* no dropped frames on the desktop PC (a slow laptop disk may drop some — the panel tells you);
  memory in the status bar stays roughly constant.

## Part C — Mount: Flextube 300P SynScan (daytime first, then at night)

**Safety first:** daylight, tube **capped**, nothing near the tube, never towards the Sun, stand next to the
mount with a hand near the **power switch**. STOP (top-right) or Esc stops everything. Close the SynScan
phone app before connecting AstroFlow directly.

Choose your route (README section 7). Recommended order: **C-HC** (USB to hand controller) or **C-APP**
(SynScan app/ASCOM) first, then **C-WIFI** (direct, *untested on real hardware*).

- [ ] **C1. Location set.** Settings → Location has your town. *Expected:* Mount → *Tonight's best targets*
  lists objects with altitude curves.
- [ ] **C2. Connect.**
  * *C-WIFI:* join the PC to `SynScan_xxxx`; Mount → *SynScan WiFi - direct*; address 192.168.4.1, port 11880.
  * *C-APP:* SynScan app connected to the mount; AstroFlow → *SynScan app on this PC/phone* (port 11882), or
    *ASCOM driver* → *Choose driver…* → *SynScan App Driver*.
  * *C-HC:* USB cable to the hand controller; *SynScan hand controller (USB cable)*, choose the COM port.

  *Expected:* "Connected", a model name, and RA/Dec/Alt/Az values. For C-WIFI the first connection after
  power-on assumes the tube is level and pointing north (the SynScan start position).
- [ ] **C3. Directions (most important).** Speed *16×*. Hold **▲ Up** for 2 s.
  *Expected:* the tube rises and stops when you release. Then ▼ Down (lowers), ◀ Left and ▶ Right (seen from
  behind the eyepiece, left/right turn the base the corresponding way). **If any direction is reversed, press
  STOP and report it before continuing.**
- [ ] **C4. STOP.** Hold ▶ at *400×*, press the red STOP (or Esc) while moving. *Expected:* motion stops
  within about a second.
- [ ] **C5. Release safety.** At *16×*, hold ▶ with the mouse and, while still holding, close the AstroFlow
  window with Alt+F4. *Expected:* the mount stops by itself within about 1.5 s (no "release" message ever
  arrived). Re-open AstroFlow and reconnect.
- [ ] **C6. Altitude limit.** Settings → Mount safety, limit 20°. Move the tube down towards 20°.
  *Expected:* moving down stops at (or about 1° past) the limit and further "down" is refused; a GoTo to a
  target below 20° is refused with an explanation.
  Set the limit back afterwards.
- [ ] **C7. Small GoTo, then sync (night).** Centre a bright star by hand (eyepiece or camera), pick that star
  in *Find a target*, press **Sync (I'm centred on it)**. Then GoTo a second bright star 20–30° away.
  *Expected:* the confirmation message (if > 30°) describes a sensible direction; the mount slews and stops;
  the star is in or near the field. Sync on it; repeat with a third star.
- [ ] **C8. GoTo accuracy.** GoTo a Messier object after 2–3 syncs. *Expected:* within the camera field
  (25′) — or use *Plate-solve & centre* (C10).
- [ ] **C9. Tracking.** Track at *Stars* rate for 10 minutes on a star. *Expected:* the star stays in the
  field (slow drift is normal for an alt-az mount; it must not run away).
- [ ] **C10. Plate-solve & centre.** Camera connected and focused, no Barlow, exposure 2–5 s. Pick a bright
  galaxy or cluster → *Plate-solve & centre*. *Expected:* steps listed with shrinking distances, ending
  "Centred to … arc-minutes" (≤ 2′).
- [ ] **C11. Where am I pointing?** Capture → tools → *Solve current frame*. *Expected:* RA/Dec and the
  objects in view; *Sync the mount to this position* improves the next GoTo.
- [ ] **C12. Park.** Mount → Park. *Expected:* a confirmation first, then the tube returns to its park/home
  position (C-WIFI: level, pointing north; C-APP/C-HC: the position set in the SynScan app/hand controller)
  and tracking stops.
- [ ] **C13. Lost connection (C-WIFI only, careful).** During a slow (8×) hand move, switch the PC's WiFi off.
  *Expected:* a red *Lost contact* warning. **The mount may keep moving** — stop it with the hand
  controller/power switch. Report whether it stopped by itself. (GoTos stop on their own at the target.)

## Part D — A real night, end to end

- [ ] **D1. Jupiter or Saturn.** 3× Barlow, Quick setup *Planet*, focus with the focus meter, 90 s video,
  Analyze → Stack (de-rotate on for Jupiter > 90 s) → Post-process *Auto* → Export.
  *Expected:* a clearly sharper result than any single frame; belts/rings detail visible.
- [ ] **D2. Moon.** Quick setup *Moon*, several overlapping 1000-frame videos → stack each → *Moon mosaic*.
  *Expected:* the panels are stitched without visible seams.
- [ ] **D3. Deep sky.** No Barlow, GoTo M13/M57/M27, Quick setup *Deep sky*, exposure from the field-rotation
  advice, Live stack for 15 min; also record FITS frames. *Expected:* the live stack improves steadily;
  frames are rotated correctly; the final Stack + Solve labels the object.
- [ ] **D4. Darks and flats.** Make darks (same exposure/gain/temperature) and flats (dawn sky or a T-shirt
  over the tube) with the calibration assistant, then stack D3 again. *Expected:* fewer hot pixels, dust
  donuts and dark corners removed; the stack notes say which masters were used.
- [ ] **D5. Second computer.** Settings → allow network access, restart; on a laptop open the shown address,
  enter the code. *Expected:* Library visible, Capture/Mount greyed out; upload a file and process it there.

## Part E — Report

Note for each failed step: the step number, the connection route (C-WIFI/C-APP/C-HC), what you expected,
what happened, and attach the bug-report file. Items that pass here can be considered verified on real
hardware.
