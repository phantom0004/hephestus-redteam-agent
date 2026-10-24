# Safety boundaries and limitations

Hephestus is a supervised research prototype. Run it only in an authorized lab. The operator account, SSH account, filesystem, model, and native runtime remain trusted parts of the environment.

## What the application enforces

- Validated settings and bounded goal/context lengths.
- Policy checks before approval and execution, including command-length limits, optional regex allowlists, denylist patterns, blocked primitives, and a rejected-batch limit.
- Explicit operator decisions; detected privilege commands receive extra confirmation when enabled.
- Web access restricted to loopback clients, with host/origin checks, a per-launch capability, operator session, token-protected changes, and one-use proposal identifiers.
- Escaped browser model/output text and a content security policy, without third-party browser scripts.
- SSH server-key verification using known-host entries, rejection of unknown keys, and no SSH connection during dry run.
- Runtime step, failure, output, and timeout controls, plus stop polling.
- Redaction of recognized credential fields/patterns and selected known environment secrets before logging.

These controls reduce specific failure paths. Pattern matching cannot prove arbitrary shell commands safe. Extra privilege confirmation is a second decision by the same operator, not independent supervisor authentication. Indirect privilege changes may not match the configured detection patterns.

## Execution authority

Local commands run as the user who launched the application. Windows uses Windows PowerShell; POSIX uses `/bin/sh`. Files, environment, working directory, network access, and account rights belong to that execution context. Use an unprivileged lab account and external isolation appropriate to the task.

SSH uses the explicitly configured user. Verify the server fingerprint through a trusted channel before enrolling it. Do not disable host-key checks to make a connection succeed. Key files and SSH agents can grant powerful access; choose credentials limited to the lab.

Commands containing shell chains remain intact for one approval. Approve the complete command, including substitutions, redirections, pipelines, and nested interpreters. After a command fails, remaining approved proposals in that batch are skipped so the next plan can adapt.

## Local browser access

Launch with `python web.py` and use its complete capability URL. Keep the URL and local session private. Restarting the server changes the token and session secret. Public hosting, port forwarding, reverse-proxy deployment, multiple operators, and debug serving are unsupported.

Loopback controls do not defend against a compromised local account, browser, extension, or operating system. Logs and pending proposals are available to the authenticated local operator. Treat browser access as command-execution authority after approval.

## Stop behavior

Use the UI stop/abort controls, console `q` or Ctrl+C, or create `HEPHESTUS_STOP` in the working directory or configured log directory. Stop signals are polled during the agent loop and supported execution paths.

Local execution attempts to kill its process tree on stop, timeout, or excessive output. Escaped/background processes may require independent inspection. SSH closes its channel; this cannot guarantee the remote process terminated. In-process model generation cannot be interrupted by these checks, and a console approval prompt may need input or Ctrl+C before control returns. A deadline is therefore not a universal wall-clock guarantee.

A web worker remains active while stopping; do not assume an end-state display proves that every external effect has ended. Confirm local and remote activity before restoring snapshots or restarting work. Remove a persistent stop file only when ready for another run.

## Network observations

Normal startup does not probe external services. `--validate-network` opts into ping to `8.8.8.8`, DNS lookup for `example.com`, and HTTPS to `example.com` from the agent host. These observations make network requests, including in a command dry run if explicitly selected.

Reachable probes establish selected host connectivity. Failed or unavailable probes do not prove isolation, and observations do not inspect the SSH target's routing or enforce an egress policy. Configure VM networking, routing, firewalling, and target scope separately.

## Sensitive data and publication

Logs retain bounded execution output before observer summarization. Context and goal updates are not directly logged verbatim, but may be repeated by plans or commands. Goal text, local paths, target metadata, and unrecognized secrets can remain. Redaction is a best-effort transformation, not a guarantee that a log is safe to publish.

Protect logs with host permissions and inspect them before export. Never put credentials in a goal, issue, screenshot, or capability URL shared with others. The software license does not extend to external model weights or target data.

The public source release excludes bundled models, logs, tests/experimental scenarios, and caches. Optional downloads and new runs create local artifacts afterward; keep those ignored files out of subsequent Git commits and release archives.

## What outcomes mean

A model completion signal is unverified. A successful command may be unrelated to the objective or only partial progress. Descriptive metrics cannot establish goal success, network isolation, autonomy, or the absence of vulnerabilities. Independently review task-specific evidence and effects.
