# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately via
[GitHub Security Advisories](https://github.com/meevagmbh/odoo-community-mcp/security/advisories/new).
Do not open a public issue. We aim to acknowledge reports within three working days.

## Coordinated vulnerability disclosure

1. You report privately (see above). Include affected version, impact and a reproduction.
2. We confirm receipt, assess severity (CVSS) and agree on a disclosure date with you,
   normally within 90 days.
3. We fix the issue, publish a patched release and a GitHub Security Advisory (with CVE
   where applicable) and credit you unless you prefer otherwise.

Actively exploited vulnerabilities are handled with priority.

## Supported versions

Security fixes are published for the latest release only, as a new patch release.
Upgrade by pulling the new image tag; the release notes name the fixed advisories.

## Release evidence (EU Cyber Resilience Act)

Every release `vX.Y.Z` provides, as GitHub release assets and/or OCI attestations:

| Evidence | Where |
|---|---|
| SBOM of the container image (SPDX 2.3, CycloneDX) | release assets, SBOM attestation in GHCR |
| SBOM of the Python dependencies (CycloneDX 1.5, from `uv.lock`) | release assets |
| SLSA build provenance | attestation in GHCR (`gh attestation verify`) |
| Image signature (Sigstore, keyless) | GHCR (`cosign verify`) |
| SHA-256 checksums of all assets | `SHA256SUMS` |
| Vulnerability gate (no known fixable high/critical CVEs at release time) | release workflow |

Dependencies are monitored continuously by Dependabot alerts and security updates.

## Security model

- The server holds no credentials. Each request carries the caller's own Odoo API key,
  which is forwarded unchanged to Odoo's JSON-2 API; Odoo enforces all access rights.
- Keys are never stored or logged. Logs contain a short SHA-256 fingerprint only.
- Requests without a well-formed `Authorization: Bearer` header are rejected with 401
  before any MCP processing.
- Only allowlisted models are reachable, each as read, read-write or read-write-delete.
  Writing and deleting each need their own operator switch; deleting is only possible for
  explicitly named models, at most 20 records per call. There is no generic method call.
- Domains, field names, sort order, written values, limits and body sizes are validated;
  client-supplied Odoo `context` is never forwarded.
- Odoo errors are mapped to fixed messages. Only for Odoo business errors (validation and
  user errors) the first line of Odoo's message is added, shortened and with token-like
  strings removed. Tracebacks never reach clients.
- No cookies are kept and redirects are not followed when talking to Odoo.
- The container image is minimal (no shell, no package manager), runs as non-root and
  supports a read-only root filesystem. Releases are signed and carry provenance.

The design targets OWASP ASVS 5.0 Level 2 for the parts it implements.
