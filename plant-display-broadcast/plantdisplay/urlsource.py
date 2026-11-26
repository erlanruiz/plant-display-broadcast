"""
Plant Display URL sessions — an interactive "virtual browser" per TV.

Each TV can be switched to URL mode. Plant Display then runs a dedicated
headless Chromium for that TV, publishing frames into capture channel
"url:<slug>" so the TV (or a Chromecast) can display it like any other source.

The admin opens /admin/browser?ip=<tv> to interact with that page: scroll,
zoom, click, type, navigate — then presses OK to commit the view and start
streaming. Re-opening resumes exactly where it was left, because the browser
session stays alive.

Cookies/sessions live in a real Chromium profile directory on disk, so a
sign-in performed inside the virtual browser persists across restarts. The
profile is wiped only when the URL changes or URL mode is turned off.

Playwright's sync API is not thread-safe, so the render thread owns the page
and executes commands from a queue.

Requires:
    py -3 -m pip install playwright
    py -3 -m playwright install chromium
"""

import io
import os
import queue
import shutil
import tempfile
import threading
import time

PROFILE_ROOT = os.path.join(tempfile.gettempdir(), "plantdisplay_profiles")


def playwright_available():
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def _err_jpeg(msg, w=1280, h=720):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (16, 20, 27))
    d = ImageDraw.Draw(img)
    d.text((44, h // 2 - 40), "Virtual browser unavailable",
           fill=(255, 107, 107))
    y = h // 2
    for line in _wrap(msg or "unknown error", 78)[:6]:
        d.text((44, y), line, fill=(139, 153, 172))
        y += 22
    b = io.BytesIO()
    img.save(b, "JPEG", quality=75)
    return b.getvalue()


def _wrap(text, n):
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > n:
            lines.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


class UrlSession:
    """A live headless page bound to one capture channel."""

    def __init__(self, slug, url, width=1920, height=1080, fps=2):
        self.slug = slug
        self.url = url
        self.width, self.height = int(width), int(height)
        self.fps = max(1, min(int(fps), 5))
        self.zoom = 1.0
        self.scroll_y = 0
        self.title = ""
        self.engine = None
        self.error = None
        self.ready = threading.Event()
        self.stop_flag = threading.Event()
        self.cmds = queue.Queue()
        self.thread = None
        self.profile_dir = os.path.join(PROFILE_ROOT, slug)

    # ---------------------------------------------------------------- state
    def to_dict(self):
        return {"slug": self.slug, "url": self.url, "zoom": round(self.zoom, 3),
                "scroll_y": self.scroll_y, "title": self.title,
                "engine": self.engine,
                "error": self.error, "alive": bool(
                    self.thread and self.thread.is_alive())}

    # ------------------------------------------------------------- lifecycle
    def start(self, channel):
        if self.thread and self.thread.is_alive():
            return
        self.stop_flag.clear()
        self.ready.clear()
        self.thread = threading.Thread(target=self._loop, args=(channel,),
                                       daemon=True)
        self.thread.start()

    def stop(self, timeout=6):
        self.stop_flag.set()
        if self.thread:
            self.thread.join(timeout=timeout)

    def wipe_profile(self):
        """Delete stored cookies/sessions for this source."""
        shutil.rmtree(self.profile_dir, ignore_errors=True)

    # -------------------------------------------------------------- commands
    def call(self, op, timeout=15, **args):
        """Queue an operation for the render thread and wait for its result."""
        if not (self.thread and self.thread.is_alive()):
            raise RuntimeError(self.error or "virtual browser is not running")
        box, ev = {}, threading.Event()
        self.cmds.put((op, args, box, ev))
        if not ev.wait(timeout):
            raise RuntimeError(f"'{op}' timed out")
        if "e" in box:
            raise RuntimeError(box["e"])
        return box.get("r")

    def _do(self, page, op, a):
        if op == "scroll":
            page.evaluate("dy => window.scrollBy(0, dy)", int(a.get("dy", 0)))
        elif op == "zoom":
            self.zoom = max(0.25, min(float(a.get("zoom", 1.0)), 4.0))
            page.evaluate("z => { document.body.style.zoom = z; }", self.zoom)
        elif op == "click":
            page.mouse.click(float(a["x"]), float(a["y"]))
            page.wait_for_timeout(400)
        elif op == "type":
            page.keyboard.type(str(a.get("text", "")), delay=25)
        elif op == "key":
            page.keyboard.press(str(a.get("key", "Enter")))
            page.wait_for_timeout(400)
        elif op == "goto":
            self.url = str(a["url"])
            page.goto(self.url, wait_until="domcontentloaded", timeout=30000)
        elif op == "back":
            page.go_back(timeout=15000)
        elif op == "reload":
            page.reload(wait_until="domcontentloaded", timeout=30000)
        elif op == "state":
            pass
        else:
            raise ValueError(f"unknown op {op}")
        # refresh cached state after every command
        try:
            self.scroll_y = int(page.evaluate("window.scrollY") or 0)
            self.title = (page.title() or "")[:80]
            self.url = page.url or self.url
        except Exception:
            pass
        return self.to_dict()

    # ------------------------------------------------------------------ loop
    # ------------------------------------------------------- engine selection
    LAUNCH_ATTEMPTS = [
        ({}, "playwright chromium (headless shell)"),
        ({"channel": "chromium"}, "playwright chromium (full build)"),
        ({"channel": "msedge"}, "Microsoft Edge (installed on the host)"),
        ({"channel": "chrome"}, "Google Chrome (installed on the host)"),
    ]

    def _launch(self, p, headless=True, viewport=None):
        """Launch a persistent context, trying each available engine.

        A fresh Playwright pip install has no browser binaries until
        `playwright install` is run, and recent versions need a separate
        headless-shell download. Falling back to the Edge/Chrome already on
        the machine means URL mode works regardless.
        """
        vp = viewport or {"width": self.width, "height": self.height}
        args = ["--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"]
        errors = []
        for kw, label in self.LAUNCH_ATTEMPTS:
            try:
                ctx = p.chromium.launch_persistent_context(
                    self.profile_dir, headless=headless, viewport=vp,
                    args=args, **kw)
                self.engine = label
                print(f"[urlsource] {self.slug}: using {label}")
                return ctx
            except Exception as exc:
                errors.append(f"{label}: {str(exc).splitlines()[0][:90]}")
        raise RuntimeError(
            "No usable browser engine. Install Playwright's browser with:  "
            "py -3 -m playwright install chromium   (or ensure Microsoft "
            "Edge is installed). Tried — " + " | ".join(errors))

    def _loop(self, channel):
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            self.error = ("Playwright is not installed. Run:  py -3 -m pip "
                          "install playwright   then   py -3 -m playwright "
                          "install chromium   and restart Plant Display.")
            channel.publish(_err_jpeg(self.error))
            self.ready.set()
            return
        try:
            os.makedirs(self.profile_dir, exist_ok=True)
            with sync_playwright() as p:
                ctx = self._launch(p, headless=True)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(self.url, wait_until="domcontentloaded",
                          timeout=30000)
                if self.zoom != 1.0:
                    page.evaluate("z => { document.body.style.zoom = z; }",
                                  self.zoom)
                if self.scroll_y:
                    page.evaluate("y => window.scrollTo(0, y)", self.scroll_y)
                self.error = None
                self.ready.set()
                interval = 1.0 / self.fps
                while not self.stop_flag.is_set():
                    t0 = time.time()
                    while not self.cmds.empty():
                        op, a, box, ev = self.cmds.get()
                        try:
                            box["r"] = self._do(page, op, a)
                        except Exception as exc:
                            box["e"] = str(exc)[:200]
                        ev.set()
                    try:
                        channel.publish(page.screenshot(type="jpeg",
                                                        quality=70))
                    except Exception as exc:
                        self.error = str(exc)[:200]
                    time.sleep(max(0.0, interval - (time.time() - t0)))
                ctx.close()
        except Exception as exc:
            self.error = str(exc)[:300]
            channel.publish(_err_jpeg(self.error))
            self.ready.set()

    # --------------------------------------------------------------- sign-in
    def signin_headed(self, minutes=10):
        """Open this profile in a VISIBLE browser on the host so a human can
        sign in with their own hands. The password never passes through
        Plant Display. Stops the headless render while it runs."""
        from playwright.sync_api import sync_playwright
        self.stop()
        try:
            with sync_playwright() as p:
                ctx = self._launch(p, headless=False,
                                   viewport={"width": 1280, "height": 800})
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(self.url, wait_until="domcontentloaded",
                          timeout=30000)
                deadline = time.time() + minutes * 60
                while time.time() < deadline and ctx.pages:
                    time.sleep(1)
                try:
                    ctx.close()
                except Exception:
                    pass
        finally:
            pass
        return True


class SessionRegistry:
    def __init__(self):
        self.sessions = {}          # slug -> UrlSession
        self.lock = threading.Lock()

    def create(self, slug, url, **kw):
        with self.lock:
            old = self.sessions.pop(slug, None)
        if old:
            old.stop()
            if old.url != url:          # URL changed -> cookies must go
                old.wipe_profile()
        s = UrlSession(slug, url, **kw)
        with self.lock:
            self.sessions[slug] = s
        return s

    def get(self, slug):
        return self.sessions.get(slug)

    def drop(self, slug, wipe=True):
        with self.lock:
            s = self.sessions.pop(slug, None)
        if s:
            s.stop()
            if wipe:
                s.wipe_profile()
        return s is not None

    def list(self):
        return [s.to_dict() for s in self.sessions.values()]


SESSIONS = SessionRegistry()
