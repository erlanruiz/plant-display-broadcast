"""
Plant Display device discovery.

* Samsung / DLNA TVs  -> SSDP M-SEARCH on the local network
* Chromecasts         -> pychromecast (mDNS)
"""

import re
import socket
import threading

SSDP_ADDR = ("239.255.255.250", 1900)
MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    "ST: ssdp:all\r\n\r\n"
).encode()


def scan_ssdp(timeout=3.0):
    """Return [{ip, name, kind}] of UPnP devices, flagging Samsung TVs."""
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    try:
        sock.sendto(MSEARCH, SSDP_ADDR)
        sock.sendto(MSEARCH, SSDP_ADDR)
        import time
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                break
            ip = addr[0]
            text = data.decode(errors="ignore")
            server = _header(text, "SERVER") or ""
            st = _header(text, "ST") or ""
            usn = _header(text, "USN") or ""
            blob = (server + " " + st + " " + usn).lower()
            is_samsung = "samsung" in blob
            is_tv = is_samsung or "dial" in blob or "mediarenderer" in blob
            if not is_tv:
                continue
            prev = found.get(ip, {})
            found[ip] = {
                "ip": ip,
                "name": prev.get("name") or (server.split("UPnP")[0].strip(" /")
                                             or ("Samsung TV" if is_samsung else "TV/Renderer")),
                "kind": "samsung" if (is_samsung or prev.get("kind") == "samsung") else "tv",
            }
    finally:
        sock.close()
    return sorted(found.values(), key=lambda d: d["ip"])


def _header(text, name):
    m = re.search(rf"^{name}:\s*(.+)$", text, re.I | re.M)
    return m.group(1).strip() if m else None


# --------------------------------------------------------------------------- #
# Chromecast
# --------------------------------------------------------------------------- #
class ChromecastRegistry:
    """Keeps discovered cast devices so they can be started/stopped by UUID."""

    def __init__(self):
        self.lock = threading.Lock()
        self.casts = {}      # uuid str -> pychromecast.Chromecast
        self.browser = None

    def scan(self, timeout=6):
        import pychromecast
        chromecasts, browser = pychromecast.get_chromecasts(timeout=timeout)
        with self.lock:
            if self.browser:
                try:
                    self.browser.stop_discovery()
                except Exception:
                    pass
            self.browser = browser
            for cc in chromecasts:
                self.casts[str(cc.uuid)] = cc
        return [{
            "uuid": str(cc.uuid),
            "name": cc.name,
            "ip": getattr(cc.cast_info, "host", ""),
            "model": getattr(cc.cast_info, "model_name", ""),
        } for cc in chromecasts]

    def add_by_ip(self, ip, port=8009):
        """Connect to a Chromecast directly by IP (no mDNS needed — works in
        VMs where multicast discovery is blocked). Returns its dict."""
        import socket as _s
        # 1) is the cast control port even reachable from here?
        probe = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        probe.settimeout(5)
        try:
            probe.connect((ip, port))
        except Exception as exc:
            raise RuntimeError(
                f"Cannot reach {ip}:{port} (TCP). Ping may work while this "
                f"port is blocked by a VLAN/firewall rule on the network "
                f"({exc}). Test from the VM with: "
                f"Test-NetConnection {ip} -Port 8009") from None
        finally:
            probe.close()
        # 2) fetch device identity over unicast, then connect
        import pychromecast
        from pychromecast.discovery import get_device_info
        try:
            info = get_device_info(ip, timeout=15)
        except Exception as exc:
            raise RuntimeError(f"{ip}:{port} is open but device info lookup "
                               f"failed: {exc}") from None
        if info is None:
            raise RuntimeError(
                f"{ip}:{port} is open but did not identify as a Chromecast. "
                "Is this the right device IP?")
        host = (ip, port, info.uuid, info.model_name, info.friendly_name)
        cc = pychromecast.get_chromecast_from_host(host, timeout=15)
        cc.wait(timeout=15)
        with self.lock:
            self.casts[str(cc.uuid)] = cc
        return {
            "uuid": str(cc.uuid),
            "name": cc.name or info.friendly_name,
            "ip": ip,
            "model": info.model_name or "",
        }

    def play(self, uuid, url, content_type="application/vnd.apple.mpegurl",
             title="Plant Display"):
        with self.lock:
            cc = self.casts.get(uuid)
        if cc is None:
            raise KeyError("Chromecast not found — run a scan first")
        cc.wait(timeout=10)
        cc.media_controller.play_media(url, content_type, title=title,
                                       stream_type="LIVE")
        cc.media_controller.block_until_active(timeout=10)
        return True

    def stop(self, uuid):
        with self.lock:
            cc = self.casts.get(uuid)
        if cc is None:
            return False
        try:
            cc.media_controller.stop()
            cc.quit_app()
        except Exception:
            pass
        return True


def scan_chromecasts_ssdp(timeout=4.0):
    """Find Chromecasts without mDNS: SSDP often still works where mDNS is
    blocked, and Cast devices answer it. Returns [{ip}] candidates."""
    import socket
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: urn:dial-multiscreen-org:service:dial:1\r\n\r\n"
    ).encode()
    ips = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    try:
        sock.sendto(msg, ("239.255.255.250", 1900))
        sock.sendto(msg, ("239.255.255.250", 1900))
        import time
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                break
            blob = data.decode(errors="ignore").lower()
            if "dial" in blob or "chromecast" in blob or "google" in blob:
                ips.add(addr[0])
    finally:
        sock.close()
    return [{"ip": ip} for ip in sorted(ips)]


CASTS = ChromecastRegistry()
