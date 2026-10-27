#!/bin/bash
set -e

# Release script for octoprox
# Usage: ./scripts/release.sh [major|minor|patch]

VERSION_FILE="pyproject.toml"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

usage() {
    echo "Usage: $0 [major|minor|patch]"
    echo ""
    echo "  major  - Bump major version (1.0.0 -> 2.0.0)"
    echo "  minor  - Bump minor version (1.0.0 -> 1.1.0)"
    echo "  patch  - Bump patch version (1.0.0 -> 1.0.1)"
    exit 1
}

if [ $# -ne 1 ]; then
    usage
fi

BUMP_TYPE=$1

if [[ ! "$BUMP_TYPE" =~ ^(major|minor|patch)$ ]]; then
    echo -e "${RED}Error: Invalid bump type '$BUMP_TYPE'${NC}"
    usage
fi

# Check for uncommitted changes
if ! git diff-index --quiet HEAD --; then
    echo -e "${RED}Error: You have uncommitted changes. Please commit or stash them first.${NC}"
    exit 1
fi

# Check we're on main branch
CURRENT_BRANCH=$(git branch --show-current)
if [ "$CURRENT_BRANCH" != "main" ]; then
    echo -e "${YELLOW}Warning: You're on branch '$CURRENT_BRANCH', not 'main'.${NC}"
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Extract current version from pyproject.toml
CURRENT_VERSION=$(grep -E '^version = "' "$VERSION_FILE" | sed 's/version = "\(.*\)"/\1/')

if [ -z "$CURRENT_VERSION" ]; then
    echo -e "${RED}Error: Could not find version in $VERSION_FILE${NC}"
    exit 1
fi

echo -e "Current version: ${YELLOW}$CURRENT_VERSION${NC}"

# Parse version components
IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT_VERSION"

# Bump version based on type
case $BUMP_TYPE in
    major)
        MAJOR=$((MAJOR + 1))
        MINOR=0
        PATCH=0
        ;;
    minor)
        MINOR=$((MINOR + 1))
        PATCH=0
        ;;
    patch)
        PATCH=$((PATCH + 1))
        ;;
esac

NEW_VERSION="$MAJOR.$MINOR.$PATCH"
TAG_NAME="v$NEW_VERSION"

echo -e "New version: ${GREEN}$NEW_VERSION${NC}"

# Check if tag already exists
if git rev-parse "$TAG_NAME" >/dev/null 2>&1; then
    echo -e "${RED}Error: Tag $TAG_NAME already exists${NC}"
    exit 1
fi

# Confirm with user
echo ""
read -p "Create release $TAG_NAME? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
fi

# Release notes (docs/_data/releases.yml): a draft from the commits since the
# last tag, or from Claude, edited in $EDITOR, then added to the notes file.
# What's New in the app, the changelog page and the GitHub Release show it.
PYTHON=".venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"
LAST_TAG=$(git describe --tags --abbrev=0 --match 'v*' 2>/dev/null || git rev-list --max-parents=0 HEAD)
# A directory, so the draft keeps its .yml name for the editor (GNU mktemp needs X's in a file template)
NOTES_DIR=$(mktemp -d "${TMPDIR:-/tmp}/octoprox-release-notes.XXXXXX")
NOTES_DRAFT="$NOTES_DIR/release-notes.yml"
DRAFT_FLAGS=""
if command -v claude >/dev/null 2>&1; then
    read -p "Draft the release notes with Claude? (y/N) " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]] && DRAFT_FLAGS="--ai"
fi
"$PYTHON" scripts/release_notes.py draft "$NEW_VERSION" "$LAST_TAG" $DRAFT_FLAGS > "$NOTES_DRAFT"
while true; do
    ${VISUAL:-${EDITOR:-vi}} "$NOTES_DRAFT"
    if "$PYTHON" scripts/release_notes.py add "$NOTES_DRAFT" "$NEW_VERSION"; then
        break
    fi
    read -p "Edit the notes again? (Y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Nn]$ ]]; then
        echo "Aborted."
        exit 1
    fi
done
rm -rf "$NOTES_DIR"

# Update version in pyproject.toml
sed -i.bak "s/^version = \".*\"/version = \"$NEW_VERSION\"/" "$VERSION_FILE"
rm -f "$VERSION_FILE.bak"

# Commit the version bump with its release notes
git add "$VERSION_FILE" docs/_data/releases.yml
git commit -m "Bump version to $NEW_VERSION"

# Create tag
git tag -a "$TAG_NAME" -m "Release $TAG_NAME"

echo ""
echo -e "${GREEN}Created commit and tag $TAG_NAME${NC}"
echo ""
echo "To push the release, run:"
echo -e "  ${YELLOW}git push origin $CURRENT_BRANCH && git push origin $TAG_NAME${NC}"
echo ""
echo "Or to push everything at once:"
echo -e "  ${YELLOW}git push origin $CURRENT_BRANCH --tags${NC}"

