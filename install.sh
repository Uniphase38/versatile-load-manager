#!/bin/sh
set -eu

DEFAULT_TARGET_DIR=/data/versatile-load-manager
DEFAULT_SERVICE_LINK=/service/versatile-load-manager
TARGET_DIR=${VLM_TARGET_DIR:-$DEFAULT_TARGET_DIR}
SERVICE_LINK=${VLM_SERVICE_LINK:-$DEFAULT_SERVICE_LINK}
COMMAND_LINK=${VLM_COMMAND_LINK:-/data/vlm}
LOG_DIR=${VLM_LOG_DIR:-/data/log/versatile-load-manager}
UPDATE_RC_LOCAL=${VLM_UPDATE_RC_LOCAL:-1}
LEGACY_TARGET_DIR=${VLM_LEGACY_TARGET_DIR:-/data/victron-load-manager}
LEGACY_SERVICE_LINK=${VLM_LEGACY_SERVICE_LINK:-/service/victron-load-manager}
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENDOR_SOURCE=${VLM_VENDOR_SOURCE:-$SCRIPT_DIR/vendor}

if [ -n "${VLM_MIGRATE_LEGACY+x}" ]; then
    MIGRATE_LEGACY=$VLM_MIGRATE_LEGACY
elif [ "$TARGET_DIR" = "$DEFAULT_TARGET_DIR" ] && [ "$SERVICE_LINK" = "$DEFAULT_SERVICE_LINK" ]; then
    MIGRATE_LEGACY=1
else
    MIGRATE_LEGACY=0
fi

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
require_absolute_safe_path VLM_LOG_DIR "$LOG_DIR"
require_absolute_safe_path VLM_LEGACY_TARGET_DIR "$LEGACY_TARGET_DIR"
require_absolute_safe_path VLM_LEGACY_SERVICE_LINK "$LEGACY_SERVICE_LINK"
case "$UPDATE_RC_LOCAL" in
    0|1) ;;
    *) echo "ERROR: VLM_UPDATE_RC_LOCAL must be 0 or 1" >&2; exit 1 ;;
esac
case "$MIGRATE_LEGACY" in
    0|1) ;;
    *) echo "ERROR: VLM_MIGRATE_LEGACY must be 0 or 1" >&2; exit 1 ;;
esac

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: install.sh must run as root" >&2
    exit 1
fi

echo "Installing Versatile Load Manager in $TARGET_DIR"

# Stop existing current and legacy instances before touching their files.
if [ -L "$SERVICE_LINK" ]; then
    svc -d "$SERVICE_LINK" || true
fi
if [ "$MIGRATE_LEGACY" -eq 1 ] && [ "$LEGACY_SERVICE_LINK" != "$SERVICE_LINK" ] && [ -L "$LEGACY_SERVICE_LINK" ]; then
    svc -d "$LEGACY_SERVICE_LINK" || true
fi

mkdir -p \
    "$TARGET_DIR/src" \
    "$TARGET_DIR/vendor" \
    "$TARGET_DIR/service/log" \
    "$LOG_DIR" \
    "$(dirname "$SERVICE_LINK")" \
    "$(dirname "$COMMAND_LINK")"
cp -R "$SCRIPT_DIR/src/." "$TARGET_DIR/src/"
cp "$SCRIPT_DIR/venus/service/run" "$TARGET_DIR/service/run"
cp "$SCRIPT_DIR/venus/service/log/run" "$TARGET_DIR/service/log/run"
cp "$SCRIPT_DIR/vlm" "$TARGET_DIR/vlm"
printf '%s\n' "$LOG_DIR" > "$TARGET_DIR/log-dir"
chmod 755 \
    "$TARGET_DIR/service/run" \
    "$TARGET_DIR/service/log/run" \
    "$TARGET_DIR/vlm"

# A fresh installation is deliberately left down until explicitly enabled.
: > "$TARGET_DIR/service/down"

if [ -d "$VENDOR_SOURCE" ]; then
    cp -R "$VENDOR_SOURCE/." "$TARGET_DIR/vendor/"
fi

if [ "$MIGRATE_LEGACY" -eq 1 ] && [ ! -f "$TARGET_DIR/config.yaml" ] && [ -f "$LEGACY_TARGET_DIR/config.yaml" ]; then
    cp "$LEGACY_TARGET_DIR/config.yaml" "$TARGET_DIR/config.yaml"
    sed -i \
        -e "s#$LEGACY_TARGET_DIR#$TARGET_DIR#g" \
        -e 's#/var/run/victron-load-manager.lock#/var/run/versatile-load-manager.lock#g' \
        -e 's#/var/lib/victron-load-manager#/data/versatile-load-manager#g' \
        "$TARGET_DIR/config.yaml"
    chmod 600 "$TARGET_DIR/config.yaml"
    echo "Migrated legacy configuration from $LEGACY_TARGET_DIR"
fi

if [ "$MIGRATE_LEGACY" -eq 1 ]; then
    for state_file in runtime.json tempo.json solar_forecast.json service_ids.json; do
        if [ ! -e "$TARGET_DIR/$state_file" ] && [ -f "$LEGACY_TARGET_DIR/$state_file" ]; then
            cp "$LEGACY_TARGET_DIR/$state_file" "$TARGET_DIR/$state_file"
            echo "Migrated legacy state: $state_file"
        fi
    done
fi

if [ ! -f "$TARGET_DIR/config.yaml" ]; then
    cp "$SCRIPT_DIR/config/config.example.yaml" "$TARGET_DIR/config.yaml"
    chmod 600 "$TARGET_DIR/config.yaml"
    echo "Created $TARGET_DIR/config.yaml (observation mode)"
else
    echo "Preserved existing $TARGET_DIR/config.yaml"
fi

PYTHONPATH="$TARGET_DIR/vendor:$TARGET_DIR/src" python3 -c \
    'import dbus, yaml, requests, pymodbus; import versatile_load_manager' || {
        echo "ERROR: Python dependencies are incomplete; service remains down" >&2
        exit 1
    }

ln -sfn "$TARGET_DIR/service" "$SERVICE_LINK"
ln -sfn "$TARGET_DIR/vlm" "$COMMAND_LINK"

# Retire only the known legacy service symlink. The old installation directory
# is deliberately retained as a rollback copy.
if [ "$MIGRATE_LEGACY" -eq 1 ] && [ "$LEGACY_SERVICE_LINK" != "$SERVICE_LINK" ] && [ -L "$LEGACY_SERVICE_LINK" ]; then
    legacy_link_target=$(readlink "$LEGACY_SERVICE_LINK" || true)
    if [ "$legacy_link_target" = "$LEGACY_TARGET_DIR/service" ]; then
        rm "$LEGACY_SERVICE_LINK"
        echo "Removed legacy service link; preserved $LEGACY_TARGET_DIR"
    fi
fi

if [ "$UPDATE_RC_LOCAL" -eq 1 ]; then
    RC_LOCAL=/data/rc.local
    if [ ! -f "$RC_LOCAL" ]; then
        printf '%s\n' '#!/bin/sh' > "$RC_LOCAL"
    fi
    case "$(sed -n '1p' "$RC_LOCAL")" in
        '#!'*) ;;
        *) echo "WARNING: $RC_LOCAL has no valid shebang; correct its first line" >&2 ;;
    esac
    # Remove only lines created by releases using the former technical name.
    if [ "$MIGRATE_LEGACY" -eq 1 ]; then
        sed -i \
            -e '\|^# Victron Load Manager service (managed by install.sh)$|d' \
            -e "\|^ln -sfn $LEGACY_TARGET_DIR/service $LEGACY_SERVICE_LINK$|d" \
            -e "\|^ln -sfn $LEGACY_TARGET_DIR/vlm $COMMAND_LINK$|d" \
            "$RC_LOCAL"
    fi
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
