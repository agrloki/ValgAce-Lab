#!/bin/sh

# Версия скрипта
VERSION="1.2"

# Определение архитектуры
IS_MIPS=0
if echo "$(uname -m)" | grep -q "mips"; then
   IS_MIPS=1
fi

# Пути по умолчанию
KLIPPER_HOME="${HOME}/klipper"
KLIPPER_CONFIG_HOME="${HOME}/printer_data/config"
MOONRAKER_CONFIG_DIR="${HOME}/printer_data/config"
SRCDIR="$PWD"
KLIPPER_ENV="${HOME}/klippy-env/bin"

# Для MIPS систем
if [ "$IS_MIPS" -eq 1 ]; then
    KLIPPER_HOME="/usr/share/klipper"
    KLIPPER_CONFIG_HOME="/usr/data/printer_data/config"
    MOONRAKER_CONFIG_DIR="/usr/data/printer_data/config"
    KLIPPER_ENV="/usr/bin"
fi

# Имена сервисов
KLIPPER_SERVICE="klipper"
MOONRAKER_SERVICE="moonraker"

usage() { 
    echo "Usage: $0 [-u] [-h] [-v]" 1>&2
    echo "Options:" 1>&2
    echo "  -u    Uninstall ValgACE" 1>&2
    echo "  -h    Show this help" 1>&2
    echo "  -v    Show version" 1>&2
    exit 1
}

show_version() {
    echo "ValgACE installer v${VERSION}"
    exit 0
}

# Парсинг аргументов
UNINSTALL=0
while getopts "uhv" arg; do
   case $arg in
       u) UNINSTALL=1;;
       h) usage;;
       v) show_version;;
       *) usage;;
   esac
done

verify_ready() {
  if [ "$IS_MIPS" -ne 1 ]; then
    if [ "$EUID" -eq 0 ]; then
        echo "[ERROR] This script must not run as root. Exiting."
        exit 1
    fi
  else
    echo "[WARNING] Running on MIPS system - root privileges expected"
  fi
}

check_service() {
    local service=$1
    if ! sudo systemctl is-enabled --quiet "$service" 2>/dev/null; then
        echo "[ERROR] Service $service not found or not enabled"
        return 1
    fi
    return 0
}

check_folders() {
    local missing=0
    
    if [ ! -d "$KLIPPER_HOME/klippy/extras/" ]; then
        echo "[ERROR] Klipper installation not found in $KLIPPER_HOME"
        missing=1
    fi

    if [ ! -d "${KLIPPER_CONFIG_HOME}/" ]; then
        echo "[ERROR] Config directory not found: $KLIPPER_CONFIG_HOME"
        missing=1
    fi

    if [ ! -f "${MOONRAKER_CONFIG_DIR}/moonraker.conf" ]; then
        echo "[ERROR] moonraker.conf not found in $MOONRAKER_CONFIG_DIR"
        missing=1
    fi

    if [ ! -d "${KLIPPER_ENV}" ]; then
        echo "[ERROR] Klipper env directory not found: $KLIPPER_ENV"
        missing=1
    fi

    if [ $missing -ne 0 ]; then
        exit 1
    fi

    echo "[OK] All required directories and files found"
}

link_extension() {
    if [ ! -f "${SRCDIR}/extras/ace.py" ]; then
        echo "[ERROR] Source file ${SRCDIR}/extras/ace.py not found"
        exit 1
    fi

    echo -n "Linking extension to Klipper... "
    if ln -sf "${SRCDIR}/extras/ace.py" "${KLIPPER_HOME}/klippy/extras/ace.py"; then
        echo "[OK]"
    else
        echo "[FAILED]"
        exit 1
    fi
}

copy_config() {
    echo -n "Copying config file... "
    if [ ! -f "${KLIPPER_CONFIG_HOME}/ace.cfg" ]; then
        if cp "${SRCDIR}/ace.cfg" "${KLIPPER_CONFIG_HOME}/"; then
            echo "[OK]"
        else
            echo "[FAILED]"
            exit 1
        fi
    else
        echo "[SKIPPED] (already exists)"
    fi
}

install_requirements() {
    echo -n "Installing requirements... "
    if [ ! -f "${SRCDIR}/requirements.txt" ]; then
        echo "[SKIPPED] (requirements.txt not found)"
        return
    fi

    if "${KLIPPER_ENV}/pip3" install -r "${SRCDIR}/requirements.txt"; then
        echo "[OK]"
    else
        echo "[FAILED]"
        exit 1
    fi
}

uninstall() {
    echo -n "Uninstalling ValgACE... "
    if [ -f "${KLIPPER_HOME}/klippy/extras/ace.py" ]; then
        if rm -f "${KLIPPER_HOME}/klippy/extras/ace.py"; then
            echo "[OK]"
            echo "Note: You need to manually remove:"
            echo "1. [update_manager ValgACE] section from moonraker.conf"
            echo "2. All ValgACE-related configurations from your printer.cfg"
        else
            echo "[FAILED]"
            exit 1
        fi
    else
        echo "[SKIPPED] (ace.py not found)"
    fi
}

restart_service() {
    local service=$1
    echo -n "Restarting $service... "
    if sudo systemctl restart "$service"; then
        echo "[OK]"
    else
        echo "[FAILED]"
        exit 1
    fi
}

stop_service() {
    local service=$1
    echo -n "Stopping $service... "
    if sudo systemctl stop "$service"; then
        echo "[OK]"
    else
        echo "[FAILED]"
        exit 1
    fi
}

start_service() {
    local service=$1
    echo -n "Starting $service... "
    if sudo systemctl start "$service"; then
        echo "[OK]"
    else
        echo "[FAILED]"
        exit 1
    fi
}

add_updater() {
    echo -n "Adding update manager to moonraker.conf... "
    if grep -q "\[update_manager ValgACE\]" "${MOONRAKER_CONFIG_DIR}/moonraker.conf"; then
        echo "[SKIPPED] (already exists)"
        return
    fi

    cat << EOF >> "${MOONRAKER_CONFIG_DIR}/moonraker.conf"

[update_manager ValgACE]
type: git_repo
path: ${SRCDIR}
primary_branch: main
origin: https://github.com/agrloki/ValgAce-Lab.git
managed_services: klipper
EOF

    echo "[OK]"
}

# Основной процесс
verify_ready
check_folders
check_service "$KLIPPER_SERVICE" || exit 1
check_service "$MOONRAKER_SERVICE" || exit 1

stop_service "$KLIPPER_SERVICE"

if [ "$UNINSTALL" -eq 1 ]; then
    uninstall
else
    install_requirements
    link_extension
    copy_config
    add_updater
    restart_service "$MOONRAKER_SERVICE"
fi

start_service "$KLIPPER_SERVICE"

echo "Operation completed successfully"
exit 0