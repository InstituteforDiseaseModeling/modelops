#!/bin/bash
# Validate that no hardcoded image references exist outside of allowed files

set -e

echo "Checking for hardcoded image references..."

# Files that are allowed to have ghcr.io references
ALLOWED_FILES=(
    "modelops-images.yaml"
    "src/modelops/images.py"
    "tests/test_images.py"
    "docs/*"
    "*.md"
    "CLAUDE.md"
)

# Build the exclude pattern
EXCLUDE_PATTERN=""
for file in "${ALLOWED_FILES[@]}"; do
    EXCLUDE_PATTERN="$EXCLUDE_PATTERN --glob '!$file'"
done

# Search for hardcoded ghcr.io references
echo "Searching for ghcr.io references..."

# Use ripgrep if available, otherwise fall back to grep
if command -v rg &> /dev/null; then
    # Using ripgrep
    VIOLATIONS=$(rg -l "ghcr\.io/(vsbuffalo|institutefordiseasemodeling|modelops)/" \
        --glob '!modelops-images.yaml' \
        --glob '!src/modelops/images.py' \
        --glob '!tests/test_images.py' \
        --glob '!scripts/validate-no-hardcoded-images.sh' \
        --glob '!*.md' \
        --glob '!docs/**' \
        --glob '!.git/**' \
        --glob '!.github/workflows/docker-build.yml' \
        --glob '!build.log' \
        --glob '!*.yaml' \
        --glob '!examples/**' \
        2>/dev/null || true)
else
    # Fallback to grep
    VIOLATIONS=$(grep -r "ghcr\.io/\(vsbuffalo\|institutefordiseasemodeling\|modelops\)/" . \
        --exclude="modelops-images.yaml" \
        --exclude="images.py" \
        --exclude="test_images.py" \
        --exclude="validate-no-hardcoded-images.sh" \
        --exclude="*.md" \
        --exclude-dir=".git" \
        --exclude-dir="docs" \
        --exclude="docker-build.yml" \
        --exclude="build.log" \
        -l 2>/dev/null || true)
fi

if [ -n "$VIOLATIONS" ]; then
    echo "✗ Found hardcoded image references in the following files:"
    echo "$VIOLATIONS"
    echo ""
    echo "Please use the centralized image configuration instead:"
    echo "  - Import: from modelops.images import get_image_config"
    echo "  - Usage: config = get_image_config()"
    echo "  - Get image: config.scheduler_image()"
    echo ""
    echo "For Makefile, use: \$(shell uv run mops dev images print <key>)"
    exit 1
fi

echo "✓ No hardcoded image references found!"

# Additional check: ensure modelops-images.yaml exists
if [ ! -f "modelops-images.yaml" ]; then
    echo "✗ Error: modelops-images.yaml not found!"
    echo "This file is required for centralized image configuration."
    exit 1
fi

echo "✓ Image configuration validation passed!"