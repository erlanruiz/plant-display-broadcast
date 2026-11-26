"""
Plant Display HLS manager.

Chromecast's default media receiver cannot play MJPEG, so for cast targets we
spawn ffmpeg to transcode into a small rolling HLS playlist, served by Flask
at /hls/<channel>/index.m3u8.

Frames are PIPED into ffmpeg's stdin at a guaranteed steady 15 fps (repeating
the latest captured frame as needed). This decouples ffmpeg's input rate from
how fast a window actually captures — slow live capture (e.g. a 1080p browser
window on a GPU-less VM) no longer starves ffmpeg and triggers the
"received no packets" startup failure.
"""

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from PIL import Image

FFMPEG = shutil.which("ffmpeg")
HLS_ROOT = os.path.join(tempfile.gettempdir(), "plantdisplay_hls")
PIPE_FPS = 15


def ffmpeg_available():
    return FFMPEG is not None


def safe_name(channel_id):
    return channel_id.replace(":", "_")


def _placeholder_jpeg(text="Starting…", size=(1280, 720)):
    import io
    img = Image.new("RGB", size, (16, 20, 27))
    try:
        from PIL import ImageDraw
        ImageDraw.Draw(img).text((48, size[1] // 2), text, fill=(255, 180, 84))
    except Exception:
        pass
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue()


def _normalize(jpeg_bytes, target):
    """Fit a JPEG into a FIXED target size (letterboxed), re-encode. ffmpeg's
    mjpeg decoder fails when frame dimensions change mid-stream, so every
    frame piped to a cast must share one resolution."""
    import io
    try:
        img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    except Exception:
        return None
    if img.size != target:
        canvas = Image.new("RGB", target, (0, 0, 0))
        fitted = img.copy()
        fitted.thumbnail(target, Image.BILINEAR)
        canvas.paste(fitted, ((target[0] - fitted.width) // 2,
                              (target[1] - fitted.height) // 2))
        img = canvas
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=72)
    return buf.getvalue()


class HlsManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.procs = {}    # channel_id -> Popen
        self.pumps = {}    # channel_id -> (thread, stop_event)
        self.logs = {}     # channel_id -> ffmpeg log path

    def ensure(self, channel_id):
        """Start (or reuse) an ffmpeg HLS transcode fed from the channel's
        live frames via a steady stdin pipe. Returns the playlist path."""
        if not FFMPEG:
            raise RuntimeError(
                "ffmpeg was not found on PATH. Install it (winget install "
                "Gyan.FFmpeg) and restart Plant Display to enable Chromecast.")
        rel = f"{safe_name(channel_id)}/index.m3u8"
        with self.lock:
            p = self.procs.get(channel_id)
            if p and p.poll() is None:
                return rel
            outdir = os.path.join(HLS_ROOT, safe_name(channel_id))
            shutil.rmtree(outdir, ignore_errors=True)
            os.makedirs(outdir, exist_ok=True)
            cmd = [
                FFMPEG, "-hide_banner", "-loglevel", "warning",
                "-f", "mjpeg", "-framerate", str(PIPE_FPS), "-i", "pipe:0",
                # silent audio — Chromecast can stall on video-only HLS
                "-f", "lavfi",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-map", "0:v:0", "-map", "1:a:0",
                "-r", str(PIPE_FPS), "-fps_mode", "cfr",
                "-c:v", "libx264", "-preset", "veryfast",
                "-profile:v", "main", "-level", "4.0",
                "-pix_fmt", "yuv420p", "-g", "30", "-keyint_min", "30",
                "-sc_threshold", "0",
                "-b:v", "4M", "-maxrate", "4.5M", "-bufsize", "9M",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "6",
                "-hls_flags",
                "delete_segments+independent_segments+omit_endlist",
                "-hls_segment_type", "mpegts",
                "-hls_segment_filename", os.path.join(outdir, "seg_%05d.ts"),
                os.path.join(outdir, "index.m3u8"),
            ]
            logpath = os.path.join(outdir, "ffmpeg.log")
            self.logs[channel_id] = logpath
            logf = open(logpath, "wb")
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=logf)
            self.procs[channel_id] = proc
            stop = threading.Event()
            t = threading.Thread(target=self._pump,
                                 args=(channel_id, proc, stop), daemon=True)
            self.pumps[channel_id] = (t, stop)
            t.start()

        # wait for the playlist AND >=2 segments before telling the Chromecast
        playlist = os.path.join(HLS_ROOT, rel)
        outdir_abs = os.path.join(HLS_ROOT, safe_name(channel_id))
        for _ in range(200):              # up to ~20 s
            p = self.procs.get(channel_id)
            if p and p.poll() is not None:
                tail = ""
                try:
                    with open(os.path.join(outdir_abs, "ffmpeg.log")) as f:
                        tail = f.read()[-600:]
                except Exception:
                    pass
                print(f"[cast] ffmpeg for {channel_id} exited {p.returncode}:\n{tail}")
                raise RuntimeError(
                    f"ffmpeg exited (code {p.returncode}). {tail.strip()[-220:]}")
            segs = ([f for f in os.listdir(outdir_abs) if f.endswith(".ts")]
                    if os.path.isdir(outdir_abs) else [])
            if os.path.exists(playlist) and len(segs) >= 2:
                break
            time.sleep(0.1)
        return rel

    def _pump(self, channel_id, proc, stop):
        """Write the channel's latest JPEG into ffmpeg stdin at a steady
        PIPE_FPS, normalized to ONE fixed resolution (ffmpeg's mjpeg decoder
        rejects a stream whose frame size changes), repeating frames so
        ffmpeg never starves."""
        from plantdisplay.capture import HUB
        ch = HUB.get(channel_id, PIPE_FPS)
        interval = 1.0 / PIPE_FPS
        target = (1280, 720)
        last = _placeholder_jpeg(size=target)
        last_src = None                 # last raw frame we normalized
        # prime: wait briefly for a first real frame
        for _ in range(PIPE_FPS):
            if ch.frame is not None:
                break
            time.sleep(0.05)
        while not stop.is_set() and proc.poll() is None:
            t0 = time.time()
            src = ch.frame
            if src is not None and src is not last_src:
                norm = _normalize(src, target)
                if norm is not None:
                    last = norm
                    last_src = src
            try:
                proc.stdin.write(last)
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                break
            time.sleep(max(0.0, interval - (time.time() - t0)))
        try:
            proc.stdin.close()
        except Exception:
            pass

    def stop(self, channel_id):
        with self.lock:
            p = self.procs.pop(channel_id, None)
            pump = self.pumps.pop(channel_id, None)
        if pump:
            pump[1].set()
        if p and p.poll() is None:
            try:
                p.send_signal(signal.SIGTERM)
                p.wait(timeout=5)
            except Exception:
                p.kill()

    def stop_all(self):
        for cid in list(self.procs):
            self.stop(cid)


HLS = HlsManager()
