# Contributing

## Development setup

```bash
git clone <this repo's URL>
cd <repo>/sdk

# Install all packages in editable mode
pip install -e "packages/acp-gateway[all]"
pip install -e "packages/acp-tracing[all]"
pip install -e "packages/acp-governance"
pip install -e "packages/acp-signals"
pip install -e "packages/acp-intelligence"
pip install -e "packages/acp-sdk"

# Install test dependencies
pip install pytest pytest-cov
```

## Running tests

```bash
# All packages
pytest packages/*/tests/ -v

# Single package
pytest packages/acp-gateway/tests/ -v
```

## Package structure

Each package follows this layout:

```
packages/<package-name>/
  <module_name>/
    __init__.py      — public API and __version__
    client.py        — main implementation
  tests/
    test_client.py
  pyproject.toml
  README.md
```

## Adding a new feature

1. Implement in the relevant `client.py`
2. Export from `__init__.py` if it's a new public class
3. Add tests in `tests/test_client.py` — use `unittest.mock` to avoid live network calls
4. Update the package `README.md` with usage examples
5. Add a CHANGELOG entry under `[Unreleased]`

## Versioning

All packages share the same version number. When releasing:
1. Update `__version__` in each `__init__.py`
2. Update all `pyproject.toml` `version` fields
3. Update `acp-sdk`'s `pyproject.toml` dependency pins for the four individual packages
4. Move `[Unreleased]` entries to a dated release in `CHANGELOG.md`
5. Create a GitHub release — the publish workflow runs automatically

## Code style

- No comments unless the "why" is non-obvious
- Type hints on all public methods
- `httpx` for all HTTP calls (no `requests`)
- Network errors return dicts with `"error"` key — never raise in public methods
- Tracing errors must be silently swallowed — tracing must never crash a host app
