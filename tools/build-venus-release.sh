#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
VENDOR_DIR=
OUTPUT_DIR=$PROJECT_DIR/dist

usage() {
    cat <<'EOF'
Build an offline Versatile Load Manager package for Venus OS.

Usage:
  tools/build-venus-release.sh --vendor DIR [--output DIR]

The vendor directory must already contain the pure-Python dependencies checked
by tools/validate_vendor.py. The command creates a .tar.gz archive and matching
.sha256 file. It does not download dependencies.

Build that directory first with:
  python3 tools/build_vendor.py
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --vendor)
            [ "$#" -ge 2 ] || { usage >&2; exit 2; }
            VENDOR_DIR=$2
            shift 2
            ;;
        --output)
            [ "$#" -ge 2 ] || { usage >&2; exit 2; }
            OUTPUT_DIR=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

[ -n "$VENDOR_DIR" ] || { echo "ERROR: --vendor is required" >&2; exit 2; }
case "$OUTPUT_DIR" in
    /*) ;;
    *) OUTPUT_DIR=$PWD/$OUTPUT_DIR ;;
esac

python3 "$SCRIPT_DIR/validate_vendor.py" "$VENDOR_DIR"

VERSION=$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' \
    "$PROJECT_DIR/src/victron_load_manager/__init__.py")
[ -n "$VERSION" ] || { echo "ERROR: unable to read package version" >&2; exit 1; }

PACKAGE_NAME=victron-load-manager-$VERSION-venus
BUILD_DIR=$(mktemp -d "${TMPDIR:-/tmp}/vlm-release.XXXXXX")
trap 'rm -rf "$BUILD_DIR"' EXIT HUP INT TERM
STAGE=$BUILD_DIR/$PACKAGE_NAME

mkdir -p "$STAGE" "$OUTPUT_DIR"
cp -R \
    "$PROJECT_DIR/config" \
    "$PROJECT_DIR/docs" \
    "$PROJECT_DIR/src" \
    "$PROJECT_DIR/venus" \
    "$STAGE/"
cp \
    "$PROJECT_DIR/README.md" \
    "$PROJECT_DIR/LICENSE" \
    "$PROJECT_DIR/NOTICE" \
    "$PROJECT_DIR/install.sh" \
    "$PROJECT_DIR/requirements.txt" \
    "$PROJECT_DIR/vlm" \
    "$STAGE/"
cp -R "$VENDOR_DIR" "$STAGE/vendor"

find "$STAGE" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$STAGE" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
chmod 755 \
    "$STAGE/install.sh" \
    "$STAGE/vlm" \
    "$STAGE/venus/service/run" \
    "$STAGE/venus/service/log/run"

ARCHIVE=$OUTPUT_DIR/$PACKAGE_NAME.tar.gz
CHECKSUM=$ARCHIVE.sha256
tar -C "$BUILD_DIR" -czf "$ARCHIVE" "$PACKAGE_NAME"
(
    cd "$OUTPUT_DIR"
    sha256sum "$(basename "$ARCHIVE")" > "$(basename "$CHECKSUM")"
)

echo "Release package: $ARCHIVE"
echo "Checksum:       $CHECKSUM"
