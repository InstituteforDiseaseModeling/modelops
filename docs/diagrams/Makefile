# Makefile for D2 Diagram Generation
# Converts D2 diagram files to PNG and PDF formats

# D2 command configuration
D2 := d2
THEME := 0  # Neutral theme
LAYOUT := elk  # Layout engine (dagre, elk, or others)
DPI := 300  # DPI for PNG output
D2_FLAGS := --theme=$(THEME) --layout=$(LAYOUT)

# Find all D2 files
D2_FILES := $(wildcard *.d2)
PNG_FILES := $(D2_FILES:.d2=.png)
PDF_FILES := $(D2_FILES:.d2=.pdf)
SVG_FILES := $(D2_FILES:.d2=.svg)

# Default target
.DEFAULT_GOAL := help

# Phony targets
.PHONY: all all-png all-pdf all-svg clean clean-png clean-pdf clean-svg help watch

# Build all formats
all: all-png all-pdf all-svg
	@echo "✅ Generated all diagram formats"

# Build all PNG files
all-png: $(PNG_FILES)
	@echo "✅ Generated all PNG diagrams"

# Build all PDF files  
all-pdf: $(PDF_FILES)
	@echo "✅ Generated all PDF diagrams"

# Build all SVG files
all-svg: $(SVG_FILES)
	@echo "✅ Generated all SVG diagrams"

# Pattern rule: D2 to PNG
%.png: %.d2
	@echo "🎨 Generating $@ from $<..."
	@$(D2) $(D2_FLAGS) --pad=20 $< $@

# Pattern rule: D2 to PDF
%.pdf: %.d2
	@echo "📄 Generating $@ from $<..."
	@$(D2) $(D2_FLAGS) --pad=20 $< $@

# Pattern rule: D2 to SVG
%.svg: %.d2
	@echo "🎭 Generating $@ from $<..."
	@$(D2) $(D2_FLAGS) --pad=20 $< $@

# Watch mode for development (opens browser with live reload)
watch:
	@echo "👁️  Starting watch mode for all diagrams..."
	@echo "Visit http://localhost:0 in your browser"
	@$(D2) $(D2_FLAGS) --watch overview.d2

# Watch specific diagram
watch-%: %.d2
	@echo "👁️  Watching $<..."
	@$(D2) $(D2_FLAGS) --watch $<

# Clean all generated files
clean: clean-png clean-pdf clean-svg
	@echo "🧹 Cleaned all generated files"

# Clean PNG files
clean-png:
	@rm -f $(PNG_FILES)
	@echo "🧹 Removed PNG files"

# Clean PDF files
clean-pdf:
	@rm -f $(PDF_FILES)
	@echo "🧹 Removed PDF files"

# Clean SVG files
clean-svg:
	@rm -f $(SVG_FILES)
	@echo "🧹 Removed SVG files"

# Help target
help:
	@echo "ModelOps D2 Diagram Generator"
	@echo "=============================="
	@echo ""
	@echo "Available targets:"
	@echo "  make all            - Generate all formats (PNG, PDF, SVG)"
	@echo "  make all-png        - Generate all PNG files"
	@echo "  make all-pdf        - Generate all PDF files"
	@echo "  make all-svg        - Generate all SVG files"
	@echo ""
	@echo "Individual diagrams:"
	@echo "  make overview.png   - Generate specific PNG"
	@echo "  make overview.pdf   - Generate specific PDF"
	@echo "  make overview.svg   - Generate specific SVG"
	@echo ""
	@echo "Watch mode (live reload):"
	@echo "  make watch          - Watch overview.d2 with live reload"
	@echo "  make watch-<name>   - Watch specific diagram (e.g., watch-pulumi-stacks)"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean          - Remove all generated files"
	@echo "  make clean-png      - Remove only PNG files"
	@echo "  make clean-pdf      - Remove only PDF files"
	@echo "  make clean-svg      - Remove only SVG files"
	@echo ""
	@echo "Configuration:"
	@echo "  THEME=$(THEME)     - D2 theme (0-300+)"
	@echo "  LAYOUT=$(LAYOUT)   - Layout engine (dagre, elk, etc.)"
	@echo "  DPI=$(DPI)         - DPI for PNG output"
	@echo ""
	@echo "Available D2 files:"
	@$(foreach file,$(D2_FILES),echo "  - $(file)";)

# List available diagrams
list:
	@echo "Available D2 diagrams:"
	@$(foreach file,$(D2_FILES),echo "  $(basename $(file))";)