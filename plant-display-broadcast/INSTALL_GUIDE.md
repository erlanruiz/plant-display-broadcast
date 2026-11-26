# Plant Display — Installation Guide

Plant Display broadcasts a Windows PC's screens, individual windows, or live web
pages to multiple TVs (Samsung, Hisense, and anything with a browser) and to
Chromecasts, all managed from a web control room. It was built to run on a
Windows 11 machine — including inside a Hyper-V VM — on the same LAN as the
TVs.

This guide takes you from a fresh machine to a working broadcast.

---

## 1. Requirements

- **Windows 10 (1903+) or Windows 11.** Works inside a Hyper-V VM.
- **Network:** the PC and all TVs/Chromecasts must be on the **same subnet**,
  and the network must allow devices to talk to each other (see §6 for the
  Hyper-V networking note — this is the single most common gotcha).
- Administrator rights on the PC (for the installer).
- Internet access during install (to download Python, ffmpeg, and the browser
  engine). Everything after install runs on your LAN.

You do **not** need to install Python, ffmpeg, or anything else by hand — the
installer does it all.

---

## 2. Install (the easy way)

1. Copy the whole `plantdisplay` folder onto the PC, e.g. to `C:\plantdisplay`.
2. Open that folder in File Explorer.
3. **Double-click `INSTALL.bat`.** Approve the admin prompt.

The installer runs six verified steps and stops with a clear message if any
fails (it never silently half-completes):

1. **Python 3.12.10** — direct from python.org (not winget), silent, added to
   PATH, with the `py` launcher.
2. **pip** — bootstrapped if missing.
3. **Python dependencies** — Flask, Pillow, mss, pychromecast, samsungtvws,
   paho-mqtt, Playwright — then verified by actually importing them.
4. **ffmpeg** — for Chromecast (direct download onto PATH).
5. **Playwright browser (Chromium)** — for URL mode. If this download fails,
   URL mode falls back to the Microsoft Edge already on the PC.
6. **Firewall** — opens inbound TCP 8090.

At the end it offers to launch Plant Display immediately.

### If you prefer to run it by hand (PowerShell as Administrator)

```powershell
cd C:\plantdisplay
Set-ExecutionPolicy -Scope Process Bypass -Force
.\install.ps1
```

> **Note:** the PowerShell script is `install.ps1` (lowercase). `INSTALL.bat`
> is just the double-click launcher that runs it elevated. Run it **from
> inside the plantdisplay folder** — if you paste the script into a bare
> PowerShell window it won't know where the project is.

---

## 3. First run

1. Double-click **`run.bat`** (or `py -3 app.py` from the folder).
2. The console prints two URLs, e.g.:
   - Viewer (for TVs):  `http://<PC-IP>:8090`
   - Admin console:     `http://<PC-IP>:8090/admin`
3. On the PC (or any device on the LAN), open the **admin** URL in a browser.

### Change the passwords (do this first)

Open `config.json` in the folder and change both:

- `viewer_password` — what a TV must enter to view (random at first start; shown in the console)
- `admin_password`  — protects the admin console (random at first start; shown in the console)

Save and restart `run.bat`.

---

## 4. Broadcasting to a TV

**Any TV with a browser (including Hisense):**
1. On the TV, open its web browser and go to `http://<PC-IP>:8090`.
2. Enter the viewer password.
3. In the admin console, find the TV under *Connected viewers* (or add a
   Samsung/Hisense TV by IP to control it) and pick a source for it.

**Samsung / Hisense smart TVs (full control):**
1. Admin console → **Scan network**, or **add TV by IP**.
2. The brand is detected automatically and the matching control driver loads
   (Samsung = tested; Hisense/VIDAA = experimental, see README §12).
3. Use **On / Off / Open viewer / 🎛 remote** to drive the TV. Tick
   **auto-open on power-up** so it reconnects itself when switched on.

**Sources you can send to a TV:**
- A whole display ("All displays combined" or "Display 1/2…").
- A single application window.
- A **web page** — tick *use a URL*, enter a URL, press *Open URL*, frame it
  (scroll/zoom/click) in the virtual browser, press OK. See README §14.

---

## 5. Chromecast

1. Admin console → **Chromecast** → add it by IP (in a VM, scanning usually
   finds nothing because Hyper-V blocks multicast — add the IP directly).
2. Pick a source and press **Cast**. Saved Chromecasts reconnect on restart.

Chromecast needs ffmpeg (installed in step 4). It transcodes to HLS, so
expect a few seconds of latency — that's normal.

---

## 6. Hyper-V / VM networking (read this if TVs can't be reached)

This is the most common problem. Inside a VM you **must** use an **External
virtual switch** bound to a physical NIC. The "Default Switch" (NAT,
172.17.x.x) **cannot reach other devices on your LAN**, so TVs and Chromecasts
will be invisible.

1. Hyper-V Manager → **Virtual Switch Manager** → New → **External** → bind to
   the host's physical network adapter.
2. Attach the VM to that switch.
3. The VM should now get an IP in the **same range as your TVs**.
4. On the wireless network, disable "AP isolation"/"client isolation" so
   devices can talk to each other.

Reserve IPs (DHCP reservations) for the PC, the TVs, and the Chromecasts —
every feature is keyed to IP, so a changed lease breaks assignments.

---

## 7. Keeping it running

- **Auto-start on boot:** put a shortcut to `run.bat` in the Startup folder
  (`shell:startup`), or create a Scheduled Task set to "run whether logged on
  or not."
- **Backup / migrate:** these three items ARE the entire state of your
  install — copy them to move to another machine:
  - `config.json` (TVs, names, Chromecasts, URL sources, passwords, settings)
  - `viewer_tokens.json`
  - `tv_tokens\` folder (Samsung pairing tokens)

---

## 8. Optional: the robust path for live browser/PowerPoint content

Capturing a hardware-accelerated browser or Office window while it's covered
or minimized is unreliable on a GPU-less VM (it can go black). Two solid
alternatives:

- **URL mode** (built in) — renders the page in Plant Display's own browser; never
  goes black. Best for dashboards and web content. See README §14.
- **Virtual display driver** — adds an invisible monitor you can put any app on
  at full quality; broadcast that display. See `VIRTUAL_DISPLAY_SETUP.md`.

---

## 9. Troubleshooting quick reference

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: flask` on start | `py -3 -m pip install -r requirements.txt`, then confirm `py -3 -m pip show flask`. Ensure `run.bat` uses the same Python. |
| Installer: `Split-Path ... null` | Run it from inside the folder: `cd C:\plantdisplay` then `.\install.ps1` (don't paste the script into a bare prompt). |
| TVs not found / "no server found" | Hyper-V External switch (§6); same subnet; firewall rule for 8090. |
| Admin page freezes until you close the browser | Fixed in current build (connection-starvation). Use the **❄ Free previews** button if it ever feels stuck. |
| Chromecast: "no packets" / won't play | ffmpeg installed? Current build pipes frames at a steady rate and normalizes size — update if you're on an old copy. |
| Virtual browser: "Executable doesn't exist" | `py -3 -m playwright install chromium`. It also auto-falls back to Microsoft Edge. |
| Wake-on-LAN unreliable on Samsung after long standby | Known Samsung hardware behavior; use wired Ethernet or enable "Power On with Mobile/IP Remote" on the TV. |

For full feature detail, see **README.md** (numbered sections per feature).
For the design history and why things are built as they are, see
**PROJECT_HISTORY.md**.
