# Security scope

Hephestus Agent is a single-operator, local research prototype for authorized labs. The supported browser entry point is `python web.py`, restricted to loopback clients and a per-launch capability session. Public hosting, reverse proxies, port forwarding, and multi-user deployment are outside this release's supported design.

Local execution has the launching user's permissions; SSH execution has the configured remote user's permissions. Policy patterns and approval are supervision controls, not isolation. Configure the lab and credential scope independently. See [safety boundaries](docs/safety.md).

## Reporting a problem

Use the repository's private security reporting facility if it is available. Otherwise, open a minimal non-sensitive issue requesting a private reporting channel. Do not post credential values, private keys, capability URLs, sensitive logs, personal data, or working offensive payloads publicly.

A useful report identifies the version, affected source path, supported entry point, expected boundary, observed behavior, prerequisites, and a safe reproduction description. Redact private material. No response-time or remediation SLA is promised for this portfolio project.

If you find an exposed credential in your own copy, revoke or rotate it with its provider and remove it from shared artifacts and history. Deleting a value from the current file does not invalidate it.

## Release handling

Model weights, run logs, tests/experimental scenarios, caches, local configuration, and credentials are excluded from the public release. Generated local output still requires review before sharing. External models and dependencies retain their own licenses and security lifecycle.

Release hardening does not constitute certification, a complete vulnerability audit, or validation of the original research outcomes.

## Known dependency limitation

The release dependency audit identifies transitive `diskcache` 5.6.3 with [CVE-2025-69872 / GHSA-w8v5-vhqr-4h9v](https://github.com/advisories/GHSA-w8v5-vhqr-4h9v), concerning unsafe pickle deserialization. No patched PyPI release was listed during this release review. `llama-cpp-python` imports this dependency, but Hephestus disables the model cache and does not enable `LlamaDiskCache`.

The vulnerable cache-reading path requires disk caching to be enabled and an attacker able to write the cache that is later read. That feature is disabled here; this does not remove the advisory from the installed dependency inventory. Do not enable pickle-backed disk caches over untrusted or shared writable files. Reassess the dependency and its use when updating the inference runtime.
