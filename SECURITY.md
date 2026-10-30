# Security scope

Hephestus is a single-operator research prototype for authorized labs. The browser UI and Flask relay bind to loopback. Only the memory-only relay is intended to be exposed, through a disposable tunnel; the browser UI must never be published.

Remote execution has the PowerShell worker user's permissions. Policy patterns, encryption, and approval are supervision controls, not isolation. The relay retains at most one encrypted in-flight job and result in process memory; it has no database, Redis, filesystem queue, or application request log. Third-party tunnel infrastructure may retain metadata outside Hephestus.

## Historical research scope

The master's dissertation evaluated an earlier research beta, before the work-use
V1 and this public V2. Its findings do not validate V2's current safeguards,
remote protocol or production suitability. Intended lab isolation was not
independently established by the retained agent records. See the
[research background](docs/RESEARCH-BACKGROUND.md) for methods and limitations.

## External models and tunnel boundaries

Download compatible text-generation GGUFs from a publisher you trust, review their
licenses and verify published checksums when available. Setup verifies only its
pinned quick-start model; manually downloaded weights do not receive that check
automatically. A file extension or coding-focused label does not establish model
quality, safety or compatibility. Follow the [model guide](README.md#models-and-hardware)
and review every proposed command regardless of the selected model.

The model and relay run on the controller. Cloudflare Quick Tunnel supplies a
temporary public route to that relay; it does not host model inference or expose
the browser UI. The target polls over outbound HTTPS, and command/result payloads
are encrypted by the application. Third-party infrastructure can still observe
traffic metadata. Returning to Local or stopping the controller ends the managed
connection; a fresh Remote session requires newly generated credentials/bundle.

## Reporting a problem

Use the repository's private security reporting facility if it is available. Otherwise, open a minimal non-sensitive issue requesting a private reporting channel. Do not post credential values, private keys, capability URLs, sensitive logs, personal data, or working offensive payloads publicly.

A useful report identifies the version, affected source path, supported entry point, expected boundary, observed behavior, prerequisites, and a safe reproduction description. Redact private material. No response-time or remediation SLA is promised for this portfolio project.

If you find an exposed credential in your own copy, revoke or rotate it with its provider and remove it from shared artifacts and history. Deleting a value from the current file does not invalidate it.

## Release handling

Model weights, caches, local configuration, and credentials are excluded from distributions. Run events, including command output, are saved per session in the local `sessions/` folder so they survive restarts; deleting a session in the UI deletes its file. Exclude `sessions/` from shares and archives and treat it as sensitive evidence. Generated output still requires review before sharing. External models and dependencies retain their own licenses and security lifecycle.

Release hardening does not constitute certification, a complete vulnerability audit, or validation of the original research outcomes.

## Known dependency limitation

The release dependency check flags transitive `diskcache` 5.6.3 with [CVE-2025-69872 / GHSA-w8v5-vhqr-4h9v](https://github.com/advisories/GHSA-w8v5-vhqr-4h9v), concerning unsafe pickle deserialization. The pinned cryptography dependency was updated to 50.0.0 after the advisory check. The verification environment was rechecked with pip-audit; diskcache remains the one affected package and no patched release was reported by that check. `llama-cpp-python` imports this dependency, but Hephestus disables the model cache and does not enable `LlamaDiskCache`.

The vulnerable cache-reading path requires disk caching to be enabled and an attacker able to write the cache that is later read. That feature is disabled here; this does not remove the advisory from the installed dependency inventory. Do not enable pickle-backed disk caches over untrusted or shared writable files. Reassess the dependency and its use when updating the inference runtime.
