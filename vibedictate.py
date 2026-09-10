#!/usr/bin/env python3
"""
vibedictate — fully offline push-to-talk dictation for Wayland / Linux.

Flow:  hold hotkey -> record -> release -> whisper.cpp -> (optional) local LLM
       cleanup -> paste at the cursor.

No network calls. Even the LLM step talks to Ollama on localhost.
"""

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

try:
    import evdev
    from evdev import ecodes
except ImportError:
    sys.exit("python-evdev is not installed:  sudo pacman -S python-evdev")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "vibedictate"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "vibedictate"
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH = DATA_DIR / "vibedictate.log"

SAMPLE_RATE = 16000
BYTES_PER_FRAME = 2  # s16le mono

DEFAULTS = {
    "hotkey": "KEY_RIGHTCTRL",
    "cancel_key": "KEY_ESC",
    "tap_threshold_ms": 250,
    "double_tap_window_ms": 350,
    "max_handsfree_seconds": 300,
    "min_recording_ms": 350,
    "stop_tail_ms": 300,
    "language": "auto",
    "whisper": {
        "bin": "",
        "model": "~/.local/share/vibedictate/models/ggml-small.bin",
        "threads": 0,
        "extra_args": [],
    },
    "llm": {
        "enabled": False,
        "url": "http://127.0.0.1:11434/api/generate",
        "model": "qwen2.5:3b",
        "timeout_s": 20,
        # Deliberately language-neutral: no filler words are listed, and the
        # model is told to answer in the language it was given. Hardcoding a
        # word list would break every language except the one it was written
        # for.
        "prompt": (
            "The text below came from speech dictation. Remove only filler "
            "words and hesitation sounds, and fix punctuation and "
            "capitalization. Keep the original language of the text. "
            "Do NOT change the meaning, summarize, rewrite, or add comments. "
            "Return only the corrected text, nothing else.\n\n"
        ),
    },
    "output": {
        "method": "paste",           # "paste" | "type"
        "paste_keycodes": [29, 47],  # LEFTCTRL + V
        "restore_clipboard": False,
        "type_delay_ms": 8,
        "paste_delay_ms": 120,
    },
    "notify": True,
    # Intermediate status notifications ("Recording", "Transcribing"). The
    # tray icon already shows the state, so you can set this to false and
    # keep only the result notification.
    "notify_progress": True,
    "sound": False,
    "keep_wav": False,
    "tray": {"enabled": True},
}


class ConfigError(Exception):
    """Missing setting or install step. Fatal at startup, but not on a live
    reload: there we keep running with the previous settings."""


def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2, ensure_ascii=False))
        log(f"wrote default config: {CONFIG_PATH}")
        return dict(DEFAULTS)
    try:
        user = json.loads(CONFIG_PATH.read_text())
    except Exception as e:
        log(f"could not read config ({e}), using defaults")
        return dict(DEFAULTS)
    return deep_merge(DEFAULTS, user)


def log(msg):
    line = f"{time.strftime('%H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------
# Notifications / sound
# --------------------------------------------------------------------------

class Feedback:
    SYNC = "string:x-canonical-private-synchronous:vibedictate"

    def __init__(self, cfg):
        self.enabled = cfg["notify"] and shutil.which("notify-send")
        self.progress = cfg["notify_progress"]
        self.sound = cfg["sound"] and shutil.which("paplay")
        self.gdbus = shutil.which("gdbus")

    def notify(self, body, icon="audio-input-microphone", timeout=1500,
               replace_id=None, want_id=False, progress=False):
        """Sends a notification. With want_id=True it uses notify-send -p and
        returns the notification id, which can later be passed to replace_id
        or close.

        progress=True marks intermediate status notifications; they are
        skipped when notify_progress is off, leaving only the results."""
        if not self.enabled or (progress and not self.progress):
            return None
        cmd = ["notify-send", "-t", str(timeout), "-i", icon, "-h", self.SYNC]
        if replace_id:
            cmd += ["-r", str(replace_id)]
        if want_id:
            cmd.append("-p")
        cmd += ["vibedictate", body]

        if not want_id:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return replace_id
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=2)
            return int(out.stdout.strip())
        except Exception:
            # If -p is unsupported the notification still went out, we just
            # do not get an id back.
            return None

    def close(self, notif_id):
        """Dismisses a notification that is still on screen."""
        if not (self.enabled and notif_id):
            return
        if self.gdbus:
            subprocess.Popen(
                [self.gdbus, "call", "--session",
                 "--dest", "org.freedesktop.Notifications",
                 "--object-path", "/org/freedesktop/Notifications",
                 "--method", "org.freedesktop.Notifications.CloseNotification",
                 str(notif_id)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            # Without gdbus: overwrite the same id with a very short timeout.
            self.notify(" ", "system-run", 1, replace_id=notif_id)

    def beep(self, name="message"):
        if not self.sound:
            return
        path = f"/usr/share/sounds/freedesktop/stereo/{name}.oga"
        if os.path.exists(path):
            subprocess.Popen(["paplay", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------

class Recorder:
    """Reads the microphone as raw s16le from stdout and writes the WAV
    ourselves, so killing the process cannot leave a broken header."""

    def __init__(self):
        self.cmd = self._pick_cmd()
        self.proc = None
        self.buf = bytearray()
        self.thread = None
        self.started_at = 0.0

    @staticmethod
    def _pick_cmd():
        if shutil.which("parecord"):
            # --latency-msec is required: by default parecord writes nothing
            # to stdout for the first ~2 seconds and then flushes everything
            # at once. Without it, dictations shorter than 2 seconds come out
            # completely empty and longer ones lose their tail. At 50ms the
            # first data arrives after ~160ms and latency drops to ~100ms.
            # pw-record and arecord do not have this problem, so we leave
            # them alone.
            return ["parecord", "--rate=16000", "--channels=1",
                    "--format=s16le", "--raw", "--latency-msec=50"]
        if shutil.which("pw-record"):
            return ["pw-record", "--rate=16000", "--channels=1",
                    "--format=s16", "--raw", "-"]
        if shutil.which("arecord"):
            return ["arecord", "-q", "-f", "S16_LE", "-r", "16000",
                    "-c", "1", "-t", "raw"]
        sys.exit("No recording tool found. Install:  sudo pacman -S libpulse")

    def start(self):
        self.buf = bytearray()
        self.started_at = time.monotonic()
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self):
        try:
            while True:
                chunk = self.proc.stdout.read(4096)
                if not chunk:
                    break
                self.buf.extend(chunk)
        except Exception:
            pass

    def stop(self, tail_ms=0):
        """With tail_ms > 0, keeps reading that much longer before killing
        the process.

        Whatever the recording tool, its stdout runs ~100-175ms behind real
        time; terminating the moment the hotkey is released eats the last
        syllable of the sentence. Draining after terminate does not help --
        whatever is in the pipe is already read by _pump, and the missing
        part never reached the process side at all. Delaying the shutdown is
        the only fix.

        Blocks the calling thread; call it with tail_ms=0 on cancel paths.
        """
        if tail_ms > 0 and self.proc and self.proc.poll() is None:
            time.sleep(tail_ms / 1000.0)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        # After terminate, _pump reads whatever is left in the pipe until EOF.
        if self.thread:
            self.thread.join(timeout=1)
        self.proc = None
        return bytes(self.buf)

    @property
    def elapsed_ms(self):
        return int((time.monotonic() - self.started_at) * 1000)


def write_wav(pcm: bytes, path: Path):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


# --------------------------------------------------------------------------
# Transcription (whisper.cpp)
# --------------------------------------------------------------------------

WHISPER_CANDIDATES = ["whisper-cli", "whisper.cpp", "whisper", "main"]
# Placeholders whisper emits for non-speech audio. The localized ones are
# kept on purpose: which of these shows up depends on the spoken language,
# not on the interface language.
NOISE_MARKERS = ("[BLANK_AUDIO]", "(sessizlik)", "[SILENCE]", "[MUSIC]",
                 "[Music]", "(müzik)", "*", "[ Sessizlik ]")


class Transcriber:
    def __init__(self, cfg):
        w = cfg["whisper"]
        self.bin = w["bin"] or self._autodetect()
        self.model = str(Path(os.path.expanduser(w["model"])))
        self.threads = w["threads"] or max(1, (os.cpu_count() or 4) - 1)
        self.extra = list(w["extra_args"])
        self.language = cfg["language"]
        self.proc = None

        # Not sys.exit: this is also constructed on a live config reload, and
        # a bad setting there must not kill the running program.
        if not self.bin:
            raise ConfigError("whisper-cli not found. Set config.json > whisper.bin.")
        if not os.path.exists(self.model):
            raise ConfigError(f"Model missing: {self.model}\nYou can download it with setup.sh.")

    @staticmethod
    def _autodetect():
        for name in WHISPER_CANDIDATES:
            p = shutil.which(name)
            if p:
                return p
        return ""

    def run(self, wav_path: Path) -> str:
        cmd = [self.bin, "-m", self.model, "-f", str(wav_path),
               "-t", str(self.threads), "-np", "-nt"]
        if self.language and self.language != "auto":
            cmd += ["-l", self.language]
        cmd += self.extra

        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        out, err = self.proc.communicate()
        rc = self.proc.returncode
        self.proc = None

        if rc != 0:
            log(f"whisper failed (rc={rc}): {err.strip()[:300]}")
            return ""
        return self._clean(out)

    @staticmethod
    def _clean(raw: str) -> str:
        lines = []
        for line in raw.splitlines():
            s = line.strip()
            if not s or s in NOISE_MARKERS:
                continue
            if s.startswith("[") and s.endswith("]"):
                continue
            lines.append(s)
        return " ".join(lines).strip()

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


# --------------------------------------------------------------------------
# Local LLM cleanup (Ollama)
# --------------------------------------------------------------------------

class LocalCleaner:
    def __init__(self, cfg):
        self.cfg = cfg["llm"]
        self.enabled = self.cfg["enabled"]

    def clean(self, text: str) -> str:
        if not self.enabled or not text:
            return text
        import urllib.request
        payload = json.dumps({
            "model": self.cfg["model"],
            "prompt": self.cfg["prompt"] + text,
            "stream": False,
            "options": {"temperature": 0},
        }).encode()
        req = urllib.request.Request(
            self.cfg["url"], data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg["timeout_s"]) as r:
                data = json.loads(r.read().decode())
            out = (data.get("response") or "").strip()
            # If the model went off the rails, fall back to the raw text --
            # this tool never swallows your words.
            if not out or len(out) > len(text) * 2.5:
                return text
            return out
        except Exception as e:
            log(f"LLM cleanup skipped ({e})")
            return text


# --------------------------------------------------------------------------
# Inserting text at the cursor (ydotool + wl-clipboard)
# --------------------------------------------------------------------------

class TextInserter:
    def __init__(self, cfg):
        self.cfg = cfg["output"]
        self.has_ydotool = bool(shutil.which("ydotool"))
        self.has_wlcopy = bool(shutil.which("wl-copy"))
        if not self.has_ydotool:
            log("WARNING: ydotool missing - text will only be copied to the clipboard.")

    def insert(self, text: str):
        if self.cfg["method"] == "type" and self.has_ydotool:
            self._type(text)
        else:
            self._paste(text)

    def _paste(self, text: str):
        if not self.has_wlcopy:
            log("WARNING: wl-clipboard missing, could not insert the text.")
            return

        previous = None
        if self.cfg["restore_clipboard"]:
            try:
                previous = subprocess.run(
                    ["wl-paste", "--no-newline"], capture_output=True, timeout=1
                ).stdout
            except Exception:
                previous = None

        subprocess.run(["wl-copy"], input=text.encode(), timeout=3)

        if self.has_ydotool:
            time.sleep(self.cfg["paste_delay_ms"] / 1000)
            keys = self.cfg["paste_keycodes"]
            seq = [f"{k}:1" for k in keys] + [f"{k}:0" for k in reversed(keys)]
            subprocess.run(["ydotool", "key"] + seq,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if previous is not None:
            time.sleep(0.35)
            subprocess.run(["wl-copy"], input=previous, timeout=3)

    def _type(self, text: str):
        subprocess.run(
            ["ydotool", "type", "--key-delay", str(self.cfg["type_delay_ms"]), text],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


# --------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------

IDLE, RECORDING, HANDSFREE, PROCESSING = "idle", "recording", "handsfree", "processing"


class DictationController:
    def __init__(self, cfg, loop):
        self.cfg = cfg
        self.loop = loop
        self.state_listener = None   # the tray icon hooks in here (optional)
        self._state = IDLE
        self.recorder = Recorder()
        self.transcriber = Transcriber(cfg)
        self.cleaner = LocalCleaner(cfg)
        self.inserter = TextInserter(cfg)
        self.fb = Feedback(cfg)
        self.last_tap = 0.0
        self.press_at = 0.0
        self.handsfree_task = None
        self.cancelled = False
        self.progress_id = None

    # ---- state -----------------------------------------------------------

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        if value == self._state:
            return
        self._state = value
        if self.state_listener is None:
            return
        # The state sometimes changes from an executor thread
        # (_finish_and_pipeline); the listener touches Qt, so we have to hop
        # back to the main thread.
        try:
            self.loop.call_soon_threadsafe(self.state_listener, value)
        except RuntimeError:
            pass   # the loop is shutting down

    # ---- hotkey events ---------------------------------------------------

    def on_hotkey_down(self):
        if self.state == PROCESSING:
            return
        if self.state == HANDSFREE:
            return  # we will handle it as a single tap on release
        if self.state == IDLE:
            self.press_at = time.monotonic()
            self._start_recording(HANDSFREE if False else RECORDING)

    def on_hotkey_up(self):
        now = time.monotonic()
        held_ms = int((now - self.press_at) * 1000)

        if self.state == HANDSFREE:
            self._stop_and_process()
            return

        if self.state != RECORDING:
            return

        # A short press is a tap. Two taps in a row switch to hands-free.
        if held_ms < self.cfg["tap_threshold_ms"]:
            self.recorder.stop()
            if (now - self.last_tap) * 1000 < self.cfg["double_tap_window_ms"]:
                self.last_tap = 0.0
                self._start_recording(HANDSFREE)
            else:
                self.last_tap = now
                self.state = IDLE
                self.fb.notify("Cancelled (too short)", "process-stop", 800)
            return

        self._stop_and_process()

    def on_cancel_key(self):
        if self.state in (RECORDING, HANDSFREE):
            self.recorder.stop()
            self._cancel_handsfree_timer()
            self.state = IDLE
            self.fb.notify("Recording cancelled", "process-stop", 900)
            log("cancelled: recording discarded")
        elif self.state == PROCESSING:
            self.cancelled = True
            self.transcriber.cancel()
            self.fb.notify("Processing cancelled", "process-stop", 900)
            log("cancelled: transcription killed")

    # ---- reloading settings ----------------------------------------------

    def reload_config(self):
        """Re-reads config.json and applies it to the running components.

        Returns the names of hotkey/cancel_key if they changed: those two are
        bound to the evdev listener at startup, so they only take effect
        after a restart. If the new settings are broken it raises ConfigError
        and nothing changes -- a working setup must not be torn down by a
        half-finished edit.
        """
        if self.state != IDLE:
            raise ConfigError("cannot reload settings while recording or processing")
        new = load_config()
        transcriber = Transcriber(new)   # first: broken settings blow up here
        self.transcriber = transcriber
        self.cleaner = LocalCleaner(new)
        self.inserter = TextInserter(new)
        self.fb = Feedback(new)
        changed = [k for k in ("hotkey", "cancel_key") if new[k] != self.cfg[k]]
        self.cfg = new
        log("settings reloaded")
        return changed

    # ---- internals -------------------------------------------------------

    def _start_recording(self, mode):
        self.state = mode
        self.cancelled = False
        self.recorder.start()
        if mode == HANDSFREE:
            self.fb.notify("Hands-free recording (single tap to stop)",
                           progress=True)
            self._start_handsfree_timer()
        else:
            self.fb.notify("Recording...", progress=True)
        self.fb.beep("message")
        log(f"recording started ({mode})")

    def _start_handsfree_timer(self):
        self._cancel_handsfree_timer()

        async def guard():
            await asyncio.sleep(self.cfg["max_handsfree_seconds"])
            if self.state == HANDSFREE:
                log("hands-free session timed out")
                self._stop_and_process()

        self.handsfree_task = self.loop.create_task(guard())

    def _cancel_handsfree_timer(self):
        if self.handsfree_task and not self.handsfree_task.done():
            self.handsfree_task.cancel()
        self.handsfree_task = None

    def _stop_and_process(self):
        self._cancel_handsfree_timer()
        # Move to PROCESSING right away so no new recording starts during the
        # tail.
        self.state = PROCESSING
        self.loop.run_in_executor(None, self._finish_and_pipeline)

    def _finish_and_pipeline(self):
        # We close the recording in the executor so that the stop_tail_ms
        # wait does not block the event loop -- hence here, not inside
        # on_key_release.
        pcm = self.recorder.stop(self.cfg["stop_tail_ms"])
        duration_ms = len(pcm) // (SAMPLE_RATE * BYTES_PER_FRAME // 1000)

        if duration_ms < self.cfg["min_recording_ms"]:
            self.state = IDLE
            self.fb.notify("Too short, skipped", "process-stop", 800)
            return

        self.progress_id = self.fb.notify("Transcribing...", "system-run",
                                          5000, want_id=True, progress=True)
        self._pipeline(pcm, duration_ms)

    def _pipeline(self, pcm: bytes, duration_ms: int):
        t0 = time.monotonic()
        tmp = Path(tempfile.gettempdir()) / f"vibedictate-{os.getpid()}.wav"
        try:
            write_wav(pcm, tmp)
            text = self.transcriber.run(tmp)

            if self.cancelled:
                log("processing cancelled, output discarded")
                return
            if not text:
                self.fb.notify("Empty transcript", "dialog-warning", 1200,
                               replace_id=self.progress_id)
                self.progress_id = None
                log("empty transcript")
                return

            raw = text
            text = self.cleaner.clean(text)
            if self.cancelled:
                return

            self.inserter.insert(text)
            took = time.monotonic() - t0
            self.fb.notify(text[:80] + ("..." if len(text) > 80 else ""),
                           "edit-paste", 1800, replace_id=self.progress_id)
            self.progress_id = None
            self.fb.beep("complete")
            log(f"ok ({duration_ms}ms audio, {took:.1f}s processing): {raw[:120]}")
        except Exception as e:
            log(f"pipeline error: {e}")
            self.fb.notify(f"Error: {e}", "dialog-error", 2500,
                           replace_id=self.progress_id)
            self.progress_id = None
        finally:
            # Do not leave the progress notification on screen on cancelled
            # or early-return paths.
            self.fb.close(self.progress_id)
            self.progress_id = None
            if not self.cfg["keep_wav"]:
                tmp.unlink(missing_ok=True)
            self.state = IDLE
            self.cancelled = False


# --------------------------------------------------------------------------
# System tray (optional: PyQt6 + qasync)
# --------------------------------------------------------------------------

# State -> (color, tooltip). No blinking or animation: some tray
# implementations buffer icon updates and end up flickering, while a solid
# color looks the same everywhere.
TRAY_STATES = {
    IDLE:       ("#9aa0a6", "idle"),
    RECORDING:  ("#e53935", "recording (hotkey held)"),
    HANDSFREE:  ("#2e9e4f", "HANDS-FREE - microphone is open"),
    PROCESSING: ("#f57c00", "transcribing..."),
}


def tray_backend():
    """Returns the modules if PyQt6 + qasync are available, otherwise None.

    If either is missing, or tray.enabled is false, the program runs without
    a tray icon just like before; that is not an error.
    """
    try:
        from PyQt6 import QtWidgets
        import qasync
    except ImportError as e:
        log(f"tray icon skipped ({e}); running without a tray")
        return None
    return QtWidgets, qasync


class TrayIcon:
    """Wrapper around QSystemTrayIcon.

    Qt symbols are imported inside this class only, so on a system without
    PyQt6 the rest of the module is completely unaffected.
    """

    def __init__(self, controller, app, on_quit, on_restart):
        from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

        self.ctrl = controller
        self.on_restart = on_restart
        self.icons = {state: self._icon(color, state != IDLE, state == HANDSFREE)
                      for state, (color, _) in TRAY_STATES.items()}

        # Keep the menu in an attribute: as a local variable Python collects
        # it and Qt shows an empty menu on right click.
        self.menu = QMenu()
        self.menu.addAction("Open settings", self._open_settings)
        self.menu.addAction("Reload settings", self._reload_settings)
        self.menu.addSeparator()
        self.menu.addAction("Show logs", self._show_logs)
        self.menu.addAction("Restart", self._restart)
        self.menu.addSeparator()
        self.menu.addAction("Quit", on_quit)

        self.tray = QSystemTrayIcon(app)
        self.tray.setContextMenu(self.menu)
        self.apply_state(controller.state)
        self.tray.show()
        controller.state_listener = self.apply_state

    # ---- showing the state -----------------------------------------------

    def apply_state(self, state):
        color, hint = TRAY_STATES.get(state, TRAY_STATES[IDLE])
        self.tray.setIcon(self.icons[state])
        self.tray.setToolTip(f"vibedictate - {hint}")

    @staticmethod
    def _icon(color_hex, filled, badge):
        """We draw the microphone ourselves instead of using a theme icon, so
        that the state color reads the same under every desktop theme.

        filled=False (idle) gives a hollow body: you can tell it is off
        without looking at the color. badge=True (hands-free) also adds a dot
        -- that is the state that matters most, and green vs. red alone is
        not enough for colorblind users.
        """
        from PyQt6.QtCore import QRectF, Qt
        from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

        pm = QPixmap(64, 64)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(color_hex)
        pen = QPen(color, 6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)

        # Body (capsule)
        if filled:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(23, 5, 18, 33), 9, 9)
        else:
            p.setPen(QPen(color, 5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(24.5, 6.5, 15, 30), 7.5, 7.5)

        # Holder arc + stem + base
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(13, 12, 38, 38), 180 * 16, 180 * 16)
        p.drawLine(32, 50, 32, 57)
        p.drawLine(20, 57, 44, 57)

        if badge:
            # First punch a hole in the icon, then draw the dot, so that the
            # dot does not look glued to the body.
            p.setPen(Qt.PenStyle.NoPen)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.setBrush(Qt.GlobalColor.black)
            p.drawEllipse(QRectF(35, 35, 29, 29))
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.setBrush(color)
            p.drawEllipse(QRectF(39, 39, 21, 21))

        p.end()
        return QIcon(pm)

    # ---- menu ------------------------------------------------------------

    @staticmethod
    def _xdg_open(path):
        if not shutil.which("xdg-open"):
            log("xdg-open missing, could not open the file")
            return
        subprocess.Popen(["xdg-open", str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _open_settings(self):
        self._xdg_open(CONFIG_PATH)

    def _show_logs(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_PATH.touch(exist_ok=True)
        self._xdg_open(LOG_PATH)

    def _reload_settings(self):
        try:
            changed = self.ctrl.reload_config()
        except ConfigError as e:
            log(f"could not reload settings: {e}")
            self.ctrl.fb.notify(f"Could not reload settings: {e}", "dialog-error", 3000)
            return
        if changed:
            # These are bound to the evdev listener, a reload is not enough.
            self.ctrl.fb.notify(
                f"{'/'.join(changed)} changed - a restart is required for it "
                f"to take effect", "dialog-warning", 4000)
        else:
            self.ctrl.fb.notify("Settings reloaded",
                                "dialog-information", 1500)

    def _restart(self):
        self.hide()
        self.on_restart()

    def hide(self):
        self.tray.hide()


def restart_in_place(cleanup):
    """Replaces the process with itself (execv).

    Because the PID does not change, systemd does not count this as an exit:
    the Restart= policy never kicks in, the service does not enter a restart
    loop, and the journal shows no gap. The tray icon only disappears until
    the new process is up (~a second).
    """
    cleanup()
    log("restarting in place (execv)")
    sys.stdout.flush()
    os.execv(sys.executable,
             [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])


# --------------------------------------------------------------------------
# Keyboard listening
# --------------------------------------------------------------------------

def find_keyboards():
    found = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except (PermissionError, OSError):
            continue
        keys = dev.capabilities().get(ecodes.EV_KEY, [])
        if ecodes.KEY_A in keys and ecodes.KEY_Z in keys and ecodes.KEY_ESC in keys:
            found.append(dev)
        else:
            dev.close()
    return found


async def watch(dev, controller, hotkey_code, cancel_code):
    try:
        async for event in dev.async_read_loop():
            if event.type != ecodes.EV_KEY:
                continue
            if event.code == hotkey_code:
                if event.value == 1:
                    controller.on_hotkey_down()
                elif event.value == 0:
                    controller.on_hotkey_up()
            elif event.code == cancel_code and event.value == 1:
                controller.on_cancel_key()
    except OSError:
        log(f"device disappeared: {dev.path}")


def keycode_of(name: str) -> int:
    code = getattr(ecodes, name, None)
    if code is None:
        sys.exit(f"Unknown key name: {name}  (find it with --find-key)")
    return code


# --------------------------------------------------------------------------
# Helper modes
# --------------------------------------------------------------------------

def find_key_mode():
    devs = find_keyboards()
    if not devs:
        sys.exit("Could not read the keyboard. Are you in the 'input' group?  groups | grep input")
    print("Press a key (Ctrl-C to quit). Put the printed name in config.json > hotkey.\n")

    async def run():
        async def w(d):
            async for e in d.async_read_loop():
                if e.type == ecodes.EV_KEY and e.value == 1:
                    name = ecodes.KEY.get(e.code, "?")
                    if isinstance(name, list):
                        name = name[0]
                    print(f"  {name}   (code {e.code})")
        await asyncio.gather(*(w(d) for d in devs))

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print()


def doctor():
    print("vibedictate installation check\n")
    ok = True

    def check(label, cond, hint=""):
        nonlocal ok
        print(f"  [{'OK  ' if cond else 'MISS'}] {label}")
        if not cond:
            ok = False
            if hint:
                print(f"          -> {hint}")

    import getpass
    import grp
    user = getpass.getuser()
    # Being listed in /etc/group is not enough: group membership is only
    # applied to a process in a fresh session. Check the two separately,
    # otherwise you get a clueless "group is OK but the keyboard is not
    # readable" output.
    listed = "input" in [g.gr_name for g in grp.getgrall() if user in g.gr_mem]
    active = "input" in [grp.getgrgid(g).gr_name for g in os.getgroups()]
    check("'input' group membership", listed,
          "sudo usermod -aG input $USER")
    check("'input' group active in this session", active,
          "listed but not applied to this session -> reboot (logging out is not enough)")
    kbds = find_keyboards()
    check("/dev/input readable", bool(kbds),
          "could not open a keyboard -> see the 'input' group steps above")
    for d in kbds:
        d.close()
    check("recording tool (parecord/arecord)",
          any(shutil.which(c) for c in ("parecord", "pw-record", "arecord")),
          "sudo pacman -S libpulse")
    check("whisper-cli", bool(Transcriber._autodetect()),
          "sudo pacman -S whisper-cpp   (or ./setup.sh build for a CUDA build)")
    check("wl-clipboard", bool(shutil.which("wl-copy")),
          "sudo pacman -S wl-clipboard")
    check("ydotool", bool(shutil.which("ydotool")), "sudo pacman -S ydotool")
    check("ydotoold running",
          subprocess.run(["pgrep", "-x", "ydotoold"],
                         stdout=subprocess.DEVNULL).returncode == 0,
          "systemctl --user enable --now ydotoold")

    cfg = load_config()
    model = os.path.expanduser(cfg["whisper"]["model"])
    check(f"model file ({Path(model).name})", os.path.exists(model),
          "download it with:  ./setup.sh model")

    if cfg["tray"]["enabled"]:
        check("tray icon (python-pyqt6 + python-qasync)",
              tray_backend() is not None,
              "sudo pacman -S python-pyqt6 python-qasync"
              "   (or set config.json > tray.enabled = false)")
        # If the service starts before the graphical session, WAYLAND_DISPLAY
        # and DISPLAY are not in its environment and the tray icon silently
        # never shows up. We cannot detect that from a terminal (the
        # variables are set here), so we look at the unit file instead.
        unit = Path.home() / ".config/systemd/user/vibedictate.service"
        if unit.exists():
            check("service bound to graphical-session.target",
                  "graphical-session.target" in unit.read_text(),
                  "./setup.sh service   then   "
                  "systemctl --user restart vibedictate.service")

    if cfg["llm"]["enabled"]:
        try:
            import urllib.request
            urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2)
            check("Ollama reachable", True)
        except Exception:
            check("Ollama reachable", False, "ollama serve")

    print("\n" + ("Everything is ready." if ok else "Something is missing, see above."))
    return 0 if ok else 1


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="offline dictation (Wayland)")
    ap.add_argument("--find-key", action="store_true", help="print the name of a key")
    ap.add_argument("--doctor", action="store_true", help="check the installation")
    args = ap.parse_args()

    if args.find_key:
        return find_key_mode()
    if args.doctor:
        return doctor()

    cfg = load_config()
    devs = find_keyboards()
    if not devs:
        sys.exit("No keyboard found. You need to be in the 'input' group:\n"
                 "  sudo usermod -aG input $USER   (then reboot)")

    hotkey = keycode_of(cfg["hotkey"])
    cancel = keycode_of(cfg["cancel_key"])

    # With the tray on, the Qt loop is the main loop and qasync binds asyncio
    # to it -- single thread, no periodic pump. Without the tray, plain
    # asyncio.
    backend = tray_backend() if cfg["tray"]["enabled"] else None
    app = None
    if backend:
        QtWidgets, qasync = backend
        app = QtWidgets.QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)   # do not quit when the menu closes
        loop = qasync.QEventLoop(app)
    else:
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        ctrl = DictationController(cfg, loop)
    except ConfigError as e:
        sys.exit(str(e))

    def close_devices():
        for d in devs:
            try:
                d.close()
            except Exception:
                pass

    tray = None
    if app is not None:
        from PyQt6.QtWidgets import QSystemTrayIcon
        if QSystemTrayIcon.isSystemTrayAvailable():
            tray = TrayIcon(ctrl, app, on_quit=loop.stop,
                            on_restart=lambda: restart_in_place(close_devices))
        else:
            # Usually the reason is: the service started before the graphical
            # session and WAYLAND_DISPLAY/DISPLAY are not in the environment.
            # setup.sh service fixes that with a graphical-session.target
            # dependency.
            log("no system tray - the graphical session may be unreachable")

    log(f"ready - hotkey {cfg['hotkey']}, listening on {len(devs)} keyboard(s)")
    log(f"model: {ctrl.transcriber.model}")
    log(f"LLM cleanup: {'on (' + cfg['llm']['model'] + ')' if cfg['llm']['enabled'] else 'off'}")
    log(f"tray icon: {'on' if tray else 'off'}")
    ctrl.fb.notify("Dictation ready", "audio-input-microphone", 1200, progress=True)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, loop.stop)

    for d in devs:
        loop.create_task(watch(d, ctrl, hotkey, cancel))

    try:
        loop.run_forever()
    finally:
        log("shutting down")
        if tray:
            tray.hide()
        close_devices()


if __name__ == "__main__":
    sys.exit(main() or 0)
