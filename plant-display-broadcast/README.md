> **Plant Display Broadcast** — free software that shows live production dashboards, screens, or web
> pages on many factory TVs from one Windows PC, without a computer behind each TV. Password
> protected, with a control-room console to choose what each TV shows.
>
> MIT License (see `LICENSE`). Author: Erlan René Ruiz Nicaragua. Samsung, Hisense, Chromecast, Tizen, and other
> product names belong to their owners; this project is independent and not affiliated with them.
> Passwords are created at random on first start and stored only in your local `config.json`
> (listed in `.gitignore`). Hisense control reads its login from the environment variables
> `HISENSE_MQTT_USER` and `HISENSE_MQTT_PASSWORD`.

# Plant Display

Broadcast your Windows 11 screen(s) — or a single window — to **any number of
Samsung TVs and Chromecasts at the same time**, over your wired/Wi-Fi network,
with **password-protected** access and a control-room web console to decide
which screen goes to which TV.

```
 Windows 11 PC / Hyper-V VM                       Your network
 ┌──────────────────────────┐
 │  Plant Display (python)     │   MJPEG    ┌─────────────┐  TV browser or
 │  · captures displays     │──────────▶│ Samsung TV 1 │  sideloaded app
 │    and windows           │   MJPEG    ├─────────────┤
 │  · admin console :8090   │──────────▶│ Samsung TV 2 │
 │  · ffmpeg → HLS          │   HLS      ├─────────────┤
 │    (for Chromecast)      │──────────▶│ Chromecast   │
 └──────────────────────────┘            └─────────────┘
```

---

## 1. Install (on the Windows 11 PC or VM)

1. Install **Python 3.10+** from https://python.org (check *"Add to PATH"*).
2. Double-click **`install.bat`** (installs Flask, mss, Pillow, pychromecast, pywin32).
3. *(Only needed for Chromecast)* install ffmpeg:
   `winget install Gyan.FFmpeg` — then open a new terminal so PATH updates.
4. Right-click **`allow_firewall.bat`** → *Run as administrator* (opens TCP 8090).
5. Edit **`config.json`** and change both passwords:

```json
{
  "port": 8090,
  "viewer_password": "<random at first start>",   <- what TVs must type
  "admin_password": "<random at first start>",    <- the control room
  "fps": 15,
  "assignments": {}
}
```

6. Double-click **`run.bat`**. It prints the URLs:

```
TVs / browsers : http://<PC-IP>:8090
Admin console  : http://<PC-IP>:8090/admin
```

## 2. Connect a Samsung TV (no install needed)

1. On the TV open the **Internet / Web Browser** app.
2. Go to `http://<PC-IP>:8090`.
3. The TV asks for the **viewer password** → enter it with the remote.
4. The TV then shows either:
   - the source you **assigned to its IP** in the admin console (instant,
     fullscreen), or
   - a **source picker** (every display + every window) if nothing is assigned.

The session is remembered in a cookie, so the TV won't ask for the password
again until you change it. Changing the viewer password in the admin console
immediately logs out every TV.

## 3. The control room (`/admin`)

- **Sources wall** — every display and every open window, with click-to-toggle
  live previews and an **ON AIR** badge on anything currently being watched.
- **Samsung / smart TVs** — *Scan network* finds TVs via SSDP, or add any
  device by IP. Pick a source in the dropdown next to a TV and that's what it
  plays when it loads the viewer page (it can be the same source on many TVs —
  that's your *duplicate* mode).
- **Chromecast** — *Scan*, pick a source, press **Cast**. Plant Display spins up
  an ffmpeg HLS transcode and tells the Chromecast to play it. Press **Stop**
  to end it. (Expect ~3–6 s latency on Chromecast; the TV-browser MJPEG path
  is sub-second.)
- **Connected viewers / assignments** — see and remove IP→source routings.
- **Viewer password** — change it from the header at any time.

## 4. Duplicate / Extend / Single window

| Mode | How |
|---|---|
| **Duplicate** | Assign the *same* Display source to several TVs. Every TV shows a copy of that screen. |
| **Single window** | Assign a *Window* source (e.g. just PowerPoint, just a dashboard browser window). The window is captured even when other windows cover it. |
| **Extend** | Windows needs a monitor to exist before you can drag things onto it. Install a free **virtual display driver**, and the new fake monitor shows up in Plant Display as another Display source you can route to a TV: |

Virtual display drivers (pick one):
- **Virtual Display Driver** — https://github.com/VirtualDrivers/Virtual-Display-Driver
  (signed IDD driver; add as many virtual monitors as you want, any resolution)
- **usbmmidd_v2 (Amyuni)** — https://www.amyuni.com/forum/viewtopic.php?t=3030
  (`deviceinstaller64 enableidd 1` per extra screen)

After installing, press **Refresh sources** in the admin console: "Display 2/3/…"
appears → drag windows onto it in Windows → assign it to a TV. The TV is now a
true wireless extended monitor.

> Tip for Hyper-V: virtual display drivers work fine inside a VM (they don't
> need a GPU), which is exactly what makes this work where Miracast can't.

## 5. Optional: install the Plant Display app on the TV ("injection" by IP)

Samsung Tizen TVs can't be pushed an app silently — Samsung requires the TV to
be in **Developer Mode**, after which you *can* install the included app over
the network by IP. One-time, per TV:

1. On the TV: open **Apps**, type `12345` on the remote → Developer mode **ON**,
   enter your **PC's IP**, restart the TV.
2. On the PC install **Tizen Studio** (with TV extension) from
   https://developer.tizen.org, create a Samsung certificate
   (Tools → Certificate Manager), then:

```bat
sdb connect <TV-IP>:26101
tizen package -t wgt -s <YourCertProfile> -- tizen-app
tizen install -n tizen-app\PlantDisplay.wgt -t <TV-name-from-"sdb devices">
```

3. The **Plant Display** app appears in the TV's app list. First launch asks for
   the server IP (remembered afterwards; red button on the remote resets it),
   then loads the same password-protected viewer.

If you skip this, the TV browser does exactly the same job — the app is just a
nicer icon + remembered server.

## 6. Chromecast notes

- Needs ffmpeg on the PC (see install step 3). The admin header shows a red
  badge if it's missing.
- The Chromecast pulls `http://<PC-IP>:8090/hls/...` directly from Plant Display;
  both must be able to reach each other on the network.
- Chromecast with Google TV also has a Chrome/browser-free path only via this
  cast flow — there is no browser on the device to type a URL into.

## 7. Performance & troubleshooting

- **TVs don't find the page** → check `allow_firewall.bat` was run, the PC and
  TVs are on the same VLAN, and **AP/client isolation is disabled** on the
  wireless VLAN (it silently blocks TV→PC traffic).
- **Hyper-V VM** → use an **External vSwitch** so the VM has a real LAN IP.
- **Choppy with many TVs** → lower `"fps"` in config.json (10 is fine for
  slides/dashboards), or reduce capture size by editing `MAX_DIMENSION` in
  `plantdisplay/capture.py`. Each TV pulls its own copy of the stream, so
  bandwidth ≈ stream size × number of TVs.
- **Window capture is black** → some hardware-accelerated apps block capture;
  capture the whole display instead, or disable hardware acceleration in that
  app.
- **No audio** → this build streams video only (TV browsers can't autoplay
  audio inside an MJPEG stream). For audio on Chromecast, ask and the ffmpeg
  pipeline can be extended with a loopback audio device (VB-Cable).
- **SSDP scan finds nothing** → some TVs only answer when on; you can always
  just open the viewer URL on the TV (it self-registers the moment it logs in)
  or add it by IP manually.

## Security model

- Viewers (TVs) must enter the **viewer password**; streams require the token
  it grants. Changing the password revokes every TV at once.
- The **admin console** has its own password.
- Everything stays on your LAN; nothing touches the internet.
- This is a LAN tool over HTTP — don't port-forward it to the internet.

## 8. Remote TV power & launch (no app install on the TV)

Plant Display can manage Samsung TVs directly from the admin console using
Samsung's own network APIs — nothing is installed on the TV:

| Button | What it does | One-time TV setting |
|---|---|---|
| **⏻ On** | Wake-on-LAN magic packet to the TV's MAC | Settings → General → Network → Expert Settings → **Power On with Mobile / IP Remote: ON** |
| **Off** | Sends the power key over Samsung's WebSocket remote API (port 8002) | Accept the **"Allow Plant Display?"** popup on the TV the first time |
| **Open viewer** | Remotely launches the TV's web browser at the viewer page, **pre-authenticated** with a one-time token, playing the TV's assigned source | Same popup, same one-time accept |

TVs are remembered in `config.json` (`"tvs"`), including their MAC, so they
survive restarts. **Scan network** auto-saves everything it finds; you can also
add a TV by IP. Pairing tokens are stored in `tv_tokens/`.

Notes & limits:
- Browsers cannot inject persistent background apps into Tizen — Samsung's
  sandbox forbids it by design. The WebSocket API above is the supported way
  to get the same outcomes (power, launch, control).
- Wake-on-LAN over Wi-Fi (WoWLAN) works on most 2018+ models when the setting
  above is on; wired Ethernet is the most reliable.
- "Off" actually sends the power toggle key; on some models a long-press
  behaviour applies. If the TV is already off, use **⏻ On** instead.
- The first WebSocket command after a TV factory reset re-triggers the Allow
  popup (token changes).

## 9. Auto-connect on power-up & virtual remote

- **auto-open on power-up** (checkbox on each TV row): Plant Display watches the
  TV every ~12 s; the moment it comes online — whether you pressed **⏻ On** in
  the console or someone used the physical remote — Plant Display waits a few
  seconds for Tizen to boot and remotely launches the browser on the viewer
  page, pre-authenticated and playing that TV's assigned source. Fully
  hands-off: power the TV, get the broadcast.
- **🎛 virtual remote** (per TV): power, home, source, menu, arrows/OK, back,
  exit, volume, mute, play/pause — sent over the same paired WebSocket API,
  so you can drive the TV entirely from the admin page.
- "Watch what the TV shows": the TV displays a Plant Display source, so the live
  preview of that source on the Sources wall *is* what the TV is showing.
  (TVs don't expose their own screen as an outbound stream, so this is as
  close as any LAN tool can get.)

## 10. Broadcasting minimized windows

Windows stops rendering an app the moment it is minimized — no capture API
can stream a window that produces no frames. Plant Display handles this with a
**freeze-frame**: minimize a broadcast window and the TVs keep showing the
last live frame (re-sent as a heartbeat so every playback mode stays alive);
restore the window and the live stream resumes by itself. Nothing is moved
or restored on your desktop.

For a *live* feed of an app you never want on a physical screen, use a
**virtual display** (section 4 "Extend"): the app stays rendered there and
streams continuously, while no monitor ever shows it.

## 11. Remote control of the host (Remote Desktop)

The admin header has a **🖥 Remote control** button. It hands your browser a
prefilled `.rdp` file pointing at this host, which launches the native Windows
Remote Desktop client straight to the machine — so you can rearrange windows,
open apps, and change what's being broadcast from anywhere.

- **Windows performs its own secure login.** Plant Display never captures, stores,
  or replays your account password — you type it into the OS prompt, not into
  Plant Display.
- One-time setup on the host: enable **Settings → System → Remote Desktop**.
  The button warns you if it detects RDP is off.
- Note: a standard RDP login opens its own session and will lock the local
  console session. If you need to see the *same* session you're broadcasting,
  keep using the live previews; RDP is for making changes, not mirroring the
  exact console.

## 12. Multi-brand TVs (Samsung + Hisense)

When you add or scan a TV, Plant Display auto-detects its brand and loads the
matching control driver:

- **Samsung** — detected via its REST API (port 8001). Full support: power
  on/off, Open viewer, virtual remote (WebSocket port 8002). Tested.
- **Hisense (VIDAA)** — detected via its MQTT API (port 36669). Power, remote
  keys, and best-effort browser launch. **Experimental** — VIDAA's API is
  undocumented and varies by model/year; verify on your set. Needs
  `paho-mqtt` (`py -3 -m pip install paho-mqtt`).
- **Broadcasting** (the viewer page) works on any TV with a browser regardless
  of brand — open `http://<PC-IP>:8090`, enter the password.

The detected brand shows in each TV's row. Control buttons route automatically
to the right protocol.

**Hisense "Open viewer" caveat:** VIDAA can launch the browser app but does
not reliably accept a target URL on all firmwares. For dependable auto-load,
use the home-page trick: open the Hisense browser once, go to the viewer URL,
log in, and set it as the browser's home page. Then "Open viewer" (or just
opening the browser) lands on Plant Display.

## 13. TV connection watchdog (auto-reconnect)

Plant Display now monitors every saved TV every 15 seconds:

- Each viewer page sends a heartbeat (its `/api/me` poll). If a TV is
  reachable on the network but its viewer page has stopped checking in for
  90 s — the browser crashed, the TV dropped the page, "no server found" —
  Plant Display **automatically re-launches the viewer on that TV**.
- Each TV row shows live status: *viewer live* / *viewer stale — reconnecting*
  / *TV offline*, plus how many auto-reconnects have happened.

This is what removes the "restart the browser / restart the whole system"
cycle. If a TV still needs manual help, its row will say why.

**Removing a TV now requires the admin password** — deleting wipes its
pairing token and assignment, so it asks for confirmation.

## 14. URL mode — a virtual browser per TV

Each TV row has a **use a URL** checkbox. Tick it, type a URL, press
**🌐 Open URL** and Plant Display starts a dedicated headless Chromium for that
TV and opens an interactive window (`/admin/browser?ip=…`) where you can:

- **scroll** (▲ ▼ / PageUp PageDown) and **zoom** (➕ ➖, 25%–400%)
- **click** anywhere on the image to click that spot in the real page
- **type** into the page, press Enter/Tab, navigate with Go / Back / Reload
- press **✓ OK** to commit the view — the TV immediately starts showing it

Re-pressing **Open URL** later reopens the window **exactly where you left
off**: the browser session stays alive, and the scroll/zoom you committed are
restored (they also survive a Plant Display restart).

Why this beats capturing a browser window: the page renders in its own
browser at a fixed 1920×1080, so it never goes black when covered, needs no
window on your desktop, and doesn't depend on the GPU.

### If the virtual browser says "Executable doesn't exist"

Playwright's pip package ships without browser binaries. Install one:

    py -3 -m playwright install chromium

Plant Display also falls back automatically, in order, to: Playwright's full
Chromium build, then **Microsoft Edge**, then Chrome — whichever is present
on the host. Since Edge ships with Windows 11, URL mode works even if the
Playwright download fails. The engine in use is logged to the console and
shown in the virtual browser's status line.

### Cookies and sign-ins

Each TV's virtual browser has its own real Chromium profile on disk, so
cookies and sign-ins **persist** — including across restarts. The profile is
deleted only when:

- you **change the URL** for that TV, or
- you **untick "use a URL"** (you'll be asked to confirm).

**To sign in to a page (Microsoft 365 / SharePoint / anything):** press
**🔐 Sign in on host** in the virtual browser window. That opens the same
profile *visibly on the Plant Display host desktop* — reach it with the admin
page's **Remote control** (RDP) button and type your password directly into
the real browser window. Streaming resumes when you close it. Your password
never passes through Plant Display or over the network to it.

**Security note, worth understanding:** once signed in, that virtual browser
holds a live authenticated session, and anyone who can reach the admin
console can view and click inside it. Use a least-privilege account for
display purposes rather than an admin one, keep the admin password strong,
and untick "use a URL" when a board is retired (that wipes the session).
