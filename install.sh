#!/usr/bin/env bash
# Host Load installer for macOS and Linux.
#
# From a downloaded copy:   ./install.sh            (or double-click Install-macOS.command)
# Without downloading:      curl -fsSL https://raw.githubusercontent.com/mrlunev/busybar-host-load/main/install.sh | bash
# To remove everything:     ./install.sh --uninstall
#
# All it does itself is make sure uv (https://docs.astral.sh/uv/) is available; uv
# then runs setup/hostload_setup.py with a Python it manages, so nothing else has
# to be installed beforehand.
set -euo pipefail

RAW_BASE="https://raw.githubusercontent.com/mrlunev/busybar-host-load/main"

action="install"
args=()
for arg in "$@"; do
  case "$arg" in
    --uninstall|uninstall) action="uninstall" ;;
    *) args+=("$arg") ;;
  esac
done

find_uv() {
  if command -v uv >/dev/null 2>&1; then command -v uv; return 0; fi
  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then echo "$candidate"; return 0; fi
  done
  return 1
}

uv_bin="$(find_uv || true)"
if [ -z "$uv_bin" ]; then
  if [ "$action" = "uninstall" ]; then
    echo "uv is not installed, so neither is Host Load's Glances. Nothing to remove on this computer."
    exit 0
  fi
  echo "Installing uv, the tool that sets up Python for Host Load (https://docs.astral.sh/uv/)..."
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
  uv_bin="$(find_uv)"
  export HOSTLOAD_UV_INSTALLED=1
fi
export HOSTLOAD_UV="$uv_bin"
# Run on a Python already on this computer when there is one; uv downloads one only if not.
export UV_PYTHON_PREFERENCE=system

# Use the copy next to this file when there is one, otherwise fetch it.
here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
script="$here/setup/hostload_setup.py"
if [ -z "$here" ] || [ ! -f "$script" ]; then
  script="$RAW_BASE/setup/hostload_setup.py"
fi

# Piped from curl, stdin is the script itself; questions must read the terminal.
if [ ! -t 0 ] && [ -r /dev/tty ]; then
  exec "$uv_bin" run --quiet --no-project "$script" "$action" ${args[@]+"${args[@]}"} </dev/tty
fi
exec "$uv_bin" run --quiet --no-project "$script" "$action" ${args[@]+"${args[@]}"}
