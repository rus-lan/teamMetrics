#!/usr/bin/env bash
# Build the source release bundle for team-metrics into dist/release/:
#   team-metrics.tar.gz  - runtime files only (what the skill needs to run)
#   install.sh                  - copied in unmodified, downloaded by curl one-liner
#   checksums-sha256.txt        - sha256 over the two files above
#
# Usage: scripts/release.sh [VERSION]
#   VERSION defaults to the contents of ./VERSION. If given, it must match
#   ./VERSION exactly -- this is the single source of truth for the project
#   version, and a mismatch (e.g. forgetting to bump VERSION before tagging)
#   hard-fails instead of silently releasing the wrong number.
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION="${1:-}"
PKG_VERSION="$(tr -d '[:space:]' < VERSION)"
if [ -z "$VERSION" ]; then
  VERSION="$PKG_VERSION"
elif [ "$VERSION" != "$PKG_VERSION" ]; then
  echo "VERSION argument ($VERSION) does not match the VERSION file ($PKG_VERSION)." >&2
  echo "Bump the VERSION file to $VERSION first, or pass no argument to release the current version." >&2
  exit 1
fi
TAG="v${VERSION}"

SKILL_VERSION="$(sed -n 's/^version: *//p' SKILL.md | head -1 | tr -d '[:space:]')"
if [ "$SKILL_VERSION" != "$PKG_VERSION" ]; then
  echo "SKILL.md frontmatter version ($SKILL_VERSION) does not match the VERSION file ($PKG_VERSION)." >&2
  echo "Update SKILL.md's 'version:' field to $PKG_VERSION first." >&2
  exit 1
fi

SKILL_NAME="team-metrics"
OUT_DIR="dist/release"
STAGE_DIR="dist/stage/${SKILL_NAME}"
TARBALL_NAME="${SKILL_NAME}.tar.gz"
INSTALL_SCRIPT="install.sh"

# Runtime bundle -- must match install.sh's BUNDLE_ITEMS exactly, since the
# same directory is what LOCAL install.sh copies from a checkout and what
# REMOTE install.sh extracts and installs from. tests/, demo/, .research/,
# and screenshots/ are deliberately left out: nothing at runtime reads them.
BUNDLE_ITEMS="SKILL.md README.md scripts templates .team-metrics.example.json VERSION"

if [ ! -f "$INSTALL_SCRIPT" ]; then
  echo "install script not found: $INSTALL_SCRIPT" >&2
  exit 1
fi

rm -rf "dist/stage" "$OUT_DIR"
mkdir -p "$STAGE_DIR" "$OUT_DIR"

echo "Staging ${SKILL_NAME} v${VERSION}..."
for item in $BUNDLE_ITEMS; do
  if [ ! -e "$item" ]; then
    echo "missing bundle item: $item" >&2
    exit 1
  fi
  cp -R "$item" "$STAGE_DIR/$item"
done

find "$STAGE_DIR" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find "$STAGE_DIR" -type f -name '*.py[co]' -delete 2>/dev/null || true
# scripts/ is bundled whole for team-metrics + team_metrics/, but this
# release script itself is a build-time tool, not something the skill needs
# at runtime -- drop it from the staged copy.
rm -f "$STAGE_DIR/scripts/release.sh"
chmod +x "$STAGE_DIR/scripts/team-metrics"

echo "Packing $TARBALL_NAME..."
( cd dist/stage && tar czf "$OLDPWD/$OUT_DIR/$TARBALL_NAME" "$SKILL_NAME" )
rm -rf dist/stage

echo "Copying $INSTALL_SCRIPT..."
cp "$INSTALL_SCRIPT" "$OUT_DIR/install.sh"

echo "Generating SHA256 checksums..."
( cd "$OUT_DIR" && sha256sum -- * > checksums-sha256.txt )

echo ""
echo "Built release ${TAG} in $OUT_DIR:"
ls -la "$OUT_DIR"
