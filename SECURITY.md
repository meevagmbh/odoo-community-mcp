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

## Remediation time frames

Vulnerable third-party components (Python packages, base image, GitHub Actions) are
updated within these time frames, counted from the day a fixed version is available.
The severity is the CVSS rating of the advisory, adjusted for how this server uses the
component.

| Severity | Fixed release within |
|---|---|
| Critical | 7 days |
| High | 30 days |
| Medium | 90 days |
| Low | next regular update |

Libraries without known vulnerabilities are updated routinely: Dependabot proposes updates
weekly (after a 7-day cooldown for new releases), and they are merged within 30 days.

## Security model

- The server holds no credentials. Each request carries the caller's own Odoo API key,
  which is forwarded unchanged to Odoo's JSON-2 API; Odoo enforces all access rights.
- Keys are never stored or logged. Logs contain a short SHA-256 fingerprint only.
- Requests without a well-formed `Authorization: Bearer` header, or with a key Odoo
  rejects, are refused with 401 before any MCP processing (see [Authentication](#authentication)).
- Every response carries `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` and
  `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`.
- Model names from clients must be plain Odoo model names before they are matched against
  the allowlist or put into the Odoo URL path.
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

## Authentication

**One pathway.** `/mcp` accepts only `Authorization: Bearer <Odoo API key>`. `/healthz`
is the only unauthenticated path; it returns `{"status":"ok"}` and nothing else. There
is no other login, session or token.

**Key check.** Before any MCP method runs, including `initialize`, `tools/list` and
`list_models`, the server asks Odoo whether it accepts the key (`res.users.context_get`
with that key). Odoo's answer is cached per key for `AUTH_CACHE_SECONDS` (default 60 s):

- accepted (or authenticated but without rights for that method): the request continues;
- rejected: 401, also for repeated requests with that key until the cache entry expires;
- timeout or server error: 503, not cached.

So a key revoked in Odoo keeps working here for at most `AUTH_CACHE_SECONDS`; the field
metadata cache has the same bound.

**Anti-automation.** Keys not in the cache share one budget of
`AUTH_ATTEMPTS_PER_MINUTE` key checks per process; beyond it, such requests get 429
without reaching Odoo. Verified keys are then limited to `RATE_LIMIT_PER_MINUTE` requests
each. One attacker can use up the shared budget and delay new, legitimate keys for up to
a minute; operators should therefore also limit requests per client IP at the reverse
proxy.

**Single factor, by design (ASVS v5.0.0-6.3.3).** Access needs one factor: an Odoo API
key. MCP clients send it unattended on every request, so a second interactive factor per
request is not possible, and Odoo itself does not ask for TOTP on API keys. The
mitigating controls are:

- Keys are created by the user in Odoo after Odoo's identity check (password, or passkey
  where configured). Operators must enforce Odoo's two-factor authentication for users
  who may create keys.
- Odoo limits a key's lifetime (at most three months) and its scope (`rpc`), and users or
  administrators can revoke a key at any time; revocation takes effect here within
  `AUTH_CACHE_SECONDS`.
- Each key acts only with its owner's Odoo rights. The gateway narrows them further by
  the model allowlist, and writing and deleting each need an operator switch.
- Keys travel only over TLS (client to proxy, and to Odoo unless the operator sets
  `ODOO_ALLOW_INSECURE_HTTP`), and are never stored or logged.
- Rate limits per key and for key checks, and an audit event per tool call and per
  rejected request (with the key fingerprint), allow misuse to be detected.

OAuth 2.1 with an MFA-capable identity provider would replace this rationale.

## Transport security

- Client to server: TLS ends at the operator's reverse proxy. The server itself listens on
  plain HTTP and has no TLS setting; keep the proxy-to-container hop on the same host or
  an isolated network.
- Server to Odoo: `https://` with certificate and hostname verification is required.
  `ODOO_ALLOW_INSECURE_HTTP=true` permits `http://`, which sends API keys and data
  unencrypted; it is meant for development and tests only.

## Cryptographic inventory

| Use | Algorithm / mechanism | Protects | Where |
|---|---|---|---|
| Key digest for rate-limit buckets, key-check and field caches | SHA-256 (full hex digest, in memory only) | Avoids holding raw keys as cache keys | `src/odoo_mcp/auth.py` |
| Key fingerprint in logs | First 8 hex chars (32 bits) of the SHA-256 digest | Identifies a key in logs without revealing it; not a secret, not for authentication | `src/odoo_mcp/auth.py` |
| Connection to Odoo | TLS via httpx2, certificate and hostname verification on | API keys and Odoo data in transit | `src/odoo_mcp/odoo_client.py` |
| Release checksums | SHA-256 (`SHA256SUMS`) | Integrity of release assets | `.github/workflows/release.yml` |
| Image signature | Sigstore keyless signing with cosign (ephemeral key, Fulcio certificate, Rekor log) | Authenticity of container images | `.github/workflows/release.yml` |
| Provenance and SBOM attestations | Sigstore-signed in-toto attestations | Build origin and contents of images | `.github/workflows/release.yml` |

The server generates no keys, random tokens or passwords and encrypts no data itself.
Any new use of cryptography must be added to this table.

## Data inventory and protection levels

| Data | Class | Requirements |
|---|---|---|
| Odoo API keys | Secret (credential) | Only in request headers and memory; TLS in transit; never stored, logged or returned. Logs carry the fingerprint only. |
| Odoo records and field metadata relayed to clients (may include personal data such as names, e-mail addresses, addresses and accounting data) | Confidential, may be personal | Read and written with the caller's own Odoo rights and only for allowlisted models; TLS in transit; never stored or logged; responses are `no-store`. Field metadata is cached in memory for 60 s per key. |
| Client input (domains, values, ids) | Confidential, may be personal | Validated, forwarded to Odoo only; never logged. |
| Odoo business error text (first line) | Internal | Returned to the caller only, shortened and with token-like strings removed; never logged. |
| Audit log (key fingerprint, tool, model, field names, record ids, outcome, timing) | Internal, pseudonymous | Written to stdout as JSON. Operators must restrict access to operations staff, protect it in transit and at rest in their log platform, and define a retention period. |
| Configuration (Odoo URL, database name, model allowlist) | Internal | Set by the operator through environment variables; contains no secrets. |
