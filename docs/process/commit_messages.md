# Commit Messages for Organization Transfer

## modelops (main repository)
```
refactor: update all references for org transfer to IDM

- Update GitHub repository URLs from vsbuffalo to institutefordiseasemodeling
- Update Docker image references to use IDM's GHCR namespace
- Update CI/CD workflows to push to IDM registry
- Update documentation links for issues and discussions
- Maintain vsbuffalo/oras-py fork reference (pending upstream PR)

Files updated (19):
- Core: pyproject.toml, install.sh
- CI/CD: .github/workflows/docker-build.yml
- Docker: modelops-images.yaml, Dockerfile.{worker,scheduler,runner}
- Config: examples/workspace.yaml, src/modelops/cli/{dev,templates}.py, src/modelops/core/unified_config.py
- Docs: README.md, docs/setup/quick_start.md, docs/dev/readme.md
- Examples: examples/simulation-workflow/pyproject.toml

Part of repository transfer to Institute for Disease Modeling organization.
```

## modelops-bundle
```
refactor: update repository references for IDM org transfer

- Update GitHub URLs from vsbuffalo to institutefordiseasemodeling
- Update test badge and documentation links
- Update default dependencies in project templates
- Keep vsbuffalo/oras-py reference (fork pending upstream PR)

Files updated (4):
- README.md: badges, installation, clone URLs
- pyproject.toml: modelops-contracts dependency
- src/modelops_bundle/templates.py: default project dependencies

Part of repository transfer to Institute for Disease Modeling organization.
```

## modelops-calabaria
```
refactor: update repository references for IDM org transfer

- Update GitHub URLs from vsbuffalo to institutefordiseasemodeling
- Update test badge and documentation links
- Update related project references in README
- Update modelops-contracts dependency

Files updated (2):
- README.md: badges, installation, related projects (5 references)
- pyproject.toml: modelops-contracts dependency

Part of repository transfer to Institute for Disease Modeling organization.
```

## modelops-contracts
```
No commit needed - repository is clean of hardcoded references.
Only .git/config will update automatically upon transfer.
```
