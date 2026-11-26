"""
Plant Display capture engine.

Channels identify what is being captured:
    monitor:0      -> the whole virtual desktop (all monitors combined)
    monitor:N      -> physical/virtual monitor N (1-based, as enumerated by mss)
    window:HWND    -> a single top-level window (Windows only)

Capture threads are started on demand (first viewer) and stopped automatically
when nobody has pulled a frame for IDLE_TIMEOUT seconds.
"""

import io
import sys
import time
import threading

from PIL import Image

IS_WINDOWS = sys.platform == "win32"
IDLE_TIMEOUT = 15.0          # seconds with no consumers before a channel stops
DEFAULT_FPS = 15
JPEG_QUALITY = 70
MAX_DIMENSION = 1920         # frames are downscaled to fit this (keeps CPU sane)

if IS_WINDOWS:
    import ctypes
    try:  # make coordinates DPI-correct
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Frame hub
# --------------------------------------------------------------------------- #
class Channel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.cond = threading.Condition()
        self.frame = None            # latest JPEG bytes
        self.seq = 0
        self.last_pull = time.time()
        self.thread = None
        self.stop_flag = threading.Event()
        self.error = None

    def publish(self, jpeg_bytes):
        with self.cond:
            self.frame = jpeg_bytes
            self.seq += 1
            self.cond.notify_all()

    def wait_frame(self, last_seq, timeout=5.0):
        """Block until a frame newer than last_seq exists. Returns (seq, bytes)."""
        self.last_pull = time.time()
        with self.cond:
            if self.seq == last_seq:
                self.cond.wait(timeout)
            return self.seq, self.frame


class CaptureHub:
    def __init__(self):
        self.channels = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._reaper, daemon=True).start()

    def get(self, channel_id, fps=DEFAULT_FPS):
        with self.lock:
            ch = self.channels.get(channel_id)
            if ch is None or (ch.thread is not None and not ch.thread.is_alive()):
                ch = Channel(channel_id)
                ch.thread = threading.Thread(
                    target=_capture_loop, args=(ch, fps), daemon=True)
                ch.thread.start()
                self.channels[channel_id] = ch
            return ch

    def _reaper(self):
        while True:
            time.sleep(5)
            now = time.time()
            with self.lock:
                for cid in list(self.channels):
                    ch = self.channels[cid]
                    if now - ch.last_pull > IDLE_TIMEOUT:
                        ch.stop_flag.set()
                        with ch.cond:
                            ch.cond.notify_all()
                        del self.channels[cid]


HUB = CaptureHub()


# --------------------------------------------------------------------------- #
# Source enumeration
# --------------------------------------------------------------------------- #
def list_monitors():
    """Return [{id, label, width, height}] using a throwaway mss instance."""
    try:
        import mss
    except Exception:
        return []
    out = []
    try:
        ctx = mss.mss()
    except Exception:
        return []
    with ctx as sct:
        for i, mon in enumerate(sct.monitors):
            label = ("All displays combined" if i == 0
                     else f"Display {i} ({mon['width']}x{mon['height']})")
            out.append({
                "id": f"monitor:{i}",
                "label": label,
                "width": mon["width"],
                "height": mon["height"],
            })
    return out


def _dedupe_labels(items):
    """Append a short suffix to windows that share a title, so multiple
    windows of the same app (two Excel books, two Edge windows) are
    distinguishable in the picker."""
    from collections import Counter
    counts = Counter(it["label"] for it in items)
    seen = {}
    out = []
    for it in items:
        lab = it["label"]
        if counts[lab] > 1:
            seen[lab] = seen.get(lab, 0) + 1
            short = it["id"].split(":")[-1][-4:]
            lab = f"{lab} #{seen[lab]} ({short})"
        out.append({**it, "label": lab})
    return out


def list_windows():
    """Return [{id, label}] of visible, titled top-level windows (Windows only)."""
    if not IS_WINDOWS:
        return []
    import win32gui

    wins = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title or title in ("Program Manager", "Windows Input Experience"):
            return
        iconic = win32gui.IsIconic(hwnd)
        if iconic:  # judge size by the restored bounds, not the iconic rect
            l, t, r, b = win32gui.GetWindowPlacement(hwnd)[4]
        else:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
        if r - l < 80 or b - t < 60:
            return
        label = title[:90] + (" (minimized)" if iconic else "")
        wins.append({"id": f"window:{hwnd}", "label": label})

    win32gui.EnumWindows(cb, None)
    return _dedupe_labels(wins)


# --------------------------------------------------------------------------- #
# Capture loops
# --------------------------------------------------------------------------- #
def _encode(img):
    if max(img.size) > MAX_DIMENSION:
        ratio = MAX_DIMENSION / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)),
                         Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def _capture_loop(ch, fps):
    kind, _, arg = ch.id.partition(":")
    interval = 1.0 / max(1, fps)
    try:
        if kind == "monitor":
            _monitor_loop(ch, int(arg), interval)
        elif kind == "window":
            _window_loop(ch, int(arg), interval)
        elif kind == "url":
            # Frames are pushed into this channel by plantdisplay.urlsource's
            # headless-browser thread, so this loop only needs to idle while
            # the channel stays alive.
            while not ch.stop_flag.is_set():
                time.sleep(0.5)
        else:
            raise ValueError(f"Unknown channel kind: {kind}")
    except Exception as exc:  # publish a readable error card for viewers
        ch.error = str(exc)
        print(f"[capture] {ch.id} stopped: {exc}")
        try:
            from PIL import ImageDraw
            img = Image.new("RGB", (960, 540), (20, 24, 31))
            d = ImageDraw.Draw(img)
            d.text((48, 230), "Plant Display capture stopped:",
                   fill=(255, 180, 84))
            d.text((48, 270), str(exc)[:120], fill=(220, 224, 230))
            d.text((48, 310), "Re-select the source in the admin console.",
                   fill=(139, 153, 172))
            ch.publish(_encode(img))
        except Exception:
            pass


def _monitor_loop(ch, index, interval):
    import mss
    with mss.mss() as sct:
        if index >= len(sct.monitors):
            raise ValueError(f"Monitor {index} not found")
        mon = sct.monitors[index]
        while not ch.stop_flag.is_set():
            t0 = time.time()
            shot = sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            ch.publish(_encode(img))
            time.sleep(max(0.0, interval - (time.time() - t0)))


def _mostly_black(img):
    """True if the frame is essentially empty (PrintWindow silently failed)."""
    return img.convert("L").resize((32, 18)).getextrema()[1] < 10


def _printwindow_grab(hwnd, w, h):
    """Capture a window via PrintWindow. Returns PIL image or None."""
    import win32gui
    import win32ui
    import ctypes

    PW_RENDERFULLCONTENT = 2
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        ok = ctypes.windll.user32.PrintWindow(
            hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        if not ok:
            return None
        info = bmp.GetInfo()
        data = bmp.GetBitmapBits(True)
        return Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]),
            data, "raw", "BGRX", 0, 1)
    finally:
        import win32gui as _wg
        _wg.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        _wg.ReleaseDC(hwnd, hwnd_dc)


def _pick_stable(hist, now, min_age=0.8):
    """Newest frame that is at least min_age seconds old — i.e. captured
    before the minimize animation started polluting the stream."""
    pick = None
    for ts, jpeg in hist:
        if now - ts >= min_age:
            pick = jpeg
        else:
            break
    if pick is None and hist:
        pick = hist[0][1]
    return pick


def _grab_with_timeout(fn, timeout=0.8):
    """Run a capture call in a daemon thread; return None if it doesn't
    finish in `timeout` seconds. PrintWindow can block indefinitely on some
    GPU-composited windows — this guarantees the publish loop keeps running."""
    box = [None]
    done = threading.Event()

    def run():
        try:
            box[0] = fn()
        except Exception:
            box[0] = None
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    done.wait(timeout)
    return box[0] if done.is_set() else None


def _window_loop(ch, hwnd, interval):
    import win32gui
    import mss
    from collections import deque

    sct = None
    fails = 0
    frozen_beat = 0.0
    freeze_jpeg = None
    hist = deque(maxlen=60)          # (timestamp, jpeg) of recent live frames
    while not ch.stop_flag.is_set():
        t0 = time.time()
        if not win32gui.IsWindow(hwnd):
            raise ValueError("Window was closed")

        # Minimized: Windows stops rendering the app, so freeze the stream
        # on a clean frame captured BEFORE the minimize animation. Republish
        # it at the normal frame rate (NOT every 2 s) — a sparse stream makes
        # ffmpeg's startup probe time out with "no packets" when casting.
        if win32gui.IsIconic(hwnd):
            if freeze_jpeg is None:
                freeze_jpeg = _pick_stable(hist, time.time()) or ch.frame
            if freeze_jpeg is None:          # minimized before any live frame
                ph = Image.new("RGB", (960, 540), (20, 24, 31))
                try:
                    from PIL import ImageDraw
                    d = ImageDraw.Draw(ph)
                    d.text((48, 250), "Window is minimized.",
                           fill=(255, 180, 84))
                    d.text((48, 290), "Restore it once to capture its image; "
                           "it will then freeze (not drop) when minimized.",
                           fill=(139, 153, 172))
                except Exception:
                    pass
                freeze_jpeg = _encode(ph)
            ch.publish(freeze_jpeg)
            time.sleep(interval)
            continue
        freeze_jpeg = None               # window is back: resume live

        published = False
        try:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            w, h = max(1, r - l), max(1, b - t)

            img = _printwindow_grab(hwnd, w, h)

            # Hardware-accelerated apps (Excel, Chrome, ...) can hand
            # PrintWindow a black frame. If the window is actually on
            # screen, crop it from a live screen grab instead.
            if img is None or _mostly_black(img):
                try:
                    if sct is None:
                        sct = mss.mss()
                    shot = sct.grab({"left": l, "top": t,
                                     "width": w, "height": h})
                    alt = Image.frombytes("RGB", shot.size, shot.bgra,
                                          "raw", "BGRX")
                    if img is None or not _mostly_black(alt):
                        img = alt
                except Exception:
                    pass

            if img is not None:
                jpeg = _encode(img)
                ch.publish(jpeg)
                hist.append((time.time(), jpeg))
                published = True
                fails = 0
        except ValueError:
            raise                       # window closed: stop the channel
        except Exception as exc:        # transient (resize, lock, ...) — retry
            fails += 1
            if fails in (1, 30):
                print(f"[capture] {ch.id}: {exc} (retrying)")
            if fails > 300:
                raise

        # CRITICAL: never emit zero frames, or ffmpeg/HLS dies with "no
        # packets". If this tick produced nothing usable, republish the last
        # good frame, or a placeholder, so the stream always has packets.
        if not published:
            fallback = (hist[-1][1] if hist else ch.frame)
            if fallback is None:
                ph = Image.new("RGB", (960, 540), (16, 20, 27))
                try:
                    from PIL import ImageDraw
                    d = ImageDraw.Draw(ph)
                    d.text((48, 250), "Waiting for window to render…",
                           fill=(255, 180, 84))
                    d.text((48, 290), "Bring it on-screen once if it stays "
                           "blank (hardware-accelerated apps).",
                           fill=(139, 153, 172))
                except Exception:
                    pass
                fallback = _encode(ph)
            ch.publish(fallback)
        time.sleep(max(0.0, interval - (time.time() - t0)))


# --------------------------------------------------------------------------- #
# MJPEG generator used by the Flask routes
# --------------------------------------------------------------------------- #
BOUNDARY = b"--plantdisplayframe"


def mjpeg_stream(channel_id, fps=DEFAULT_FPS):
    ch = HUB.get(channel_id, fps)
    last = 0
    while True:
        seq, frame = ch.wait_frame(last)
        if frame is None:
            time.sleep(0.1)
            continue
        if seq == last:
            continue
        last = seq
        yield (BOUNDARY + b"\r\n"
               b"Content-Type: image/jpeg\r\n"
               b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
               + frame + b"\r\n")
