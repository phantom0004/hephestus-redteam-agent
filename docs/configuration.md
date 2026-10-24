# Configuration

Copy [config.example.yaml](../config.example.yaml) to `config.local.yaml`. Supply `--config config.local.yaml` to the CLI, web launcher, or batch utility, or set `HEPHESTUS_CONFIG`.

Precedence: **CLI overrides → environment → YAML → defaults**. Nested YAML keys override equivalent flat keys. YAML model/log/key/known-host paths resolve beside the YAML file; environment and CLI paths resolve from the working directory. `~` expands to the current user's home. Invalid values fail validation.

## Model and inference

| YAML key | Environment variable | Default / meaning |
| --- | --- | --- |
| `llm.model` | `LLM_MODEL` | Project `models/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf`. |
| `llm.max_tokens` | `LLM_MAX_TOKENS` | `640`; positive generation limit. |
| `llm.temperature` | `LLM_TEMPERATURE` | `0.45`; range `0..2`. |
| `llm.top_p` | `LLM_TOP_P` | `0.9`; greater than zero, at most one. |
| `llm.context_size` | `LLM_CONTEXT_SIZE` | `8192`; at least `512`, leaving room for prompt and generation. |
| `llm.gpu_layers` | `LLM_GPU_LAYERS` | `0` CPU; positive layer count or `-1` to request full offload with compatible runtime. |
| `llm.threads` | `LLM_THREADS` | Half detected logical CPUs, minimum one. |
| `llm.batch_size` | `LLM_BATCH_SIZE` | `512`; positive prompt-processing batch size. |
| `llm.chat_format` | `LLM_CHAT_FORMAT` | Empty; use metadata/runtime selection. Override only with a supported format. |

The loader checks file availability and GGUF magic; it does not certify provenance, model quality, architecture support, or chat-template correctness. The native tokenizer makes a final prompt-budget check. Context and batch settings affect memory demand. See [models](models.md).

## Runtime

| YAML key | Environment variable | Default / meaning |
| --- | --- | --- |
| `runtime.log_dir` | `LOG_DIR` | Project `logs/`. |
| `runtime.dry_run` | `DRY_RUN` | `false`; plan and approve without executing. |
| `runtime.max_steps` | `MAX_STEPS` | `15`; positive iteration limit. |
| `runtime.timeout_seconds` | `RUN_TIMEOUT_SECONDS` | `0` disables overall deadline; otherwise nonnegative seconds. |
| `runtime.backend` | `BACKEND` | `local`; `local` or `ssh`. |
| `runtime.command_timeout_seconds` | `COMMAND_TIMEOUT_SECONDS` | `120`; positive per-command timeout, bounded by remaining run deadline. |
| `runtime.max_context_tokens` | `MAX_CONTEXT_TOKENS` | `5000`; conservative history budget below model context size. |
| `runtime.output_max_chars` | `OUTPUT_MAX_CHARS` | `32000`; positive retained-text limit per stream, with separate buffer/size checks. |
| `runtime.max_repeated_failures` | `MAX_REPEATED_FAILURES` | `3`; repeated unchanged failures and consecutive empty-proposal threshold. |
| `runtime.target_os` | `TARGET_OS` | `auto`; `auto`, `linux`, or `windows` for policy. |
| `runtime.require_supervisor_approval` | `REQUIRE_SUPERVISOR_APPROVAL` | `true`; extra confirmation for detected privilege commands, not a separate identity. |

History trimming uses a conservative byte-budgeted tail, not an exact token count. The native tokenizer applies a final check. Stop/deadline checks cannot preempt an in-process generation call. SSH timeout/stop closes the channel; the remote process may continue.

## Policy

| YAML key | Environment variable | Default / meaning |
| --- | --- | --- |
| `policy.max_command_length` | `POLICY_MAX_COMMAND_LENGTH` | `4000`; positive character limit. |
| `policy.max_violations` | `POLICY_MAX_VIOLATIONS` | `3`; rejected-batch limit. |
| `policy.allowlist` | `POLICY_ALLOWLIST` | Empty list; optional regex restrictions. |
| `policy.denylist` | `POLICY_DENYLIST` | Dangerous-pattern rules in `hephestus/config.py`. |
| `policy.blocked_primitives` | `POLICY_BLOCKED_PRIMITIVES` | `dd`, `mkfs`, `reboot`, `shutdown`, `format`, `diskpart`, `bcdedit`. |

Use YAML lists for regexes. Environment lists are comma-separated. Lists replace corresponding defaults. OS selection affects primitive checks; it does not switch the execution shell. Pattern rules are not a shell parser or sandbox.

## SSH

| YAML key | Environment variable | Default / meaning |
| --- | --- | --- |
| `ssh.host` | `SSH_HOST` | Empty; required for SSH. |
| `ssh.port` | `SSH_PORT` | `22`; range `1..65535`. |
| `ssh.username` | `SSH_USERNAME` | Empty; required for SSH. |
| `ssh.key_path` | `SSH_KEY_PATH` | Empty; optional private-key file. Agent and normal key discovery remain available. |
| `ssh.known_hosts` | `SSH_KNOWN_HOSTS` | Empty; optional additional known-host file. Normal user entries are loaded. |
| `ssh.timeout_seconds` | `SSH_TIMEOUT_SECONDS` | `30`; positive connection/authentication timeout. |

Verify server fingerprints independently before enrollment. An unverified `ssh-keyscan` result alone is not proof of identity. Unknown or changed host keys fail closed. Prefer an unprivileged lab user.

## Interfaces

CLI overrides: `--model`, `--max-tokens`, `--max-steps`, `--timeout`, `--backend`, `--log-dir`, and `--dry-run`. `--context` supplies planning context. `--validate-network` explicitly requests host connectivity observations; it does not enforce isolation. Use `hephestus --help`.

The web launcher accepts `--config`, `--port`, `--no-browser`, and loopback `--host` values `127.0.0.1`, `localhost`, or `::1`. Public bind and debug mode are unsupported. Use its complete per-launch capability URL.
