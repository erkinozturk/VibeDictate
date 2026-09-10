# VibeDictate

**Türkçe:** [README.tr.md](README.tr.md)

Push-to-talk dictation for **Linux / Wayland**, **fully offline**.

Hold the key, speak, release: your speech is transcribed and pasted wherever
the cursor happens to be — terminal, browser, editor, anywhere. No network
calls; the whisper.cpp model runs on your own machine.

Inspired by the Wispr Flow style of push-to-talk dictation; written from
scratch for Linux/Wayland, with no code taken from any other project.

**Requirements:** a Wayland session (KDE / GNOME / Hyprland — the compositor
does not matter) and an Arch-based distribution (`setup.sh` uses `pacman`).
**An NVIDIA GPU is optional** — with one you get CUDA + `large-v3-turbo`,
without one it quietly falls back to CPU + `small`.

```bash
git clone https://github.com/erkinozturk/VibeDictate.git
cd VibeDictate
./setup.sh all        # packages → permissions → model → build → service
# REBOOT for the input group (a logout is not enough), then:
vibedictate --doctor
systemctl --user enable --now vibedictate.service
```

The default hotkey is **right Ctrl**. The default language is **auto-detect**
(`language: "auto"`); if you always dictate in one language, set
`config.json > language` to it (e.g. `"tr"`, `"de"`, `"en"`) so whisper does
not have to guess every time.

---

## Usage

| Gesture | What happens |
| --- | --- |
| **Hold** the hotkey | Records while held. On release: transcribe → clean up → paste. |
| **Double tap** the hotkey | Starts a **hands-free** session — records without holding the key. |
| **Single tap** while hands-free | Ends the session and processes it. |
| **Esc** while recording | Discards the recording, nothing is typed. |
| **Esc** while processing | Kills the running whisper process. |
| _(forgotten session)_ | Stops by itself after 5 minutes (`max_handsfree_seconds`). |

Keys are listened to **passively** via `evdev` — the key you press still
reaches the focused application. Wayland offers no global shortcut API, so we
read at the `/dev/input` level; that also makes this independent of the
compositor.

Right Ctrl is rarely used and is a single key, which makes it a good fit for
push-to-talk. To change it, run `vibedictate --find-key`, press the key you
want, and put the printed name into `config.json > hotkey`.

---

## System tray

A microphone icon sits in the tray and its color follows the state machine
exactly:

| Icon | State | Meaning |
| --- | --- | --- |
| Hollow **grey** microphone | `idle` | Idle, microphone closed |
| Solid **red** microphone | `recording` | Hotkey held, recording |
| Solid **green** microphone + dot | `handsfree` | **Hands-free session — microphone is open** |
| Solid **orange** microphone | `processing` | Transcribing |

Green is the one that matters: in a hands-free session you are not holding
any key, so it is easy to forget that the microphone is still open. The dot
next to it distinguishes that state on themes that wash out colors and for
colorblind users. There is no animation or blinking — some tray
implementations buffer icon updates and end up flickering, while a solid
color looks the same everywhere. Hovering the icon also spells out the state.

Right-click menu: **Open settings** (`config.json` via `xdg-open`), **Reload
settings**, **Show logs**, **Restart**, **Quit**.

**Reload settings** applies the whisper model, the LLM settings, the paste
method and the notification settings without restarting the app. `hotkey` and
`cancel_key` are the exception: they are bound to the evdev listener at
startup, so they need a restart — if they changed, the notification says so.
If the new settings are broken (e.g. a missing model file) nothing changes
and the running setup stays up.

**Restart** uses `os.execv`: since the PID does not change, systemd does not
count it as an exit, the `Restart=` policy never kicks in, and the service
does not enter a restart loop.

The tray is optional: with `config.json > tray.enabled` set to `false`, or
without `python-pyqt6` / `python-qasync` installed, the program runs without
a tray — no error, just one line in the log.

---

## Installation

`./setup.sh all` does everything in one command. You can also run the steps
separately — the order matters:

```bash
./setup.sh deps       # packages (cmake/git with a GPU, whisper-cpp without)
./setup.sh perms      # input group + /dev/uinput udev rule
./setup.sh model      # whisper model (size is chosen by GPU if omitted)
./setup.sh build      # builds whisper.cpp with CUDA when a GPU is present
./setup.sh service    # ydotoold + vibedictate systemd user services
```

You can also pass the size explicitly:
`./setup.sh model small|medium|large-v3-turbo`

### What happens when you have a GPU

If `nvcc` **and** a working NVIDIA driver are present, the `build` step
clones whisper.cpp into `~/.local/share/vibedictate/whisper.cpp` and builds
it with CUDA:

* the compute capability is detected automatically via
  `nvidia-smi --query-gpu=compute_cap` (e.g. RTX 3050 →
  `-DCMAKE_CUDA_ARCHITECTURES=86`),
* the host compiler is taken from `$NVCC_CCBIN` (see the pitfalls below),
* the `large-v3-turbo` model is downloaded,
* `config.json > whisper.bin` and `whisper.model` are written accordingly.

The build takes a few minutes and happens once; if the binary is in place,
later runs skip it (force a rebuild with
`rm -rf ~/.local/share/vibedictate/whisper.cpp/build`).

Without a GPU the `build` step passes quietly, the `whisper-cli` from the
`whisper-cpp` package installed by `deps` is used from PATH, and the model is
`small`.

### Checking

```bash
vibedictate --doctor
```

Every prerequisite is checked one by one:

```
vibedictate installation check

  [OK  ] 'input' group membership
  [OK  ] 'input' group active in this session
  [OK  ] /dev/input readable
  [OK  ] recording tool (parecord/arecord)
  [OK  ] whisper-cli
  [OK  ] wl-clipboard
  [OK  ] ydotool
  [OK  ] ydotoold running
  [OK  ] model file (ggml-large-v3-turbo.bin)
  [OK  ] tray icon (python-pyqt6 + python-qasync)
  [OK  ] service bound to graphical-session.target

Everything is ready.
```

Anything missing is marked `[MISS]` with the fix printed underneath.

Once everything is OK, start it:

```bash
systemctl --user enable --now vibedictate.service
journalctl --user -u vibedictate -f
```

To try it by hand first: `./vibedictate.py`

---

## Choosing a model

For any language other than English you need a multilingual model (the ones
with an `.en` suffix are English-only).

| Model | Size | CPU (Ryzen 5 5500) | Quality |
| --- | --- | --- | --- |
| `small` | ~466 MB | 10 s of audio ≈ 3-5 s | Good enough for everyday dictation |
| `medium` | ~1.5 GB | 10 s of audio ≈ 10-15 s | Noticeably better |
| `large-v3-turbo` | ~1.6 GB | Slow on CPU | Best; faster than real time with CUDA |

---

## Settings

`~/.config/vibedictate/config.json` — created with the defaults on the first
run. After editing, use **Reload settings** from the tray, or
`systemctl --user restart vibedictate`.

Two example files ship with the repo:

* `config.example.json` — template with every key (CPU / `small`)
* `config.cuda-example.json` — a CUDA + `large-v3-turbo` setup

**Fix the paths in the example files to match your own system.** Replace the
`/home/USER/...` path in `config.cuda-example.json` with your own username
(or just run `./setup.sh build`, which writes the correct path into
`config.json` for you).

| Key | What it does |
| --- | --- |
| `hotkey` / `cancel_key` | evdev key names (find them with `--find-key`) |
| `tap_threshold_ms` | Holding shorter than this counts as a "tap" |
| `double_tap_window_ms` | Two taps closer than this start a hands-free session |
| `max_handsfree_seconds` | How long before a forgotten session stops itself |
| `min_recording_ms` | Recordings shorter than this are discarded unprocessed |
| `stop_tail_ms` | How much longer recording continues after the key is released |
| `language` | Whisper language; `"auto"` also works |
| `whisper.bin` | Path to `whisper-cli`; found on PATH when empty |
| `whisper.model` | The `ggml-*.bin` model file |
| `whisper.threads` | `0` = number of CPU cores − 1 |
| `whisper.extra_args` | Extra flags for whisper-cli (e.g. `["-bs", "5"]`) |
| `llm.*` | Optional local Ollama cleanup (below) |
| `output.method` | `"paste"` (clipboard + Ctrl+V) or `"type"` (key by key) |
| `output.paste_keycodes` | Default `[29, 47]` = Ctrl+V |
| `notify` / `notify_progress` | All notifications / only the intermediate ones |
| `sound`, `keep_wav` | Sound effect; keep the recorded WAV in `/tmp` (debugging) |
| `tray.enabled` | The tray icon |

### Paste shortcut

The default is `Ctrl+V` (`[29, 47]`). If your terminal needs `Ctrl+Shift+V`,
use `[29, 42, 47]` (42 = LEFTSHIFT).

### Recording tail (`stop_tail_ms`)

When the hotkey is released the recording process is not killed immediately;
it keeps being read for another `stop_tail_ms` (300 ms by default). Data from
the audio device is always ~100-175 ms behind real time, so closing instantly
eats the last syllable of the sentence. The wait happens in the recording
worker and does not block the key-listening loop. Set it to `0` for the old
behavior.

### Notifications (`notify_progress`)

`notify` turns all notifications on and off. `notify_progress` only silences
the intermediate status ones ("Recording…", "Transcribing…"); result
notifications, errors and cancellation messages still come through. Since the
tray icon already shows the state, `false` is a sensible choice.

---

## LLM cleanup (optional, local)

```json
"llm": { "enabled": true, "model": "qwen2.5:3b" }
```

A small, fast Ollama model is enough — the job is only filler-word removal
and punctuation. Do not use a code model; `qwen2.5:3b` or `llama3.2:3b` fit
better.

The default prompt is written in English but is **language-neutral**: the
model is told to keep the text in its original language, and no
language-specific filler word list is baked in. Dictation in any language
goes through the same prompt.

```bash
ollama pull qwen2.5:3b
```

If the model goes off the rails or the request fails, the raw transcript is
used as is — the tool never swallows your words. The request goes to
`127.0.0.1:11434` and never leaves your machine.

---

## Known pitfalls

Every one of these bit us during setup; they deserve their own section
because they all fail *without* an error message.

**A logout is not enough for the `input` group — you have to reboot.**
The membership is written to `/etc/group` and shows up in a fresh login
session, but the systemd **user manager** that runs the service does not die
on logout/login: it keeps running with the old group list, so vibedictate
still cannot open `/dev/input`. `--doctor` shows this as two separate lines:
`'input' group membership` is OK while `'input' group active in this session`
is `[MISS]`.

**On Arch the package is `whisper-cpp`, not `whisper.cpp`.** A dash, not a
dot, and it is in the official `extra` repo (no need to look in the AUR).
CachyOS ships an x86_64_v3 optimized build.

**There is no `gcc`/`g++` symlink inside `/opt/cuda/bin`.** nvcc does not
accept every gcc version, and the "the cuda package brings its own compiler"
assumption does not hold on Arch. The host compiler has to be passed
explicitly; `setup.sh` reads it from `$NVCC_CCBIN`:

```bash
NVCC_CCBIN=/usr/bin/g++-15 ./setup.sh build
```

If the variable is empty the script looks for `/opt/cuda/bin/g++` and
`g++-15/14/13`, and falls back to the cmake default — that is where an
`unsupported GNU version` error comes from.

**Without `parecord --latency-msec=50`, short recordings come out silently
empty.** With the default buffering, `parecord` writes nothing to stdout for
the first ~2 seconds, so dictations shorter than that give us zero bytes and
whisper returns empty text. No error, no warning, simply nothing gets pasted.
`pw-record` and `arecord` do not have this problem.

**The systemd unit must be bound to `graphical-session.target`.** With
`WantedBy=default.target` the service starts **before** the graphical session
and `WAYLAND_DISPLAY` / `DISPLAY` are never in its environment. The result:
the tray icon never appears and `wl-copy` does not work — both without any
visible error. The correct form is `After=` + `PartOf=` +
`WantedBy=graphical-session.target`; `setup.sh service` writes that and also
migrates the old `default.target` symlink. `--doctor` inspects the unit file
and warns about it.

**The running copy and the development copy are separate.** The service runs
`~/.local/bin/vibedictate`; `vibedictate.py` in the repo is only the source.
After changing the code you have to refresh the installation:

```bash
./setup.sh service && systemctl --user restart vibedictate.service
```

---

## Troubleshooting

**"No keyboard found"** → you are not in the `input` group, or you have not
rebooted. `vibedictate --doctor` tells you which one.

**Text lands in the clipboard but is not pasted** → `ydotoold` is not
running: `systemctl --user status ydotoold`. If that does not help, check the
`/dev/uinput` permission — the group in `ls -l /dev/uinput` must be `input`.

**Nothing is pasted and the clipboard is empty too** → the service may have
started before the graphical session; see the `graphical-session.target`
pitfall above.

**The transcript comes out empty** → first check whether the wrong microphone
source is selected. Set `config.json > keep_wav: true` and listen to
`/tmp/vibedictate-*.wav`. If the recording itself is empty or truncated, the
`parecord --latency-msec` pitfall is in play.

**Right Ctrl triggers something else** → pick another key with
`vibedictate --find-key`.

**The tray icon does not show up** → run `vibedictate --doctor`. The two most
common reasons: `python-pyqt6`/`python-qasync` are not installed, or the
`graphical-session.target` issue again:

```bash
./setup.sh service
systemctl --user restart vibedictate.service
systemctl --user show -p Environment vibedictate.service   # check
```

**The CUDA build says `unsupported GNU version`** →
`NVCC_CCBIN=/usr/bin/g++-15 ./setup.sh build` (replace the version with a gcc
that your nvcc accepts).

**Dictation is slow** → look at the model name in `vibedictate --doctor`. If
you have a GPU you should be on `large-v3-turbo` + the CUDA build; on CPU
that model is slower than real time, so drop to `small`.

Logs: `~/.local/share/vibedictate/vibedictate.log`
or `journalctl --user -u vibedictate -f`

---

## What is here

```
vibedictate.py          single-file daemon
  Recorder              parecord/pw-record/arecord -> raw 16 kHz mono PCM
  Transcriber           whisper-cli subprocess, cancellable
  LocalCleaner          Ollama /api/generate, falls back to the raw text
  TextInserter          wl-copy + ydotool Ctrl+V (or direct typing)
  DictationController   record -> transcribe -> clean -> paste state machine
  TrayIcon              QSystemTrayIcon, shows the state by color (optional)
  watch()               passive evdev keyboard listening
setup.sh                packages, permissions, model, CUDA build, systemd
config.example.json     settings template (CPU / small)
config.cuda-example.json  CUDA + large-v3-turbo example
README.md               this file
README.tr.md            Turkish documentation
```

There is no networking code: `urllib` only talks to `127.0.0.1:11434`, and
only when `llm.enabled` is on.

With the tray on, the Qt loop is the main loop and `qasync` binds asyncio to
it — a single thread, no periodic "pump". With the tray off it is a plain
asyncio loop and Qt is never imported.

---

## License and maintenance

MIT — see [LICENSE](LICENSE).

This is a personal tool. It was written to work on my machine and is shared
as such; no active maintenance is promised, and issues and pull requests may
go unanswered. Forking it and adapting it to your own setup is the healthiest
option.
