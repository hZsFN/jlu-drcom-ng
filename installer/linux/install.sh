#!/usr/bin/env sh
# Installer for JLU DrCOM NG on Linux (desktop bundle or standalone drcomd).
#
# Usage:
#   ./install.sh                   # install for current user (~/.local)
#   ./install.sh --systemd         # + register systemd --user service
#   ./install.sh --desktop         # + register XDG desktop entry (.desktop)
#   ./install.sh --system          # install system-wide under /opt/jlu-drcom-ng
#   ./install.sh --uninstall       # remove installed files, desktop & systemd unit

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
APP_NAME="jlu-drcom-ng"
UNIT_NAME="jlu-drcom-ng.service"

WITH_SYSTEMD=0
WITH_DESKTOP=0
SYSTEM_WIDE=0
UNINSTALL=0

for arg in "$@"; do
    case "$arg" in
        --systemd)   WITH_SYSTEMD=1 ;;
        --desktop)   WITH_DESKTOP=1 ;;
        --system)    SYSTEM_WIDE=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help)
            sed -n '2,12p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            printf 'Unknown option: %s\n' "$arg" >&2
            exit 2
            ;;
    esac
done

XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"

if [ "$SYSTEM_WIDE" -eq 1 ]; then
    INSTALL_DIR="/opt/${APP_NAME}"
    BIN_DIR="/usr/local/bin"
    DESKTOP_DIR="/usr/local/share/applications"
else
    INSTALL_DIR="${XDG_DATA_HOME}/${APP_NAME}"
    BIN_DIR="${HOME}/.local/bin"
    DESKTOP_DIR="${XDG_DATA_HOME}/applications"
fi

UNIT_FILE="${XDG_CONFIG_HOME}/systemd/user/${UNIT_NAME}"
DESKTOP_FILE="${DESKTOP_DIR}/${APP_NAME}.desktop"

if [ "$UNINSTALL" -eq 1 ]; then
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user disable --now "${UNIT_NAME}" >/dev/null 2>&1 || true
        rm -f "${UNIT_FILE}"
        systemctl --user daemon-reload >/dev/null 2>&1 || true
    else
        rm -f "${UNIT_FILE}"
    fi
    rm -f "${DESKTOP_FILE}" "${BIN_DIR}/jlu-drcom-ng" "${BIN_DIR}/drcomd"
    rm -rf "${INSTALL_DIR}"
    printf 'Uninstalled JLU DrCOM NG from %s\n' "${INSTALL_DIR}"
    exit 0
fi

# Locate payload relative to this script (release tarball unpack or repo checkout)
if [ -x "${SCRIPT_DIR}/JLU-DrCOM-NG" ]; then
    MODE="bundle"
    EXEC_TARGET="${INSTALL_DIR}/JLU-DrCOM-NG"
    SERVICE_ARGS="--autostart --minimized"
elif [ -x "${SCRIPT_DIR}/drcomd" ]; then
    MODE="daemon"
    EXEC_TARGET="${INSTALL_DIR}/drcomd"
    SERVICE_ARGS="--config ${XDG_DATA_HOME}/JLU-DrCOM-NG/config.json"
else
    printf 'Error: neither JLU-DrCOM-NG nor drcomd found alongside install.sh\n' >&2
    exit 1
fi

mkdir -p "${INSTALL_DIR}" "${BIN_DIR}"
cp -af "${SCRIPT_DIR}/." "${INSTALL_DIR}/"

if [ "$MODE" = "bundle" ]; then
    chmod +x "${INSTALL_DIR}/JLU-DrCOM-NG"
    ln -sf "${INSTALL_DIR}/JLU-DrCOM-NG" "${BIN_DIR}/jlu-drcom-ng"
    if [ -f "${INSTALL_DIR}/drcomd" ]; then
        chmod +x "${INSTALL_DIR}/drcomd"
        ln -sf "${INSTALL_DIR}/drcomd" "${BIN_DIR}/drcomd"
    elif [ -f "${INSTALL_DIR}/_internal/drcomd" ]; then
        chmod +x "${INSTALL_DIR}/_internal/drcomd"
        ln -sf "${INSTALL_DIR}/_internal/drcomd" "${BIN_DIR}/drcomd"
    fi
    EXEC_START="${EXEC_TARGET} ${SERVICE_ARGS}"
else
    chmod +x "${INSTALL_DIR}/drcomd"
    ln -sf "${INSTALL_DIR}/drcomd" "${BIN_DIR}/drcomd"
    EXEC_START="${EXEC_TARGET} ${SERVICE_ARGS}"
fi

if [ "$WITH_DESKTOP" -eq 1 ] && [ "$MODE" = "bundle" ]; then
    mkdir -p "${DESKTOP_DIR}"
    cat > "${DESKTOP_FILE}" <<EOF
[Desktop Entry]
Type=Application
Name=JLU DrCOM NG
Comment=Jilin University Campus Network Client
Exec=${INSTALL_DIR}/JLU-DrCOM-NG
Terminal=false
Categories=Network;Utility;
StartupNotify=false
EOF
    printf 'Installed desktop entry: %s\n' "${DESKTOP_FILE}"
fi

if [ "$WITH_SYSTEMD" -eq 1 ]; then
    mkdir -p "$(dirname "${UNIT_FILE}")"
    if [ -f "${SCRIPT_DIR}/${UNIT_NAME}.in" ]; then
        sed -e "s|@WORKING_DIR@|${INSTALL_DIR}|g" \
            -e "s|@EXEC_START@|${EXEC_START}|g" \
            "${SCRIPT_DIR}/${UNIT_NAME}.in" > "${UNIT_FILE}"
    else
        cat > "${UNIT_FILE}" <<EOF
[Unit]
Description=JLU DrCOM NG Campus Network Client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${EXEC_START}
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
EOF
    fi
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user daemon-reload
        systemctl --user enable --now "${UNIT_NAME}" || \
            printf 'Warning: systemctl --user enable failed (no user session?)\n' >&2
    fi
    printf 'Installed systemd user unit: %s\n' "${UNIT_FILE}"
fi

printf 'Installed JLU DrCOM NG (%s) to %s\n' "${MODE}" "${INSTALL_DIR}"
printf 'Binary symlink(s) placed in %s\n' "${BIN_DIR}"
