BUNDLE_DIR := $(CURDIR)
PACKAGE_DIR := $(BUNDLE_DIR)/build

.PHONY: package clean

package:
	uv run mops bundle package --bundle-path $(BUNDLE_DIR) --output-dir $(PACKAGE_DIR)

clean:
	rm -rf $(PACKAGE_DIR) $(BUNDLE_DIR)/test_bundle.egg-info
