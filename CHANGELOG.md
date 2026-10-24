# Changelog

## 0.2.0 — 2026-10-24

Public portfolio release of Hephestus Agent, a supervised local-model command agent developed as a master's cybersecurity project.

- Added isolated virtual-environment setup, pinned direct dependencies, CPU-first inference, explicit hardware settings, and an optional verified download of an official Qwen instruct GGUF.
- Applied backend selection consistently across interfaces, preserved shell-chain semantics, rejected invalid approvals, bounded output, and added repeated-failure handling.
- Restricted the browser UI to local capability-authenticated operation, protected state changes and approval proposals, added privilege confirmation, and escaped untrusted text.
- Enforced SSH known-host verification, deferred connections until execution, and kept dry runs disconnected.
- Improved stop/deadline handling while documenting inference, console-input, process-tree, and remote-process limits.
- Removed automatic external network probes; optional connectivity observations explicitly avoid claiming target isolation.
- Updated logs and metrics to separate model completion claims from independently reviewed outcomes; removed inferred financial costs and unsupported inferential statistics.
- Retained supervised user-supplied batch goals and VM snapshot utilities with distinct trial logs and fail-closed snapshot/readiness handling.
- Consolidated documentation and excluded weights, run logs, experimental test material, and caches from the source release.

The release changes software behavior and reporting. It does not rerun the original thesis experiments, verify previously unverified goals, or establish a new benchmark.
