# Plant Display — Project History & Design Notes

This document captures what Plant Display is, how it evolved, and the reasoning
behind the non-obvious design choices — so anyone picking the project up can
understand *why* it works the way it does, not just *how*.

---

## The problem it solves

A Windows 11 machine (running inside a Hyper-V VM) needed to broadcast its
screen and specific windows to several wall-mounted TVs at once. Normal
Miracast/wireless-display doesn't work in a VM (no Wi-Fi Direct / WDDM), so
instead Plant Display serves the content over the LAN: each TV opens a web page
that plays the assigned source, and Samsung/Hisense TVs are additionally
controlled over the network.

---

## Architecture at a glance

- **`app.py`** — Flask app: viewer + admin routes, auth, TV control endpoints,
  Chromecast endpoints, URL-mode endpoints, RDP endpoint, and the background
  TV health-watchdog thread.
- **`plantdisplay/capture.py`** — capture engine. Displays via `mss`; windows via
  `PrintWindow` with a screen-crop fallback. On-demand capture threads publish
  JPEG frames into per-source "channels"; an MJPEG generator streams them to
  viewers. Handles minimized-window freeze-frames and label de-duplication.
- **`plantdisplay/castctl.py`** — Chromecast HLS manager. Pipes frames into
  ffmpeg at a steady 15fps and transcodes to a rolling HLS playlist.
- **`plantdisplay/discovery.py`** — SSDP TV scan + Chromecast discovery / add-by-IP.
- **`plantdisplay/tvcontrol.py`** — Samsung control (samsungtvws), Wake-on-LAN,
  **brand detection**, and the driver dispatcher.
- **`plantdisplay/hisense.py`** — Hisense/VIDAA control over MQTT (experimental).
- **`plantdisplay/urlsource.py`** — the per-TV "virtual browser" (headless
  Playwright Chromium with a command queue and persistent cookie profile).
- **`plantdisplay/templates/`** — `admin.html` (control room), `view.html` (the
  TV viewer page), `browser.html` (interactive virtual-browser UI).
- **State files:** `config.json`, `viewer_tokens.json`, `tv_tokens/`. These
  three ARE the install state — copy them to migrate.

---

## Key problems solved along the way (and the lessons)

**Hyper-V networking.** The Default Switch (NAT, 172.17.x.x) can't reach LAN
devices; you must use an **External** vSwitch so the VM lands on the TVs'
subnet. This was the first and most common blocker.

**Chromecast "received no packets" / "-22 Invalid argument".** The hardest
bug. Root causes, fixed in layers:
1. ffmpeg starved when the *live window capture* was slow (~3fps on a GPU-less
   VM) — fixed by **piping frames into ffmpeg's stdin at a steady 15fps**,
   repeating the last frame as needed, so ffmpeg's input is decoupled from
   capture speed. (This is why minimized windows, which stream a steady freeze
   frame, worked while live windows didn't.)
2. Window frames change dimensions between grabs (resize/DPI); a raw JPEG pipe
   can't tolerate that — fixed by **normalizing every piped frame to one fixed
   even size**.
*Lesson:* for HLS/ffmpeg, a steady, uniform input stream matters more than a
fast one.

**Window capture of GPU-accelerated browsers.** `PrintWindow` returns black
for GPU-composited windows when they're occluded/minimized; the screen-crop
fallback only works when the window is on top. This is a genuine Windows
limitation, not a fixable bug. Two robust answers were added instead:
- **URL mode** — render the page in our own headless browser (never occluded).
- **Virtual display** — put the app on an invisible monitor; capture via the
  fast, reliable `mss` display path.

**Admin page freezing until the browser was closed.** Classic **connection
starvation**: each live MJPEG preview holds one of the browser's ~6
connections-per-host open forever, so once enough previews were open, control
API calls queued behind them and never fired (a refresh didn't help; only
closing the page released the sockets). Fixed by: suspending previews during
control calls, aborting control calls after 8s, capping previews at 2, and a
manual **❄ Free previews** button.

**TVs dropping the viewer ("no server found").** Added a **health watchdog**:
each viewer heartbeats via its `/api/me` poll; if a TV is reachable but its
viewer stopped checking in for 90s, Plant Display re-launches the viewer
automatically. Ended the "restart the browser / restart everything" cycle.

**Multi-brand TVs.** Rather than bolt Hisense onto the Samsung path, a
**brand-driver architecture** was built: at add-time the brand is detected
(Samsung answers REST on 8001; Hisense VIDAA listens on MQTT 36669) and every
control call routes to the matching driver. Samsung is fully tested; Hisense
is experimental because VIDAA's API is undocumented and varies by model.

**URL mode / virtual browser.** Evolved from a global source list into a
**per-TV interactive browser**: tick "use a URL", press Open URL, and an
interactive window lets you scroll/zoom/click/type, then OK to stream that
exact view. Each TV's browser has its own persistent Chromium profile, so
cookies/sign-ins survive — wiped only when the URL changes or URL mode is
turned off. Engine selection falls back Chromium → Edge → Chrome so it works
even if Playwright's browser download fails.

---

## Security decisions (deliberate, please preserve)

These were chosen on purpose and should not be "simplified" away:

- **Passwords are never embedded in files or scripts.** The RDP button serves
  a prefilled `.rdp` with the *username* only; Windows handles the password via
  its own secure prompt / Credential Manager. TV deletion re-checks the admin
  password from `config.json` rather than any hardcoded value.
- **Plant Display never captures your Microsoft/SharePoint password.** To sign in
  to a page in URL mode, the **🔐 Sign in on host** button opens the browser
  profile *visibly on the host desktop* (reach it via RDP) so you type the
  password into the real browser, not through Plant Display's HTTP UI (which is
  plain HTTP on port 8090).
- **Admin console gates everything sensitive.** Note that once a URL-mode
  browser is signed in, anyone who can reach the admin console can see/click
  inside it — so use a least-privilege account for display purposes and keep
  the admin password strong. Turn off URL mode to wipe a stored session.

---

## Known limitations / honest caveats

- **Hisense/VIDAA control is experimental** — power and keys should work;
  "open browser at a specific URL" is firmware-dependent, so the home-page
  trick is the reliable path (README §12).
- **Live browser/Office window capture is imperfect on a GPU-less VM** — use
  URL mode or a virtual display for those.
- **Wake-on-LAN on Samsung** becomes unreliable after long standby (hardware
  behavior) — wired Ethernet or the TV's "Power On with Mobile/IP Remote"
  setting helps.
- **HTTP only (port 8090).** Fine on a trusted LAN; don't expose it to the
  internet. Don't type real high-value passwords into the web UI.

---

## Where to go next (ideas for a future maintainer)

- Harden the Hisense driver against real hardware once model/firmware are known.
- Optional HTTPS (self-signed) if the console ever needs to leave the LAN.
- Windows Graphics Capture (WGC) as a capture backend for occluded GPU windows
  (the modern API OBS uses) — considered but not built, since it's Windows-only
  and couldn't be tested in the build environment.
