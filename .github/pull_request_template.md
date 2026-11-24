## What and why

<!-- What does this change and why? Link issues with "Closes #123". -->

## How it was tested

- [ ] `scripts/check.sh` (format, lint, mypy, unit tests)
- [ ] `scripts/integration.sh` (if Odoo interaction changed)
- [ ] Image smoke test (if Dockerfile or runtime changed)

## Security

- [ ] No API key, request body or Odoo error text can reach logs or clients
- [ ] New inputs are validated against an allowlist
