# Contributing

Thanks for helping. This project handles other people's credentials and business data,
so changes are held to a high bar for tests and security.

**Security issues:** do not open a public issue or pull request. Report them privately as
described in [SECURITY.md](SECURITY.md).

## Setup

You need Docker and Git. Python 3.15 and uv come from a pinned container, so no local
Python is required.

```sh
git clone https://github.com/meevagmbh/odoo-community-mcp.git
cd odoo-community-mcp
git config core.hooksPath .githooks   # pre-commit: format + lint, pre-push: all checks
scripts/check.sh                      # format, lint, mypy (strict), unit tests + coverage
```

With a local uv ≥ 0.13.8 and Python 3.15 the scripts use them directly. Prefix any
other command with `scripts/dev.sh` to run it in the container, e.g.
`scripts/dev.sh uv run pytest -k domain`.

| Command | What it does |
|---|---|
| `scripts/check.sh` | Same checks as CI: `ruff format --check`, `ruff check`, `mypy`, `pytest --cov` (≥ 90 %) |
| `scripts/integration.sh` | Starts a throwaway Odoo (20 by default, `ODOO_IMAGE=odoo:19.0` or `master` for others) + PostgreSQL, seeds two users with API keys, runs `tests/integration` |
| `docker build -t odoo-community-mcp:dev . && scripts/image-smoke-test.sh odoo-community-mcp:dev` | Builds the image and checks it runs read-only, non-root, without a shell |
| `scripts/dev.sh uv run ruff format .` | Formats the code (CI only checks, it never rewrites) |

## How we work

- **Test first.** Write a failing test, make it pass, then refactor. New logic ships with
  tests; never skip, weaken or delete a test to get a green build.
- **Security rules are tests.** Anything that touches authentication, input handling,
  logging or error output needs a test that proves the rule, e.g. that a key never appears
  in logs or that a non-allowlisted model is refused.
- **Keep the gateway narrow.** Every new input is validated against an allowlist. No
  generic "call any Odoo method" tool, no client-supplied Odoo `context`, no Odoo error
  text or traceback towards clients.
- **English code.** Identifiers, comments and commit messages are in English.
- **Types.** `mypy --strict` must pass; avoid `Any` where a precise type is possible.

## Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org):
  `type(scope): subject` in imperative mood, no trailing period. Types: `feat`, `fix`,
  `docs`, `refactor`, `test`, `chore`, `ci`, `build`, `perf`.
- Work on a branch and open a pull request against `main`; direct pushes are blocked.
- All required checks must pass and review threads must be resolved. Pull requests are
  squash-merged, so the PR title becomes the commit message and must follow the
  convention too.
- Fill in the pull request template, including how you tested the change.

## Dependencies

Every new or upgraded dependency is a supply-chain decision.

- Check that it is maintained, has no known vulnerability in the exact version, is the
  canonical package (no typo-squat) and is not near end of life.
- Pin exact versions in `pyproject.toml`; `uv.lock` is committed and CI runs with `--locked`.
- A 7-day cooldown applies: uv (`exclude-newer = "7 days"`) and Dependabot do not pick
  releases younger than that. For an urgent security fix, override it for that one package
  and say so in the pull request.
- Container base images and GitHub Actions are pinned by digest or commit SHA. Dependabot
  proposes updates; build and runtime base images must move together.

## Releases

Maintainers release by bumping `version` in `pyproject.toml`, merging, and pushing a
`vX.Y.Z` tag on `main`. The release workflow publishes the signed image with SBOMs and
provenance; see the README.

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
