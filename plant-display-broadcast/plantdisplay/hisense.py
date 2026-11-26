"""
Plant Display Hisense (VIDAA) TV control — EXPERIMENTAL.

VIDAA TVs expose an MQTT-based remote API on port 36669 with TLS and a
per-TV auth handshake. This module mirrors the Samsung driver's interface
(power_toggle, open_url, send_key) so app.py can treat brands uniformly.

Reality notes:
- The API/port/auth varies across VIDAA model years; this targets the common
  36669 + paho-mqtt scheme used by community VIDAA integrations.
- "open browser at URL" is not reliably exposed on all VIDAA firmwares, so the
  home-page trick (set the viewer URL as the browser home page once) is the
  robust path; open_url here best-effort launches the browser app.
- Requires `paho-mqtt`. If it's missing, calls raise a clear error.
"""

import json
import os
import ssl
import time

PORT = 36669

# VIDAA remote key codes (subset, mapped from Plant Display's ALLOWED_KEYS names)
KEY_MAP = {
    "KEY_POWER": "KEY_POWER",
    "KEY_HOME": "KEY_HOME",
    "KEY_RETURN": "KEY_RETURNS",
    "KEY_EXIT": "KEY_EXIT",
    "KEY_ENTER": "KEY_OK",
    "KEY_UP": "KEY_UP", "KEY_DOWN": "KEY_DOWN",
    "KEY_LEFT": "KEY_LEFT", "KEY_RIGHT": "KEY_RIGHT",
    "KEY_VOLUP": "KEY_VOLUMEUP", "KEY_VOLDOWN": "KEY_VOLUMEDOWN",
    "KEY_MUTE": "KEY_MUTE",
    "KEY_CHUP": "KEY_CHANNELUP", "KEY_CHDOWN": "KEY_CHANNELDOWN",
    "KEY_SOURCE": "KEY_SOURCE", "KEY_MENU": "KEY_MENU",
    "KEY_PLAY": "KEY_PLAY", "KEY_PAUSE": "KEY_PAUSE", "KEY_STOP": "KEY_STOP",
}


def _client(ip):
    try:
        import paho.mqtt.client as mqtt
    except Exception:
        raise RuntimeError(
            "Hisense control needs paho-mqtt. Install it: "
            "py -3 -m pip install paho-mqtt, then restart Plant Display.")
    c = mqtt.Client()
    # VIDAA TVs run a local MQTT broker. Its login is not stored in this code:
    # set HISENSE_MQTT_USER and HISENSE_MQTT_PASSWORD before starting the app.
    user = os.environ.get("HISENSE_MQTT_USER", "")
    pw = os.environ.get("HISENSE_MQTT_PASSWORD", "")
    if not user:
        raise RuntimeError("Hisense control needs HISENSE_MQTT_USER and "
                           "HISENSE_MQTT_PASSWORD set before starting Plant Display.")
    c.username_pw_set(user, pw)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    c.tls_set_context(ctx)
    c.connect(ip, PORT, keepalive=10)
    c.loop_start()
    return c


def _publish(ip, topic, payload=""):
    c = _client(ip)
    try:
        info = c.publish(topic, payload if isinstance(payload, str)
                         else json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=5)
        time.sleep(0.5)
    finally:
        c.loop_stop()
        try:
            c.disconnect()
        except Exception:
            pass
    return True


def power_toggle(ip):
    return _publish(ip, "/remoteapp/tv/remote_service/%s$normal/actions/sendkey"
                    % ip, "KEY_POWER")


def send_key(ip, key):
    code = KEY_MAP.get(key)
    if code is None:
        raise ValueError(f"Key not supported on Hisense: {key}")
    return _publish(ip, "/remoteapp/tv/remote_service/%s$normal/actions/sendkey"
                    % ip, code)


def open_url(ip, url):
    """Best-effort: launch the VIDAA browser. URL targeting is firmware
    dependent — set the viewer URL as the browser home page for reliability."""
    payload = {"name": "Browser", "url": url}
    try:
        _publish(ip,
                 "/remoteapp/tv/ui_service/%s$normal/actions/launchapp" % ip,
                 payload)
        return "VIDAA launchapp"
    except Exception:
        # fall back to a HOME keypress so the user can pick the browser
        _publish(ip, "/remoteapp/tv/remote_service/%s$normal/actions/sendkey"
                 % ip, "KEY_HOME")
        return "VIDAA home (manual browser)"


def tv_info(ip):
    """Lightweight reachability probe for a VIDAA TV (MQTT port open)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect((ip, PORT))
        return {"online": True, "brand": "hisense"}
    except Exception:
        return {"online": False, "brand": "hisense"}
    finally:
        s.close()
