# Architecture

Hephestus is a single-operator Python application. The Click CLI and local Flask UI share an agent loop; inference runs in process through `llama-cpp-python`. There is no hosted model service, application database, billing system, or multi-tenant account model.

## Execution flow

1. Load and validate configuration, goal, and context.
2. Select the configured backend centrally in the agent.
3. Ask the local model for a plan using goal, context, and bounded observation history.
4. Extract explicit commands without splitting `&&` or `||` chains.
5. Reject the entire batch if a command fails policy.
6. Obtain operator approval; revalidate that approved commands belong to the proposal.
7. Execute approved commands in order; stop the remaining batch after an execution error.
8. Record bounded output, summarize observations, and repeat until a stop condition.

A standalone completion signal ends the run as an unverified model claim. A signal mixed with executable proposals does not verify the goal. Exit code zero describes command status, not a research outcome.

## Components

| Source | Responsibility |
| --- | --- |
| `hephestus/cli.py`, `web.py` | Interface startup and configuration. |
| `hephestus/config.py` | Validated defaults, YAML, environment, CLI settings. |
| `hephestus/src/agent.py` | Shared orchestration, failure tracking, limits, termination. |
| `hephestus/src/planner.py`, `local_llm.py` | Prompt construction and serialized native inference. |
| `hephestus/src/interpreter.py` | Command extraction. |
| `hephestus/src/policy_engine.py` | Configurable pattern checks and stop signal. |
| `hephestus/src/human_gate.py` | Console approval or installed web approver. |
| `hephestus/src/executor.py` | Windows PowerShell, POSIX `/bin/sh`, SSH. |
| `hephestus/src/observer.py`, `logger.py` | Observation summaries and redacted JSONL events. |
| `hephestus/web/app.py`, `runner.py` | Local session controls, one worker, one-use proposals, event streaming. |

## Authority boundaries

The model proposes text. Policy and approval mediate it before a shell runs. Pattern matching cannot prove a command harmless, detect every indirect privilege action, or replace VM isolation. The approving operator remains responsible for meaning and scope.

Local execution inherits the account and working directory. Windows uses Windows PowerShell with no profile and noninteractive input; POSIX uses `/bin/sh`. Generated syntax must match the shell and installed tools. SSH uses the configured user, verified known-host entries, and lazy connection establishment. Dry runs return before command execution or SSH connection.

The web launcher permits loopback binding only. Requests are checked for loopback client and host; a per-launch capability establishes a session, and changes require the operator token. Approval carries a one-use proposal identifier. Detected privilege commands need extra confirmation. This is the local operator's authority, not a separate supervisor account.

The browser escapes model text and output; no remote script or font dependency is required. A content security policy limits scripts. Protect the capability URL, browser session, host account, model files, and native dependencies.

## Resources and retained data

| Resource | Default and behavior |
| --- | --- |
| GGUF | Project `models/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf`; external, optional download. |
| Logs | Project `logs/run_<timestamp>_<run-id-prefix>.jsonl`; configurable and ignored by Git. |
| Batch logs | `<output-dir>/logs/<safe-id>_trial<n>_<unique-suffix>/`. |
| Model state | Lazy in-process load; serialized inference; configurable context, threads, batch, GPU layers. |
| Local output | Disk-backed temporary streams polled for size; bounded retained text. |
| SSH output | Bounded buffers and polling; channel closure does not supervise remote-process lifetime. |
| Stop file | `HEPHESTUS_STOP` in working directory or configured log directory. |

Logs receive bounded execution results before observer summaries. Redaction covers recognizable secrets and known environment values, not arbitrary confidential content. Local logs are mutable evidence, not a tamper-proof audit service.

Normal startup makes no automatic external connectivity probes. The optional CLI observations inspect selected connectivity from the agent host; failed probes and SSH selection do not prove target isolation. Configure networking separately.

See [configuration](configuration.md) and [safety](safety.md).
