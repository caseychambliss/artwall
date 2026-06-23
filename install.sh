#!/usr/bin/env bash
# artwall install script
# ============================================================
# Sets up artwall for a single Linux user:
#   1. Checks Python 3.9+ is available
#   2. Installs pip dependencies
#   3. Creates the config directory and copies config.ini.example
#      (skips copy if a config.ini already exists)
#   4. Installs the systemd user service and timer
#   5. Enables and starts the timer
#   6. Runs artwall once immediately
#
# Usage:
#   bash install.sh
#
# To uninstall:
#   bash install.sh --uninstall

set -euo pipefail

# ── Colour output ──────────────────────────────────────────────────────────────
GREEN="\033[92m"
YELLOW="\033[93m"
RED="\033[91m"
CYAN="\033[96m"
RESET="\033[0m"

ok()   { echo -e "  ${GREEN}OK${RESET}   $*"; }
warn() { echo -e "  ${YELLOW}WARN${RESET} $*"; }
err()  { echo -e "  ${RED}ERR${RESET}  $*" >&2; }
info() { echo -e "  ${CYAN}--${RESET}   $*"; }

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTWALL_PY="${SCRIPT_DIR}/artwall.py"
CONFIG_DIR="${HOME}/.config/artwall"
CONFIG_FILE="${CONFIG_DIR}/config.ini"
SYSTEMD_DIR="${HOME}/.config/systemd/user"

# ── Uninstall ──────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
    echo
    info "Uninstalling artwall..."
    systemctl --user stop  artwall.timer  2>/dev/null || true
    systemctl --user disable artwall.timer 2>/dev/null || true
    rm -f "${SYSTEMD_DIR}/artwall.service" "${SYSTEMD_DIR}/artwall.timer"
    systemctl --user daemon-reload 2>/dev/null || true
    ok "systemd timer and service removed"
    info "Config and cache were not removed. To clean up fully:"
    echo "      rm -rf ${CONFIG_DIR} ~/.cache/artwall"
    echo
    exit 0
fi

echo
echo -e "  ${CYAN}artwall${RESET} installer"
echo

# ── Step 1: Python version check ──────────────────────────────────────────────
info "[1/5] Checking Python version"
PYTHON_BIN=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done
if [[ -z "$PYTHON_BIN" ]]; then
    err "Python 3.9 or higher is required but was not found."
    err "Install it with:  sudo apt install python3"
    exit 1
fi
ok "Found: $(${PYTHON_BIN} --version)"

# ── Step 2: Install pip dependencies ──────────────────────────────────────────
info "[2/5] Installing Python dependencies"
if "${PYTHON_BIN}" -m pip install -r "${SCRIPT_DIR}/requirements.txt" \
        --break-system-packages --quiet 2>/dev/null; then
    ok "Dependencies installed (system pip)"
elif "${PYTHON_BIN}" -m pip install -r "${SCRIPT_DIR}/requirements.txt" \
        --quiet; then
    ok "Dependencies installed (user pip)"
else
    err "pip install failed. Try manually:"
    err "  pip install requests Pillow --break-system-packages"
    exit 1
fi

# ── Step 3: Config setup ───────────────────────────────────────────────────────
info "[3/5] Setting up configuration"
mkdir -p "${CONFIG_DIR}"
if [[ -f "${CONFIG_FILE}" ]]; then
    warn "Config already exists at ${CONFIG_FILE} -- not overwriting"
    warn "Delete it and re-run install.sh to reset to defaults"
else
    cp "${SCRIPT_DIR}/config.ini.example" "${CONFIG_FILE}"
    ok "Config created at ${CONFIG_FILE}"
    echo
    warn "NEXT STEP: edit ${CONFIG_FILE} before the first run."
    warn "At minimum, review the [filters] section and enable/disable sources."
    warn "If you want Rijksmuseum, register for a free API key at:"
    warn "  https://www.rijksmuseum.nl/en/research/conduct-research/data/access-to-and-use-of-the-rijksmuseum-api"
    warn "Then add your key under [sources] in config.ini."
    echo
fi

# ── Step 4: systemd service and timer ─────────────────────────────────────────
info "[4/5] Installing systemd user service and timer"
mkdir -p "${SYSTEMD_DIR}"

# Read interval_hours from config
INTERVAL_HOURS=$(grep -E "^interval_hours" "${CONFIG_FILE}" | head -1 | awk -F= '{print $2}' | tr -d ' ')
INTERVAL_HOURS="${INTERVAL_HOURS:-24}"
INTERVAL="${INTERVAL_HOURS}h"

# Write service file with the actual path to artwall.py
sed "s|ARTWALL_EXEC_PATH|${PYTHON_BIN} ${ARTWALL_PY}|g" \
    "${SCRIPT_DIR}/systemd/artwall.service" > "${SYSTEMD_DIR}/artwall.service"

# Write timer file with the configured interval
sed "s|ARTWALL_INTERVAL|${INTERVAL}|g" \
    "${SCRIPT_DIR}/systemd/artwall.timer" > "${SYSTEMD_DIR}/artwall.timer"

ok "Service installed at ${SYSTEMD_DIR}/artwall.service"
ok "Timer installed at ${SYSTEMD_DIR}/artwall.timer (interval: ${INTERVAL})"

systemctl --user daemon-reload
systemctl --user enable artwall.timer
systemctl --user start  artwall.timer
ok "Timer enabled and started"
info "Check status with:  systemctl --user status artwall.timer"

# ── Step 5: First run ──────────────────────────────────────────name────────────
info "[5/5] Running artwall for the first time"
echo
"${PYTHON_BIN}" "${ARTWALL_PY}"

echo
ok "Installation complete."
info "artwall will rotate your wallpaper every ${INTERVAL_HOURS} hours."
info "Run manually at any time:  python3 ${ARTWALL_PY}"
info "View current artwork info: python3 ${ARTWALL_PY} --info"
info "Uninstall:                 bash ${SCRIPT_DIR}/install.sh --uninstall"
echo
