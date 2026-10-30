# Changelog

## 2.0.0 — portfolio release

- Default local execution and same-machine browser approval; no remote infrastructure at startup.
- Explicit Local/Remote selector with a built-in guide and worker connection status.
- Per-connection generated endpoint and relay credentials; private downloadable worker bundle.
- Owned relay/tunnel lifecycle with cleanup on setup failure, Local selection and normal shutdown.
- Generic cybersecurity audit PDF without company artwork or confidentiality labels; evidence hashes retained.
- Rewritten operating guide, supplied README banner and source-release exclusions.
- Retains the upgraded app's model library, tuning, GPU telemetry, Quick/Goal workflows,
  command edits, sessions, evidence exports and Ask the AI panel.
- Removes private runtime material and nonportable setup output from the source folder.

### Documentation and source cleanup

- Move the renamed remote handler to `hephestus/worker/remote_handler.ps1`; update packaging and connection bundles.
- Remove bundled tests, test-only tools/dependencies, an unused network helper and internal release planning material.
- Add beginner model selection, exact GGUF download/loading steps and hardware guidance to the README, model guide and in-app Help.
- Explain Local versus Remote execution and the controller-hosted relay / temporary Cloudflare route in the architecture diagram.

### Publication checks

- Controller terminal is launch/log-only; the installed entry opens the browser workspace.
- Edits rotate approval identity and bind decisions to immutable revised text; enabled safety review is repeated.
- Reject misplaced shell-language labels and regenerate model output instead of treating them as commands.
- Remote transport bounds account for serialized Unicode/control output; unknown execution outcomes stop automatic retries.
- Update cryptography to 50.0.0 and retain the documented disabled-cache dependency advisory.
- Fresh installed packages use writable per-user data, with HEPHESTUS_DATA_DIR available for portable setups.
- Add a local animated README banner and explicit full-authorization guidance.

## Earlier code history

Local GGUF inference, supervised command proposals, terminal and browser control,
policy checks, local or SSH execution, JSONL audit logging and experimental metrics.
Project lineage: the master's study evaluated an earlier research beta. The author
subsequently expanded V1 for red-team work and developed V2 for public release.
The historical study did not evaluate V1 or V2. See [research background](docs/RESEARCH-BACKGROUND.md).

## Research documentation

- Add the MSc origin, author, dissertation context, methods, findings and selected primary references.
- Distinguish the evaluated research beta, later work-use V1 and current public V2.
- Preserve model selection, installation and Local/Remote operating guides; make no new performance claims.
