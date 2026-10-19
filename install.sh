#!/bin/sh
set -eu

TARGET_DIR=${VLM_TARGET_DIR:-/data/victron-load-manager}
SERVICE_LINK=${VLM_SERVICE_LINK:-/service/victron-load-manager}
COMMAND_LINK=${VLM_COMMAND_LINK:-/data/vlm}
UPDATE_RC_LOCAL=${VLM_UPDATE_RC_LOCAL:-1}
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENDOR_SOURCE=${VLM_VENDOR_SOURCE:-$SCRIPT_DIR/vendor}

require_absolute_safe_path() {
    name=$1
    value=$2
    case "$value" in
        /*) ;;
        *) echo "ERROR: $name must be an absolute path: $value" >&2; exit 1 ;;
    esac
    case "$value" in
        /|/data|/service|/usr|/usr/local|/usr/local/bin)
            echo "ERROR: refusing unsafe $name: $value" >&2
            exit 1
            ;;
    esac
}

require_absolute_safe_path VLM_TARGET_DIR "$TARGET_DIR"
require_absolute_safe_path VLM_SERVICE_LINK "$SERVICE_LINK"
require_absolute_safe_path VLM_COMMAND_LINK "$COMMAND_LINK"
case "$UPDATE_RC_LOCAL" in
    0|1) ;;
    *) echo "ERROR: VLM_UPDATE_RC_LOCAL must be 0 or 1" >&2; exit 1 ;;
esac

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: install.sh must run as root" >&2
    exit 1
fi

echo "Installing Versatile Load Manager in $TARGET_DIR"

# Stop an existing instance before replacing its source files.
if [ -L "$SERVICE_LINK" ]; then
    svc -d "$SERVICE_LINK" || true
fi

mkdir -p \
    "$TARGET_DIR/src" \
    "$TARGET_DIR/vendor" \
    "$TARGET_DIR/logs" \
    "$TARGET_DIR/service/log" \
    "$(dirname "$COMMAND_LINK")"
cp -R "$SCRIPT_DIR/src/." "$TARGET_DIR/src/"
cp "$SCRIPT_DIR/venus/service/run" "$TARGET_DIR/service/run"
cp "$SCRIPT_DIR/venus/service/log/run" "$TARGET_DIR/service/log/run"
cp "$SCRIPT_DIR/vlm" "$TARGET_DIR/vlm"
chmod 755 \
    "$TARGET_DIR/service/run" \
    "$TARGET_DIR/service/log/run" \
    "$TARGET_DIR/vlm"

# A fresh installation is deliberately left down until explicitly enabled.
: > "$TARGET_DIR/service/down"

if [ -d "$VENDOR_SOURCE" ]; then
    cp -R "$VENDOR_SOURCE/." "$TARGET_DIR/vendor/"
fi

if [ ! -f "$TARGET_DIR/config.yaml" ]; then
    cp "$SCRIPT_DIR/config/config.example.yaml" "$TARGET_DIR/config.yaml"
    chmod 600 "$TARGET_DIR/config.yaml"
    echo "Created $TARGET_DIR/config.yaml (observation mode)"
else
    echo "Preserved existing $TARGET_DIR/config.yaml"
fi

PYTHONPATH="$TARGET_DIR/vendor:$TARGET_DIR/src" python3 -c \
    'import dbus, yaml, requests, pymodbus; import victron_load_manager' || {
        echo "ERROR: Python dependencies are incomplete; service remains down" >&2
        exit 1
    }

ln -sfn "$TARGET_DIR/service" "$SERVICE_LINK"
ln -sfn "$TARGET_DIR/vlm" "$COMMAND_LINK"

if [ "$UPDATE_RC_LOCAL" -eq 1 ]; then
    RC_LOCAL=/data/rc.local
    if [ ! -f "$RC_LOCAL" ]; then
        printf '%s\n' '#!/bin/sh' > "$RC_LOCAL"
    fi
    case "$(sed -n '1p' "$RC_LOCAL")" in
        '#!'*) ;;
        *) echo "WARNING: $RC_LOCAL has no valid shebang; correct its first line" >&2 ;;
    esac
    if ! grep -Fqx "ln -sfn $TARGET_DIR/service $SERVICE_LINK" "$RC_LOCAL"; then
        printf '%s\n' \
            '' \
            '# Versatile Load Manager service (managed by install.sh)' \
            "ln -sfn $TARGET_DIR/service $SERVICE_LINK" >> "$RC_LOCAL"
    fi
    if ! grep -Fqx "ln -sfn $TARGET_DIR/vlm $COMMAND_LINK" "$RC_LOCAL"; then
        printf '%s\n' \
            "mkdir -p $(dirname "$COMMAND_LINK")" \
            "ln -sfn $TARGET_DIR/vlm $COMMAND_LINK" >> "$RC_LOCAL"
    fi
    chmod 755 "$RC_LOCAL"
else
    echo "Skipped /data/rc.local update (test installation)"
fi

echo "Installation complete; service is DOWN"
echo "Edit $TARGET_DIR/config.yaml, then enable with: $COMMAND_LINK enable"
echo "Administrative command installed: $COMMAND_LINK --help"
