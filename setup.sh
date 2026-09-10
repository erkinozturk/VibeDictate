#!/usr/bin/env bash
# vibedictate installer (CachyOS / Arch)
#
# Usage:
#   ./setup.sh all                       one-command install (recommended)
#   ./setup.sh deps | perms | model [small|medium|large-v3-turbo] | build | service
#
# Order used by "all": packages -> permissions -> model -> build -> service
# With an NVIDIA GPU and nvcc present, whisper.cpp is built with CUDA and
# large-v3-turbo is used; otherwise it quietly falls back to the whisper-cpp
# package and the small model.
set -euo pipefail

INSTALL_DIR="$HOME/.local/share/vibedictate"
MODEL_DIR="$INSTALL_DIR/models"
SRC_DIR="$INSTALL_DIR/whisper.cpp"
BIN_DIR="$HOME/.local/bin"

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m    WARNING:\033[0m %s\n' "$*"; }

# --------------------------------------------------------------------------
# GPU detection
# --------------------------------------------------------------------------

# A CUDA build needs both the driver (nvidia-smi must see a GPU) and the
# compiler (nvcc). If either is missing we take the CPU path -- that is not
# an error, just slower.
has_cuda() {
  command -v nvcc >/dev/null 2>&1 || return 1
  command -v nvidia-smi >/dev/null 2>&1 || return 1
  nvidia-smi -L 2>/dev/null | grep -q '^GPU 0:' || return 1
}

# Compute capability -> CMAKE_CUDA_ARCHITECTURES. "8.6" -> "86".
# If it cannot be read, leave the detection to cmake's own "native".
cuda_arch() {
  local cap
  cap="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null \
         | head -n1 | tr -d '[:space:].')"
  [[ "$cap" =~ ^[0-9]+$ ]] && echo "$cap" || echo "native"
}

# nvcc does not accept every gcc version, so we pass the host compiler
# explicitly. On Arch there is no gcc/g++ symlink inside /opt/cuda/bin, so
# the "the cuda package takes care of it" assumption does not hold: we look
# at $NVCC_CCBIN first.
host_compiler() {
  if [[ -n "${NVCC_CCBIN:-}" ]]; then echo "$NVCC_CCBIN"; return; fi
  local c
  for c in /opt/cuda/bin/g++ /usr/bin/g++-15 /usr/bin/g++-14 /usr/bin/g++-13; do
    [[ -x "$c" ]] && { echo "$c"; return; }
  done
  echo ""
}

# --------------------------------------------------------------------------
# Writing config.json
# --------------------------------------------------------------------------

# The config file is created on the first run, so it may not exist yet during
# install. If it does, we merge only the given keys into it and the user's own
# edits stay untouched.
config_set() {
  python3 -c '
import json, os, sys
from pathlib import Path
patch = json.loads(sys.argv[1])
base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
p = Path(base) / "vibedictate" / "config.json"
p.parent.mkdir(parents=True, exist_ok=True)
cur = {}
if p.exists():
    try:
        cur = json.loads(p.read_text())
    except Exception:
        cur = {}
def merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            merge(a[k], v)
        else:
            a[k] = v
    return a
p.write_text(json.dumps(merge(cur, patch), indent=2, ensure_ascii=False) + "\n")
print("    config: %s" % p)
' "$1"
}

# --------------------------------------------------------------------------

deps() {
  say "Installing packages"
  # python-pyqt6 / python-qasync are only needed for the tray icon. Without
  # them, or with config.json > tray.enabled false, the program runs trayless.
  local pkgs=(
    python-evdev
    libpulse
    wl-clipboard
    ydotool
    libnotify
    curl
    python-pyqt6
    python-qasync
  )

  if has_cuda; then
    echo "    NVIDIA GPU + nvcc found -> whisper.cpp will be built from source"
    pkgs+=(git cmake base-devel)
  else
    # On Arch/CachyOS the package is called "whisper-cpp" (a dash, not a dot)
    # and lives in the official "extra" repo; CachyOS ships an x86_64_v3
    # optimized build.
    echo "    No NVIDIA GPU/nvcc -> using the prebuilt whisper-cpp package (CPU)"
    pkgs+=(whisper-cpp)
  fi

  sudo pacman -S --needed --noconfirm "${pkgs[@]}"
}

perms() {
  say "Keyboard read permission (the 'input' group)"
  if id -nG "$USER" | grep -qw input; then
    echo "    already in the input group"
  else
    sudo usermod -aG input "$USER"
    # A logout/login is not enough: the systemd user manager (and the service
    # under it) keeps running with the old group list. Reboot.
    warn "added to the group -> YOU MUST REBOOT (a logout is not enough)"
  fi

  say "/dev/uinput permission (for ydotool)"
  echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' \
    | sudo tee /etc/udev/rules.d/80-uinput.rules >/dev/null
  sudo udevadm control --reload-rules
  sudo udevadm trigger --name-match=uinput || true
  sudo modprobe uinput || true
  echo "    udev rule written"
}

model() {
  # No size given: large-v3-turbo with a GPU, small without one.
  local size="${1:-}"
  if [[ -z "$size" ]]; then
    if has_cuda; then size="large-v3-turbo"; else size="small"; fi
  fi

  local file
  case "$size" in
    small)           file="ggml-small.bin" ;;
    medium)          file="ggml-medium.bin" ;;
    large-v3-turbo)  file="ggml-large-v3-turbo.bin" ;;
    *) echo "Unknown size: $size"; exit 1 ;;
  esac

  mkdir -p "$MODEL_DIR"
  if [[ -f "$MODEL_DIR/$file" ]]; then
    say "$file already present, skipping the download"
  else
    say "Downloading $file (multilingual model)"
    # --fail is essential: without it a 404 HTML page gets written to disk as
    # if it were the model.
    curl -fL --progress-bar \
      -o "$MODEL_DIR/$file" \
      "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$file" \
      || { rm -f "$MODEL_DIR/$file"; echo "Download failed."; exit 1; }
    echo "    -> $MODEL_DIR/$file"
  fi

  config_set "$(printf '{"whisper": {"model": "%s"}}' "$MODEL_DIR/$file")"
}

build() {
  if ! has_cuda; then
    say "Skipping the CUDA build (no NVIDIA GPU / nvcc)"
    if command -v whisper-cli >/dev/null 2>&1; then
      echo "    using the packaged whisper-cli: $(command -v whisper-cli)"
      # bin is left empty on purpose: vibedictate finds it on PATH itself.
      config_set '{"whisper": {"bin": "", "extra_args": []}}'
    else
      warn "whisper-cli is not on PATH. Run './setup.sh deps'."
    fi
    return
  fi

  local bin="$SRC_DIR/build/bin/whisper-cli"
  if [[ -x "$bin" ]]; then
    say "whisper.cpp is already built, skipping"
    echo "    to rebuild: rm -rf $SRC_DIR/build"
  else
    say "Building whisper.cpp with CUDA"
    mkdir -p "$INSTALL_DIR"
    if [[ -d "$SRC_DIR/.git" ]]; then
      echo "    source already present: $SRC_DIR"
    else
      git clone --depth 1 https://github.com/ggml-org/whisper.cpp "$SRC_DIR"
    fi

    local arch ccbin cmake_args
    arch="$(cuda_arch)"
    ccbin="$(host_compiler)"
    cmake_args=(-B "$SRC_DIR/build" -S "$SRC_DIR"
                -DCMAKE_BUILD_TYPE=Release
                -DGGML_CUDA=1
                -DCMAKE_CUDA_ARCHITECTURES="$arch")

    echo "    compute capability: $arch"
    if [[ -n "$ccbin" ]]; then
      echo "    host compiler.....: $ccbin"
      cmake_args+=(-DCMAKE_CUDA_HOST_COMPILER="$ccbin")
    else
      warn "no host compiler found; trying the cmake default."
      warn "if you get 'unsupported GNU version':  NVCC_CCBIN=/usr/bin/g++-15 ./setup.sh build"
    fi

    cmake "${cmake_args[@]}"
    cmake --build "$SRC_DIR/build" -j --config Release

    [[ -x "$bin" ]] || { echo "Build finished but $bin is missing."; exit 1; }
  fi

  config_set "$(printf '{"whisper": {"bin": "%s", "extra_args": ["-bs", "5"]}}' "$bin")"
  echo "    -> $bin"
}

service() {
  say "Installing services"
  mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$HOME/.config/systemd/user"

  # This is the copy that actually runs; the file in the repo is the
  # development copy. After changing the code you must run this step again
  # for the change to take effect.
  install -m 755 "$(dirname "$0")/vibedictate.py" "$BIN_DIR/vibedictate"

  # ydotoold (user service)
  cat > "$HOME/.config/systemd/user/ydotoold.service" <<'EOF'
[Unit]
Description=ydotool daemon

[Service]
ExecStart=/usr/bin/ydotoold
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF

  # graphical-session.target is essential: the tray icon needs
  # WAYLAND_DISPLAY/DISPLAY, and those are only handed to the systemd user
  # manager as the graphical session comes up. Bound to default.target the
  # service starts earlier, those variables are absent from its environment,
  # and the tray icon never appears.
  cat > "$HOME/.config/systemd/user/vibedictate.service" <<EOF
[Unit]
Description=vibedictate - offline dictation
After=graphical-session.target ydotoold.service
PartOf=graphical-session.target
Wants=ydotoold.service

[Service]
ExecStart=$BIN_DIR/vibedictate
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
EOF

  systemctl --user daemon-reload

  # Older installs used WantedBy=default.target; if that symlink is still
  # around the service starts early again. If it is enabled, move the symlink
  # to the new target.
  if systemctl --user is-enabled --quiet vibedictate.service 2>/dev/null; then
    systemctl --user reenable vibedictate.service >/dev/null
    echo "    vibedictate.service moved to graphical-session.target"
    echo "    (to activate: systemctl --user restart vibedictate.service)"
  fi
  systemctl --user enable --now ydotoold.service
  echo "    ydotoold is running"

  # If BIN_DIR is not on PATH the "vibedictate" command cannot be found.
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
      echo
      warn "$BIN_DIR is not on PATH, the 'vibedictate' command will not be found."
      echo "       bash/zsh:  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
      echo "       fish....:  fish_add_path ~/.local/bin"
      ;;
  esac
  echo
  echo "    To start the dictation service:"
  echo "       systemctl --user enable --now vibedictate.service"
  echo "       journalctl --user -u vibedictate -f      # logs"
}

case "${1:-all}" in
  deps)    deps ;;
  perms)   perms ;;
  model)   model "${2:-}" ;;
  build)   build ;;
  service) service ;;
  all)     deps; perms; model "${2:-}"; build; service ;;
  *) echo "Usage: $0 {deps|perms|model [small|medium|large-v3-turbo]|build|service|all}"; exit 1 ;;
esac

say "Done. Check with:  vibedictate --doctor"
