# starsim-sir

A ModelOps bundle project.

## Quick Start

```bash
# Add files to track
mops-bundle add .

# Generate manifest
mops-bundle manifest

# Push to registry
mops-bundle push
```

## Project Structure

```
starsim-sir/
├── pyproject.toml    # Project configuration
├── README.md         # This file
└── .modelopsignore   # Patterns to exclude from bundle
```

## Next Steps

1. Add your model files with `mops-bundle add <files>`
2. Create a manifest with `mops-bundle manifest`
3. Push to your registry with `mops-bundle push`
