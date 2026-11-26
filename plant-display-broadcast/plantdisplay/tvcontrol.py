"""
Plant Display Samsung TV control.

* Power ON  -> Wake-on-LAN magic packet (TV must have "Power On with Mobile" /
               IP Remote enabled: Settings > General > Network > Expert).
* Power OFF -> KEY_POWER over Samsung's WebSocket remote API (port 8002).
* Open viewer -> remotely launches the TV's web browser at the Plant Display URL.

The first WebSocket command shows a one-time "Allow Plant Display?" popup on the
TV; once accepted, the pairing token is stored in tv_tokens/ and reused.
"""

import os
import re
import socket
import struct
import subprocess
import sys
import threading

IS_WINDOWS = sys.platform == "win32"
TOKENS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "tv_tokens")
os.makedirs(TOKENS_DIR, exist_ok=True)

MAC_RE = re.compile(r"((?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2})")


# --------------------------------------------------------------------------- #
# MAC discovery (needed for Wake-on-LAN)
# --------------------------------------------------------------------------- #
def get_mac(ip):
    """Best-effort MAC lookup for an IP via the ARP table. Returns str or None."""
    try:  # make sure the IP is in the ARP cache
        ping = ["ping", "-n", "1", "-w", "500", ip] if IS_WINDOWS \
            else ["ping", "-c", "1", "-W", "1", ip]
        subprocess.run(ping, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=4)
    except Exception:
        pass
    try:
        out = subprocess.run(["arp", "-a", ip] if IS_WINDOWS else ["arp", "-n", ip],
                             capture_output=True, text=True, timeout=4).stdout
        for line in out.splitlines():
            if ip in line:
                m = MAC_RE.search(line)
                if m:
                    return m.group(1).replace("-", ":").lower()
        m = MAC_RE.search(out)
        return m.group(1).replace("-", ":").lower() if m else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Wake-on-LAN
# --------------------------------------------------------------------------- #
def wake(mac, ip=None):
    """Send WoL magic packets aggressively: global broadcast, subnet
    broadcast and (if known) straight at the TV's last IP."""
    mac_clean = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if len(mac_clean) != 12:
        raise ValueError(f"Invalid MAC address: {mac!r}")
    payload = b"\xff" * 6 + bytes.fromhex(mac_clean) * 16
    targets = [("255.255.255.255", 9), ("255.255.255.255", 7)]
    if ip:
        parts = ip.split(".")
        if len(parts) == 4:
            bc = ".".join(parts[:3] + ["255"])      # /24 directed broadcast
            targets += [(bc, 9), (bc, 7), (ip, 9), (ip, 7)]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        for _ in range(4):
            for t in targets:
                try:
                    s.sendto(payload, t)
                except OSError:
                    pass
    finally:
        s.close()
    return True


# --------------------------------------------------------------------------- #
# Samsung WebSocket remote API (samsungtvws)
# --------------------------------------------------------------------------- #
# Older Tizen TVs don't support token auth: they must be driven over the
# legacy ws port 8001 (and remember the permission by device, not token).
# Newer TVs use 8002 + token. We probe once per TV and cache the answer.
_PORT_CACHE = {}


def _port_for(ip):
    if ip in _PORT_CACHE:
        return _PORT_CACHE[ip]
    info = tv_info(ip)
    if not info.get("online"):
        return 8002              # unknown — don't cache, re-probe next time
    port = 8002 if info.get("tokenAuth") else 8001
    _PORT_CACHE[ip] = port
    print(f"[tvcontrol] {ip}: token auth "
          f"{'supported - port 8002' if port == 8002 else 'NOT supported - legacy port 8001'}")
    return port


def _tv(ip):
    from samsungtvws import SamsungTVWS
    port = _port_for(ip)
    token_file = (os.path.join(TOKENS_DIR, f"{ip.replace(':', '_')}.token")
                  if port == 8002 else None)
    # 60 s: the first connection holds until the "Allow?" popup on the TV is
    # answered with the physical remote, which takes longer than a few seconds.
    return SamsungTVWS(host=ip, port=port, token_file=token_file,
                       name="Plant Display", timeout=60)


def friendly_ws_error(exc):
    s = str(exc)
    sl = s.lower()
    if any(b in sl for b in BENIGN_EVENTS) or "handshake" in sl:
        return ("The TV interrupted the pairing handshake repeatedly "
                f"({s[:60]}). Press the button once more — pairing usually "
                "completes on the next try. If it keeps happening, update "
                "the TV library on the PC: py -3 -m pip install -U "
                "samsungtvws, then restart Plant Display.")
    if "10061" in s or "refused" in s.lower():
        return ("The TV is off or in deep standby (its network API is not "
                "listening). Wake it first with ⏻ On — and on the TV make "
                "sure Settings > General > Network > Expert Settings > "
                "'Power On with Mobile / IP Remote' is enabled so it keeps "
                "listening in standby.")
    if "ms.channel.timeout" in s.lower().replace(" ", ""):
        return ("The TV did not approve the connection in time. Press the "
                "button again and watch the TV screen: accept the 'Allow "
                "Plant Display?' popup with the TV remote within 60 seconds. "
                "If no popup appears, on the TV go to Settings > General > "
                "External Device Manager > Device Connection Manager: set "
                "Access Notification to 'First Time Only', and in Device List "
                "delete any denied 'Plant Display' entry, then retry.")
    if "unauthorized" in s.lower() or "ms.channel.unauthorized" in s.lower():
        return ("The TV denied Plant Display. On the TV: Settings > General > "
                "External Device Manager > Device Connection Manager > "
                "Device List — delete the Plant Display entry, then retry and "
                "accept the popup.")
    return s


BENIGN_EVENTS = ("touchdisable", "touchenable", "clientconnect",
                 "ready", "voiceapp", "imechannel")


def _retryable(exc):
    s = str(exc).lower()
    return any(b in s for b in BENIGN_EVENTS) or "handshake" in s


def _run(ip, fn, attempts=3):
    """Open a WS session, run one command, give the TV a moment to process
    it, then ALWAYS close. Retries when the TV emits a benign event during
    the handshake (ms.remote.touchDisable etc.), which otherwise aborts
    pairing before the token is saved."""
    import time
    last = None
    for i in range(attempts):
        tv = _tv(ip)
        try:
            result = fn(tv)
            time.sleep(1.0)          # let the TV consume the message
            return result
        except Exception as exc:
            last = exc
            if not _retryable(exc):
                raise
            time.sleep(1.2)          # benign event — reconnect and retry
        finally:
            try:
                tv.close()
            except Exception:
                pass
    raise last


def power_toggle(ip):
    """Sends KEY_POWER. With the TV on, this turns it off (use wake() to turn on)."""
    _drop_session(ip)
    _run(ip, lambda tv: tv.shortcuts().power())
    return True


def open_url(ip, url):
    """Remotely open the TV's web browser at the given URL.

    Tries Samsung's REST application API first (most reliable across
    firmwares), then the WebSocket launch. Returns the method that worked."""
    import requests
    errors = []
    try:  # Strategy 1: REST NATIVE_LAUNCH with the URL as metaTag
        r = requests.post(
            f"http://{ip}:8001/api/v2/applications/org.tizen.browser",
            json={"action_type": "NATIVE_LAUNCH", "metaTag": url},
            timeout=8)
        if r.status_code == 200:
            return "REST launch"
        errors.append(f"REST: HTTP {r.status_code} {r.text[:80]}")
    except Exception as exc:
        errors.append(f"REST: {exc}")
    try:  # Strategy 2: WebSocket app launch
        _run(ip, lambda tv: tv.open_browser(url))
        return "WebSocket launch"
    except Exception as exc:
        errors.append(f"WS: {exc}")
    raise RuntimeError(" | ".join(errors))


ALLOWED_KEYS = {
    "KEY_POWER", "KEY_HOME", "KEY_RETURN", "KEY_EXIT", "KEY_ENTER",
    "KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT",
    "KEY_VOLUP", "KEY_VOLDOWN", "KEY_MUTE",
    "KEY_CHUP", "KEY_CHDOWN", "KEY_SOURCE", "KEY_MENU", "KEY_PLAY",
    "KEY_PAUSE", "KEY_STOP",
}


def _tv_fast(ip):
    """Short-timeout client for the persistent key session (pairing is
    expected to already exist by the time the remote pad is used)."""
    from samsungtvws import SamsungTVWS
    port = _port_for(ip)
    token_file = (os.path.join(TOKENS_DIR, f"{ip.replace(':', '_')}.token")
                  if port == 8002 else None)
    return SamsungTVWS(host=ip, port=port, token_file=token_file,
                       name="Plant Display", timeout=8)


# Persistent remote sessions: one open WebSocket per TV, reused for every
# key press. This turns 2-4 s per press into ~0.1 s.
_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()


def _session(ip):
    with _SESSIONS_LOCK:
        tv = _SESSIONS.get(ip)
        if tv is None:
            tv = _tv_fast(ip)
            _SESSIONS[ip] = tv
        return tv


def _drop_session(ip):
    with _SESSIONS_LOCK:
        tv = _SESSIONS.pop(ip, None)
    if tv is not None:
        try:
            tv.close()
        except Exception:
            pass


def send_key(ip, key):
    """Send a key press on the persistent session; reconnect and retry on
    stale sockets or benign handshake events."""
    import time
    if key not in ALLOWED_KEYS:
        raise ValueError(f"Key not allowed: {key}")
    last = None
    for attempt in range(3):
        tv = _session(ip)
        try:
            tv.send_key(key)
            if key == "KEY_POWER":      # TV will kill the socket anyway
                _drop_session(ip)
            return True
        except Exception as exc:
            last = exc
            _drop_session(ip)           # stale socket or handshake event
            if not _retryable(exc) and attempt > 0:
                raise
            time.sleep(0.8)
    raise last


def tv_info(ip):
    """Quick reachability probe of the TV's REST API (port 8001)."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{ip}:8001/api/v2/", timeout=3) as r:
            data = json.loads(r.read().decode())
        dev = data.get("device", {})
        return {
            "online": True,
            "name": dev.get("name") or data.get("name"),
            "model": dev.get("modelName"),
            "wifi_mac": (dev.get("wifiMac") or "").lower() or None,
            "tokenAuth": dev.get("TokenAuthSupport") == "true",
        }
    except Exception:
        return {"online": False}


# --------------------------------------------------------------------------- #
# Brand detection + driver dispatch
# --------------------------------------------------------------------------- #
def detect_brand(ip):
    """Identify the TV vendor so the right control driver is used.
    Samsung answers its REST API on 8001; Hisense VIDAA listens on MQTT 36669.
    Returns 'samsung', 'hisense', or 'unknown'."""
    info = tv_info(ip)              # Samsung REST probe
    if info.get("online"):
        return "samsung"
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect((ip, 36669))     # VIDAA MQTT
        return "hisense"
    except Exception:
        return "unknown"
    finally:
        s.close()


def _driver(brand):
    """Return the control module for a brand. Samsung uses this module's
    functions; Hisense uses the hisense module."""
    if brand == "hisense":
        from plantdisplay import hisense
        return hisense
    return _SAMSUNG_DRIVER       # this module's own functions


# A small shim exposing this module's Samsung functions as a driver object.
class _SamsungDriver:
    power_toggle = staticmethod(lambda ip: power_toggle(ip))
    send_key = staticmethod(lambda ip, k: send_key(ip, k))
    open_url = staticmethod(lambda ip, url: open_url(ip, url))


_SAMSUNG_DRIVER = _SamsungDriver()


def drive_power(ip, brand):
    return _driver(brand).power_toggle(ip)


def drive_key(ip, brand, key):
    return _driver(brand).send_key(ip, key)


def drive_open(ip, brand, url):
    return _driver(brand).open_url(ip, url)
