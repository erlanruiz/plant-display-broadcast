"""
Plant Display — broadcast your Windows 11 screen(s) to Samsung TVs (browser /
Tizen app) and Chromecasts, with password protection and per-device routing.

Run:  python app.py            (or run.bat)
TVs:  open http://<PC-IP>:8090 in the TV browser and enter the password.
You:  open http://localhost:8090/admin to manage everything.
"""

import json
import os
import secrets
import socket
import sys
import threading
import time

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_from_directory)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plantdisplay import capture
from plantdisplay.capture import HUB, mjpeg_stream, list_monitors, list_windows
from plantdisplay.discovery import scan_ssdp, CASTS
from plantdisplay.castctl import HLS, HLS_ROOT, ffmpeg_available
from plantdisplay import tvcontrol
from plantdisplay import urlsource
from plantdisplay.urlsource import SESSIONS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

app = Flask(__name__,
            template_folder=os.path.join(BASE_DIR, "plantdisplay", "templates"))

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG = {
    "port": 8090,
    "viewer_password": "",            # set at first start (random) — see config.json
    "admin_password": "",             # set at first start (random) — see config.json
    "fps": 15,
    "assignments": {},                # "<tv-ip>" -> "monitor:1"
    "tvs": {},                        # "<tv-ip>" -> {name, mac}
    "chromecasts": {},                # "<cast-ip>" -> {name, model}
    "urls": {},                       # slug -> saved URL source
    "rdp_username": "",               # optional: prefilled in the RDP shortcut (no password)
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as exc:
            print(f"[!] Could not read config.json ({exc}); using defaults")
    # Never run with an empty password: create random ones on first start.
    changed = not os.path.exists(CONFIG_PATH)
    for key, size in (("viewer_password", 9), ("admin_password", 12)):
        if not str(cfg.get(key, "")).strip():
            cfg[key] = secrets.token_urlsafe(size)
            changed = True
    if changed:
        save_config(cfg)
        print("[i] config.json holds the viewer and admin passwords (random on first start).")
        print(f"    viewer password: {cfg['viewer_password']}")
        print(f"    admin password : {cfg['admin_password']}")
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


CFG = load_config()
CFG_LOCK = threading.Lock()

# --------------------------------------------------------------------------- #
# Auth — viewer tokens (TVs) and admin tokens, plus an internal token that
# the local ffmpeg/Chromecast pipeline uses.
# --------------------------------------------------------------------------- #
VIEWER_TOKENS = set()
ADMIN_TOKENS = set()
INTERNAL_TOKEN = secrets.token_urlsafe(24)
RELOAD_SIGNALS = {}        # viewer ip -> counter; bump to force that TV to reload
TOKENS_PATH = os.path.join(BASE_DIR, "viewer_tokens.json")

try:  # viewer sessions survive restarts so TVs don't get logged out
    with open(TOKENS_PATH, encoding="utf-8") as _f:
        VIEWER_TOKENS.update(json.load(_f))
except Exception:
    pass


def _save_tokens():
    try:
        with open(TOKENS_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(VIEWER_TOKENS), f)
    except Exception:
        pass


def client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "?")\
                  .split(",")[0].strip()


def has_viewer_access():
    tok = request.cookies.get("sb_token") or request.args.get("token")
    if tok in VIEWER_TOKENS or tok in ADMIN_TOKENS or tok == INTERNAL_TOKEN:
        return True
    # per-TV opt-in: admin marked this device's IP as trusted (no password)
    ip = client_ip()
    with CFG_LOCK:
        tv = CFG.get("tvs", {}).get(ip)
    return bool(tv and tv.get("trusted"))


def has_admin_access():
    tok = request.cookies.get("sb_admin") or request.args.get("admin_token")
    return tok in ADMIN_TOKENS


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Viewer routes (what the TV uses)
# --------------------------------------------------------------------------- #
@app.route("/")
def viewer():
    return render_template("view.html")


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    pwd = str(data.get("password", ""))
    if secrets.compare_digest(pwd, CFG["viewer_password"]):
        tok = secrets.token_urlsafe(24)
        VIEWER_TOKENS.add(tok)
        _save_tokens()
        resp = jsonify(ok=True, token=tok)
        resp.set_cookie("sb_token", tok, max_age=365 * 24 * 3600,
                        httponly=False, samesite="Lax")
        return resp
    return jsonify(ok=False, error="Wrong password"), 401


@app.route("/api/me")
def me():
    ip = client_ip()
    if not has_viewer_access():
        return jsonify(authed=False, ip=ip)
    with CFG_LOCK:
        assigned = CFG["assignments"].get(ip)
    VIEWER_SEEN[ip] = time.time()          # heartbeat for the TV watchdog
    return jsonify(authed=True, ip=ip, assigned=assigned,
                   reload=RELOAD_SIGNALS.get(ip, 0))


@app.route("/api/channels")
def channels():
    if not has_viewer_access():
        return jsonify(error="auth"), 401
    return jsonify(monitors=list_monitors(),
                   windows=list_windows())


@app.route("/stream/<path:channel_id>")
def stream(channel_id):
    if not has_viewer_access():
        return Response("Password required", status=401)
    fps = int(request.args.get("fps", CFG.get("fps", 15)))
    gen = mjpeg_stream(channel_id, fps=max(1, min(30, fps)))
    return Response(
        gen, mimetype="multipart/x-mixed-replace; boundary=plantdisplayframe",
        headers={"Cache-Control": "no-cache, no-store",
                 "X-Accel-Buffering": "no"})


@app.route("/frame/<path:channel_id>")
def single_frame(channel_id):
    """One JPEG snapshot — fallback for TV browsers that can't render MJPEG."""
    if not has_viewer_access():
        return Response("Password required", status=401)
    ch = HUB.get(channel_id, int(CFG.get("fps", 15)))
    _, frame = ch.wait_frame(ch.seq, timeout=5)   # wait for the next frame
    if frame is None:
        _, frame = ch.wait_frame(ch.seq, timeout=5)
    if frame is None:
        return Response("No frame yet", status=503)
    return Response(frame, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-cache, no-store"})


@app.route("/hls/<path:filename>")
def hls_files(filename):
    # Playlists/segments are unguessable only via the casting flow; still keep
    # them inside the temp dir and never accept absolute paths.
    resp = send_from_directory(HLS_ROOT, filename, conditional=True)
    if filename.endswith(".m3u8"):
        resp.headers["Content-Type"] = "application/vnd.apple.mpegurl"
    elif filename.endswith(".ts"):
        resp.headers["Content-Type"] = "video/mp2t"
    resp.headers["Cache-Control"] = "no-cache, no-store"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


# --------------------------------------------------------------------------- #
# Admin routes
# --------------------------------------------------------------------------- #
@app.route("/admin")
def admin_page():
    return render_template("admin.html")


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json(silent=True) or {}
    if secrets.compare_digest(str(data.get("password", "")),
                              CFG["admin_password"]):
        tok = secrets.token_urlsafe(24)
        ADMIN_TOKENS.add(tok)
        resp = jsonify(ok=True)
        resp.set_cookie("sb_admin", tok, max_age=24 * 3600, samesite="Lax")
        resp.set_cookie("sb_token", tok, max_age=24 * 3600, samesite="Lax")
        return resp
    return jsonify(ok=False, error="Wrong password"), 401


def admin_required(fn):
    from functools import wraps

    @wraps(fn)
    def inner(*a, **kw):
        if not has_admin_access():
            return jsonify(error="auth"), 401
        return fn(*a, **kw)
    return inner


@app.route("/api/admin/state")
@admin_required
def admin_state():
    with CFG_LOCK:
        assignments = dict(CFG["assignments"])
        tvs = {ip: dict(info) for ip, info in CFG.get("tvs", {}).items()}
        chromecasts = {ip: dict(info)
                       for ip, info in CFG.get("chromecasts", {}).items()}
    health = {}
    now = time.time()
    for ip in tvs:
        h = TV_HEALTH.get(ip, {})
        seen = VIEWER_SEEN.get(ip, 0)
        health[ip] = {
            "online": h.get("online"),
            "viewer_age": int(now - seen) if seen else None,
            "reconnects": h.get("reconnects", 0),
        }
    return jsonify(
        server_ip=lan_ip(),
        port=CFG["port"],
        viewer_password=CFG["viewer_password"],
        ffmpeg=ffmpeg_available(),
        monitors=list_monitors(),
        windows=list_windows(),
        assignments=assignments,
        tvs=tvs,
        chromecasts=chromecasts,
        health=health,
        active_channels=sorted(HUB.channels.keys()),
        active_casts=sorted(HLS.procs.keys()),
    )


@app.route("/api/admin/rdp")
@admin_required
def rdp_status():
    """Report whether RDP is enabled on the host, for the admin UI hint."""
    enabled = None
    if sys.platform == "win32":
        try:
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Terminal Server")
            val, _ = winreg.QueryValueEx(k, "fDenyTSConnections")
            enabled = (val == 0)
            winreg.CloseKey(k)
        except Exception:
            enabled = None
    return jsonify(host=lan_ip(), enabled=enabled)


@app.route("/admin/host.rdp")
def host_rdp():
    """Serve a prefilled .rdp file that launches the native Remote Desktop
    client straight to this host. Windows performs its own secure login —
    Plant Display never handles the password."""
    if not has_admin_access():
        return Response("admin only", status=401)
    host = lan_ip()
    user = CFG.get("rdp_username", "")
    rdp = (
        f"full address:s:{host}:3389\r\n"
        + (f"username:s:{user}\r\n" if user else "")
        + "prompt for credentials:i:0\r\n"
        "authentication level:i:0\r\n"
        "enablecredsspsupport:i:1\r\n"
        "administrative session:i:0\r\n"
        "screen mode id:i:2\r\n"
        "use multimon:i:0\r\n"
        "desktopwidth:i:1920\r\n"
        "desktopheight:i:1080\r\n"
        "session bpp:i:32\r\n"
        "audiomode:i:0\r\n"
        "redirectclipboard:i:1\r\n"
        "autoreconnection enabled:i:1\r\n"
    )
    return Response(
        rdp, mimetype="application/x-rdp",
        headers={"Content-Disposition":
                 'attachment; filename="plantdisplay-host.rdp"'})


@app.route("/api/admin/sources")
@admin_required
def admin_sources():
    """Lightweight poll target: just the current displays and windows."""
    return jsonify(monitors=list_monitors(), windows=list_windows(),
                   urls=SESSIONS.list())


def _url_slug(ip):
    return "tv_" + ip.replace(".", "_")


def _tv_url_session(ip):
    s = SESSIONS.get(_url_slug(ip))
    if s is None:
        raise KeyError("URL mode is not enabled for this TV")
    return s


@app.route("/api/admin/tv/urlmode", methods=["POST"])
@admin_required
def tv_urlmode():
    """Turn URL mode on/off for one TV.

    on  -> create (or reuse) a virtual browser for that TV
    off -> stop it, wipe its cookies/profile, clear the assignment
    """
    data = request.get_json(silent=True) or {}
    ip = data.get("ip")
    on = bool(data.get("on"))
    url = (data.get("url") or "").strip()
    slug = _url_slug(ip)

    with CFG_LOCK:
        tv = CFG.get("tvs", {}).get(ip)
        if tv is None:
            return jsonify(error="unknown TV"), 400

    if not on:                                  # ---- unchecked: tear down
        SESSIONS.drop(slug, wipe=True)          # deletes the cookie profile
        with CFG_LOCK:
            tv.pop("url", None)
            tv["use_url"] = False
            if CFG["assignments"].get(ip, "").startswith("url:"):
                CFG["assignments"].pop(ip, None)
            CFG.get("urls", {}).pop(slug, None)
            save_config(CFG)
        return jsonify(ok=True, use_url=False)

    if not url:
        return jsonify(error="Enter a URL first"), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not urlsource.playwright_available():
        return jsonify(error="The virtual browser needs Playwright. Run: "
                       "py -3 -m pip install playwright  then  py -3 -m "
                       "playwright install chromium  and restart Plant Display."
                       ), 400

    existing = SESSIONS.get(slug)
    if existing and existing.url == url and existing.thread \
            and existing.thread.is_alive():
        sess = existing                          # keep session AND cookies
    else:
        prev = None
        with CFG_LOCK:
            prev = (CFG.get("urls", {}).get(slug) or {})
        sess = SESSIONS.create(slug, url)        # wipes cookies if URL changed
        if prev.get("url") == url:               # same page: restore the view
            sess.zoom = float(prev.get("zoom", 1.0))
            sess.scroll_y = int(prev.get("scroll_y", 0))
        sess.start(HUB.get(f"url:{slug}", 2))
        sess.ready.wait(timeout=40)

    with CFG_LOCK:
        tv["use_url"] = True
        tv["url"] = url
        CFG.setdefault("urls", {})[slug] = sess.to_dict()
        save_config(CFG)
    return jsonify(ok=True, use_url=True, source=sess.to_dict(),
                   id=f"url:{slug}", error=sess.error)


@app.route("/admin/browser")
def admin_browser():
    """The interactive virtual-browser page (opened in its own window)."""
    if not has_admin_access():
        return redirect("/admin")
    ip = request.args.get("ip", "")
    with CFG_LOCK:
        tv = CFG.get("tvs", {}).get(ip) or {}
    return render_template("browser.html", ip=ip,
                           tv_name=tv.get("name") or ip,
                           url=tv.get("url", ""))


@app.route("/api/admin/url/act", methods=["POST"])
@admin_required
def url_act():
    """Drive the virtual browser: scroll / zoom / click / type / navigate."""
    data = request.get_json(silent=True) or {}
    try:
        sess = _tv_url_session(data.get("ip"))
    except KeyError as exc:
        return jsonify(error=str(exc)), 404
    op = data.get("op", "state")
    try:
        state = sess.call(op, **{k: v for k, v in data.items()
                                 if k in ("dy", "zoom", "x", "y", "text",
                                          "key", "url")})
        return jsonify(ok=True, source=state)
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/admin/url/snap")
def url_snap():
    """Current frame of a TV's virtual browser (single JPEG, short-lived
    connection — deliberately not a persistent MJPEG stream)."""
    if not has_admin_access():
        return Response("admin only", status=401)
    ip = request.args.get("ip", "")
    ch = HUB.get(f"url:{_url_slug(ip)}", 2)
    frame = ch.frame
    if frame is None:
        return Response(status=204)
    return Response(frame, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.route("/api/admin/url/commit", methods=["POST"])
@admin_required
def url_commit():
    """OK pressed: remember the view and start streaming it to the TV."""
    data = request.get_json(silent=True) or {}
    ip = data.get("ip")
    try:
        sess = _tv_url_session(ip)
    except KeyError as exc:
        return jsonify(error=str(exc)), 404
    slug = _url_slug(ip)
    with CFG_LOCK:
        CFG["assignments"][ip] = f"url:{slug}"
        CFG.setdefault("urls", {})[slug] = sess.to_dict()
        save_config(CFG)
    RELOAD_SIGNALS[ip] = RELOAD_SIGNALS.get(ip, 0) + 1   # nudge the TV page
    return jsonify(ok=True, assigned=f"url:{slug}")


@app.route("/api/admin/url/signin", methods=["POST"])
@admin_required
def url_signin():
    """Open this TV's virtual browser VISIBLY on the host so a person can sign
    in by hand (use the Remote control / RDP button to reach the desktop).
    The password never passes through Plant Display."""
    data = request.get_json(silent=True) or {}
    ip = data.get("ip")
    try:
        sess = _tv_url_session(ip)
    except KeyError as exc:
        return jsonify(error=str(exc)), 404

    def run():
        try:
            sess.signin_headed(minutes=10)
        except Exception as exc:
            print(f"[signin] {ip}: {exc}")
        finally:
            sess.stop_flag.clear()
            sess.start(HUB.get(f"url:{_url_slug(ip)}", 2))
    threading.Thread(target=run, daemon=True).start()
    return jsonify(ok=True, note="A browser window is opening on the host "
                   "desktop — connect with Remote control and sign in there. "
                   "Streaming resumes when you close it (or after 10 min).")


@app.route("/api/admin/scan/tvs", methods=["POST"])
@admin_required
def scan_tvs():
    try:
        return jsonify(devices=scan_ssdp())
    except Exception as exc:
        return jsonify(devices=[], error=str(exc))


@app.route("/api/admin/scan/chromecasts", methods=["POST"])
@admin_required
def scan_ccs():
    devices, errors = [], []
    try:                                   # 1) normal mDNS scan
        devices = CASTS.scan()
    except Exception as exc:
        errors.append(f"mDNS: {exc}")
    if not devices:                        # 2) mDNS found nothing (VM): SSDP
        try:
            from plantdisplay.discovery import scan_chromecasts_ssdp
            for cand in scan_chromecasts_ssdp():
                try:
                    devices.append(CASTS.add_by_ip(cand["ip"]))
                except Exception as exc:
                    errors.append(f'{cand["ip"]}: {exc}')
        except Exception as exc:
            errors.append(f"SSDP: {exc}")
    return jsonify(devices=devices,
                   error=("; ".join(errors)[:300] if not devices and errors
                          else None))


@app.route("/api/admin/assign", methods=["POST"])
@admin_required
def assign():
    data = request.get_json(silent=True) or {}
    ip, channel = data.get("ip"), data.get("channel")
    if not ip:
        return jsonify(error="ip required"), 400
    with CFG_LOCK:
        if channel:
            CFG["assignments"][ip] = channel
        else:
            CFG["assignments"].pop(ip, None)
        save_config(CFG)
    return jsonify(ok=True, assignments=CFG["assignments"])


@app.route("/api/admin/password", methods=["POST"])
@admin_required
def set_password():
    data = request.get_json(silent=True) or {}
    new = str(data.get("viewer_password", "")).strip()
    if len(new) < 4:
        return jsonify(error="Password must be at least 4 characters"), 400
    with CFG_LOCK:
        CFG["viewer_password"] = new
        save_config(CFG)
    VIEWER_TOKENS.clear()   # force every TV to re-enter the new password
    _save_tokens()
    return jsonify(ok=True)


@app.route("/api/admin/cast/add", methods=["POST"])
@admin_required
def cast_add():
    """Add a Chromecast by IP (for VMs where mDNS discovery is blocked)."""
    data = request.get_json(silent=True) or {}
    ip = (data.get("ip") or "").strip()
    print(f"[cast/add] request for ip={ip!r}")
    if not ip:
        return jsonify(error="ip required"), 400
    try:
        dev = CASTS.add_by_ip(ip)
        print(f"[cast/add] connected: {dev}")
        with CFG_LOCK:
            CFG.setdefault("chromecasts", {})[ip] = {
                "name": dev["name"], "model": dev.get("model", "")}
            save_config(CFG)
        return jsonify(ok=True, device=dev)
    except Exception as exc:
        print(f"[cast/add] FAILED for {ip}: {exc}")
        return jsonify(error=str(exc)), 500


@app.route("/api/admin/cast/forget", methods=["POST"])
@admin_required
def cast_forget():
    data = request.get_json(silent=True) or {}
    ip = data.get("ip")
    with CFG_LOCK:
        CFG.get("chromecasts", {}).pop(ip, None)
        save_config(CFG)
    return jsonify(ok=True)


@app.route("/api/admin/cast/start", methods=["POST"])
@admin_required
def cast_start():
    data = request.get_json(silent=True) or {}
    uuid, channel = data.get("uuid"), data.get("channel")
    if not uuid or not channel:
        return jsonify(error="uuid and channel required"), 400
    base = f"http://{lan_ip()}:{CFG['port']}"
    try:
        rel = HLS.ensure(channel)
        CASTS.play(uuid, f"{base}/hls/{rel}", title=f"Plant Display {channel}")
        return jsonify(ok=True)
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/admin/cast/stop", methods=["POST"])
@admin_required
def cast_stop():
    data = request.get_json(silent=True) or {}
    uuid, channel = data.get("uuid"), data.get("channel")
    if uuid:
        CASTS.stop(uuid)
    if channel:
        HLS.stop(channel)
    return jsonify(ok=True)


@app.route("/api/admin/tv/register", methods=["POST"])
@admin_required
def tv_register():
    """Save a TV (IP + name + MAC) so it persists and can be power-controlled."""
    data = request.get_json(silent=True) or {}
    ip = (data.get("ip") or "").strip()
    if not ip:
        return jsonify(error="ip required"), 400
    info = tvcontrol.tv_info(ip)
    arp_mac = tvcontrol.get_mac(ip)            # MAC of the NIC actually in use
    mac = arp_mac or info.get("wifi_mac")
    brand = tvcontrol.detect_brand(ip)
    with CFG_LOCK:
        entry = CFG.setdefault("tvs", {}).get(ip, {})
        # keep a user-chosen custom name; only auto-fill when none set yet
        if entry.get("custom_name"):
            name = entry["name"]
        else:
            name = data.get("name") or info.get("name") or (
                "Hisense TV" if brand == "hisense" else "Samsung TV")
        entry.update({
            "name": name, "mac": mac,
            "wifi_mac": info.get("wifi_mac"),
            "model": info.get("model") or entry.get("model"),
            "brand": brand if brand != "unknown" else entry.get("brand", "samsung"),
        })
        CFG["tvs"][ip] = entry                 # trusted/auto_open preserved
        save_config(CFG)
    return jsonify(ok=True, tv=CFG["tvs"][ip], online=info.get("online", False),
                   brand=brand)


@app.route("/api/admin/tv/forget", methods=["POST"])
@admin_required
def tv_forget():
    data = request.get_json(silent=True) or {}
    ip = data.get("ip")
    # Deleting a TV wipes its pairing token and assignment, so require the
    # admin password again as a deliberate confirmation step.
    if (data.get("password") or "") != CFG.get("admin_password"):
        return jsonify(error="Wrong admin password — TV not removed"), 403
    with CFG_LOCK:
        CFG.get("tvs", {}).pop(ip, None)
        CFG["assignments"].pop(ip, None)
        save_config(CFG)
    return jsonify(ok=True)


@app.route("/api/admin/tv/power", methods=["POST"])
@admin_required
def tv_power():
    data = request.get_json(silent=True) or {}
    ip, action = data.get("ip"), data.get("action")
    with CFG_LOCK:
        tv = dict(CFG.get("tvs", {}).get(ip) or {})
    try:
        if action == "on":
            macs = {tv.get("mac"), tv.get("wifi_mac"), tvcontrol.get_mac(ip)}
            macs.discard(None)
            if not macs:
                return jsonify(error="MAC unknown — turn the TV on once "
                               "manually, then Scan network again so "
                               "Plant Display can learn its MAC."), 400
            for m in macs:
                tvcontrol.wake(m, ip)
            return jsonify(ok=True, note="Wake-on-LAN sent to "
                           f"{len(macs)} MAC(s). The TV needs 'Power On "
                           "with Mobile / IP Remote' enabled.")
        elif action == "off":
            tvcontrol.drive_power(ip, _brand_of(ip))
            return jsonify(ok=True, note="Power key sent (accept the pairing "
                           "popup on the TV the first time).")
        return jsonify(error="action must be 'on' or 'off'"), 400
    except Exception as exc:
        return jsonify(error=tvcontrol.friendly_ws_error(exc)), 500


def _is_asleep(exc):
    s = str(exc)
    return "10061" in s or "refused" in s.lower() or "timed out" in s.lower()


def _wake_then_open(ip):
    """Background: wake a sleeping TV, wait for boot, then launch the viewer."""
    import time
    with CFG_LOCK:
        tv = dict(CFG.get("tvs", {}).get(ip) or {})
    macs = {tv.get("mac"), tv.get("wifi_mac")}
    macs.discard(None)
    for m in macs:
        try:
            tvcontrol.wake(m, ip)
        except Exception:
            pass
    for _ in range(15):                      # up to ~45 s for boot
        time.sleep(3)
        if tvcontrol.tv_info(ip).get("online"):
            time.sleep(6)                    # let Tizen finish starting
            try:
                _open_viewer_on_tv(ip)
                print(f"[wake-open] viewer launched on {ip}")
            except Exception as exc:
                print(f"[wake-open] {ip}: launch failed: {exc}")
            return
    print(f"[wake-open] {ip} never came online — Wake-on-LAN not working "
          "for this TV (check 'Power On with Mobile' / use Ethernet).")


@app.route("/api/admin/tv/open", methods=["POST"])
@admin_required
def tv_open():
    """Remotely launch the TV browser at the viewer page, pre-authenticated."""
    data = request.get_json(silent=True) or {}
    ip = data.get("ip")
    if not ip:
        return jsonify(error="ip required"), 400
    tok = secrets.token_urlsafe(24)
    VIEWER_TOKENS.add(tok)
    _save_tokens()
    url = f"http://{lan_ip()}:{CFG['port']}/?token={tok}"
    # If a viewer page is already open on this TV, the firmware ignores a new
    # browser-launch — so also bump a reload signal it will pick up on its
    # next poll and reload itself.
    RELOAD_SIGNALS[ip] = RELOAD_SIGNALS.get(ip, 0) + 1
    if not tvcontrol.tv_info(ip).get("online"):
        threading.Thread(target=_wake_then_open, args=(ip,),
                         daemon=True).start()
        return jsonify(ok=True, note="TV looks off — Wake-on-LAN sent; the "
                       "viewer will launch by itself if the TV boots "
                       "(watch it for ~45 s). Progress is printed in the "
                       "Plant Display console.")
    try:
        method = tvcontrol.drive_open(ip, _brand_of(ip), url)
        return jsonify(ok=True, note=f"Viewer launch sent ({method}). If the "
                       "wrong app opens, see the browser home-page tip in "
                       "the TV section.")
    except Exception as exc:
        return jsonify(error=tvcontrol.friendly_ws_error(exc), url=url), 500


@app.route("/api/admin/tv/autoopen", methods=["POST"])
@admin_required
def tv_autoopen():
    """Enable/disable auto-launching the viewer whenever this TV powers on."""
    data = request.get_json(silent=True) or {}
    ip, enabled = data.get("ip"), bool(data.get("enabled"))
    with CFG_LOCK:
        tv = CFG.get("tvs", {}).get(ip)
        if tv is None:
            return jsonify(error="Unknown TV — save it first"), 400
        tv["auto_open"] = enabled
        save_config(CFG)
    return jsonify(ok=True, enabled=enabled)


@app.route("/api/admin/tv/trust", methods=["POST"])
@admin_required
def tv_trust():
    """Mark a TV's IP as trusted: it skips the viewer password entirely."""
    data = request.get_json(silent=True) or {}
    ip, enabled = data.get("ip"), bool(data.get("enabled"))
    with CFG_LOCK:
        tv = CFG.get("tvs", {}).get(ip)
        if tv is None:
            return jsonify(error="Unknown TV — save it first"), 400
        tv["trusted"] = enabled
        save_config(CFG)
    return jsonify(ok=True, enabled=enabled)


def _brand_of(ip):
    with CFG_LOCK:
        return (CFG.get("tvs", {}).get(ip) or {}).get("brand", "samsung")


@app.route("/api/admin/tv/rename", methods=["POST"])
@admin_required
def tv_rename():
    """Set a custom display name for a TV."""
    data = request.get_json(silent=True) or {}
    ip = data.get("ip")
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(error="name required"), 400
    with CFG_LOCK:
        tv = CFG.get("tvs", {}).get(ip)
        if tv is None:
            return jsonify(error="Unknown TV — save it first"), 400
        tv["name"] = name[:60]
        tv["custom_name"] = True
        save_config(CFG)
    return jsonify(ok=True, name=tv["name"])


@app.route("/api/admin/tv/key", methods=["POST"])
@admin_required
def tv_key():
    """Send a remote-control key press (virtual remote)."""
    data = request.get_json(silent=True) or {}
    ip, key = data.get("ip"), data.get("key")
    try:
        tvcontrol.drive_key(ip, _brand_of(ip), key)
        return jsonify(ok=True)
    except Exception as exc:
        return jsonify(error=tvcontrol.friendly_ws_error(exc)), 500


def _open_viewer_on_tv(ip):
    tok = secrets.token_urlsafe(24)
    VIEWER_TOKENS.add(tok)
    _save_tokens()
    tvcontrol.drive_open(ip, _brand_of(ip), f"http://{lan_ip()}:{CFG['port']}/?token={tok}")


VIEWER_SEEN = {}          # viewer ip -> last /api/me timestamp
TV_HEALTH = {}            # tv ip -> {"online":bool,"last_ok":ts,"reconnects":int}


def _tv_watcher():
    """Background health monitor.

    1. Detects an auto-open TV transitioning offline -> online and launches
       the viewer (original behaviour).
    2. Watches every saved TV: if it is reachable on the network but its
       viewer page has stopped polling /api/me (browser crashed, TV dropped
       the page, "no server found"), it re-launches the viewer automatically.
       This is what stops the "restart the browser / restart everything"
       cycle.
    """
    import time
    last_online = {}
    STALE_AFTER = 90          # s without a viewer poll = viewer is gone
    RECHECK = 15              # s between probes
    while True:
        time.sleep(RECHECK)
        now = time.time()
        with CFG_LOCK:
            tvs = dict(CFG.get("tvs", {}))
            assigns = dict(CFG.get("assignments", {}))
        for ip, tv in tvs.items():
            try:
                online = tvcontrol.tv_info(ip).get("online", False)
            except Exception:
                online = False
            h = TV_HEALTH.setdefault(
                ip, {"online": online, "last_ok": 0, "reconnects": 0})
            prev = last_online.get(ip)
            last_online[ip] = online
            h["online"] = online
            if online:
                h["last_ok"] = now

            # (1) just powered on -> open viewer
            if online and prev is False and tv.get("auto_open"):
                time.sleep(8)                   # let the TV finish booting
                try:
                    _open_viewer_on_tv(ip)
                    print(f"[auto-open] viewer launched on {ip}")
                except Exception as exc:
                    print(f"[auto-open] {ip} failed: {exc}")
                continue

            # (2) reachable, has an assigned source, but the viewer page is
            #     no longer polling -> the page died; re-launch it.
            if online and assigns.get(ip):
                seen = VIEWER_SEEN.get(ip, 0)
                if seen and (now - seen) > STALE_AFTER:
                    try:
                        _open_viewer_on_tv(ip)
                        h["reconnects"] += 1
                        VIEWER_SEEN[ip] = now      # give it time to come back
                        print(f"[watchdog] {ip} viewer was stale "
                              f"({int(now-seen)}s) — relaunched "
                              f"(#{h['reconnects']})")
                    except Exception as exc:
                        print(f"[watchdog] {ip} relaunch failed: {exc}")


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    threading.Thread(target=_tv_watcher, daemon=True).start()

    def _restore_urls():             # rebuild per-TV virtual browsers
        if not urlsource.playwright_available():
            return
        for ip, tv in list(CFG.get("tvs", {}).items()):
            if not tv.get("use_url") or not tv.get("url"):
                continue
            slug = _url_slug(ip)
            saved = (CFG.get("urls", {}) or {}).get(slug, {})
            try:
                sess = SESSIONS.create(slug, tv["url"])
                sess.zoom = float(saved.get("zoom", 1.0))
                sess.scroll_y = int(saved.get("scroll_y", 0))
                sess.start(HUB.get(f"url:{slug}", 2))
                print(f"[startup] virtual browser restored for {ip}: {tv['url']}")
            except Exception as exc:
                print(f"[startup] virtual browser {ip} failed: {exc}")
    threading.Thread(target=_restore_urls, daemon=True).start()

    def _reconnect_casts():           # restore saved Chromecasts by IP
        for ip in list(CFG.get("chromecasts", {})):
            try:
                CASTS.add_by_ip(ip)
                print(f"[startup] reconnected Chromecast {ip}")
            except Exception as exc:
                print(f"[startup] Chromecast {ip} offline: {exc}")
    threading.Thread(target=_reconnect_casts, daemon=True).start()
    ip = lan_ip()
    port = CFG["port"]
    print()
    print("  Plant Display is running")
    print(f"  TVs / browsers : http://{ip}:{port}        (viewer password: "
          f"{CFG['viewer_password']})")
    print(f"  Admin console  : http://{ip}:{port}/admin  (admin password in "
          "config.json)")
    try:
        from importlib.metadata import version as _v
        _sv = _v("samsungtvws")
        line = f"  samsungtvws    : {_sv}"
        if int(_sv.split(".")[0]) >= 3:
            line += ('   [!] INCOMPATIBLE — TV control will fail. Fix with:\n'
                     '                   py -3 -m pip install "samsungtvws<3"'
                     ' --force-reinstall   (then restart)')
        print(line)
    except Exception:
        pass
    if not ffmpeg_available():
        print("  [!] ffmpeg not found — Chromecast casting disabled until you "
              "install it (winget install Gyan.FFmpeg)")
    print()
    app.run(host="0.0.0.0", port=port, threaded=True)
