# Repository Guidelines

## Project Scope & Current State

This repository will host a text RPG using open Pathfinder rules, with a Python backend. All project code and distributable game content will be public; do not split the project into public and private repositories or components. It currently has no application code, dependency manifest, tests, or commit history. Framework and tooling choices remain open.

## Project Structure & Module Organization

- `src/iirpg/`: Python backend package, organized into modules and layers for game rules, narrative/session state, persistence, and transport interfaces.
- `tests/`: automated tests, organized to mirror the source modules.
- `data/`: structured rules content and game assets, with source attribution.
- `docs/`: architecture decisions, setup instructions, and rules interpretations.

Keep game mechanics independent of HTTP handlers and storage. Add directories when they contain useful files.

## Build, Test, and Development Commands

No build, server, lint, or test commands are configured yet. For an isolated local Python environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

When introducing dependencies, declare them in `pyproject.toml` and document installation, server startup, and test commands. Do not assume a web framework, package manager, or database has been selected.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` for functions and modules, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants. Add type annotations to public interfaces. No formatter or linter is configured; document selected tools when introducing them.

## Testing Guidelines

No testing framework or coverage threshold exists yet. Prefer `pytest` for initial tests, using `test_*.py` files and `test_<behavior>` functions. Cover rule boundaries, invalid actions, and session transitions. Inject dice randomness so tests are deterministic. Document the Pathfinder edition and rule source behind mechanics assertions.

## Commit & Pull Request Guidelines

There is no existing commit convention. Use concise, imperative subjects, such as `Add deterministic skill checks`. Keep changes focused. Pull requests should describe behavior, link relevant issues, state verification performed, and identify rules assumptions or configuration changes.

## Rules Licensing & Configuration

Use only Pathfinder content permitted by its source license: ORC or OGL, as applicable. Record the edition and source; include required license texts, notices, and attribution when distributing content or adaptations. Do not relicense third-party rules under the project's code license. Exclude Paizo Reserved Material, Product Identity, and trademarks unless separately permitted. Follow [Paizo's licensing guidance](https://paizo.com/licenses).

Keep secrets, local saves, and `.venv/` out of version control; provide placeholder configuration examples instead.
