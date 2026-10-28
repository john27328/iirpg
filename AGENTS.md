# Repository Guidelines

## Project Scope & Current State

This text RPG uses open Pathfinder rules and a Python backend. A chat and JSON test stub share one persistent queue. All code and distributable content will be public; do not split public and private repositories or components.

## Project Structure & Module Organization

- `src/iirpg/`: Python package; `domain.py` defines messages and visibility, `application.py` filters subscriptions, `storage.py` persists JSON envelopes, and `server.py` handles HTTP.
- `src/iirpg/web/`: browser interface, with one `Chat` and a raw JSON `JsonStub`.
- `tests/`: fast tests; `release_tests/`: separately invoked release checks.
- `dice.py`: generic dice; `rules/dice_systems.json`: formats. `docs/`: contracts; `data/pathfinder2e-remaster/`: attributed ORC rules reference.

Keep game mechanics independent of HTTP handlers and storage. Add directories when they contain useful files.

## Build, Test, and Development Commands

Python 3.11+; runtime and tests use the standard library. No frontend build is needed.

```bash
PYTHONPATH=src python3 -m iirpg --host 127.0.0.1 --port 8000
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m unittest discover -s release_tests -v
```

Commands start the server, run fast tests, and run release checks. SQLite history defaults to `.local/chat.sqlite3`. Declare dependencies in `pyproject.toml`; update `README.md` commands.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions/modules, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Type public interfaces. No formatter or linter is configured.

## Testing Guidelines

Use `unittest`, `test_*.py`, and `test_<behavior>` methods. Cover non-AI logic with focused behavior and regression tests: visibility, cursors, persistence, migrations, isolation, retries, and validation. Keep the default suite within a few seconds; no coverage quota or tests mirroring implementation. Use temporary data and deterministic dice; cite rules sources. Never call external AI APIs in these suites; stub AI boundaries. Run fast tests for relevant changes. Put slower process, browser, load, and deployment checks in `release_tests/`; run applicable checks before releases. Keep live AI evaluations separately opt-in.

## Commit & Pull Request Guidelines

Use imperative subjects, following `Add repository contributor guidelines`. Keep changes focused. PRs describe behavior, linked issues, verification, rules assumptions, and configuration changes.

## Rules Licensing & Configuration

Use Pathfinder content permitted by its source license: ORC or OGL. Record edition and source; distribute required licenses, notices, and attribution. Do not relicense third-party rules under the code license. Exclude Paizo Reserved Material, Product Identity, and trademarks unless separately permitted. Follow [Paizo's licensing guidance](https://paizo.com/licenses).

Keep secrets, local saves, and `.venv/` out of version control; provide placeholder configuration examples instead.
