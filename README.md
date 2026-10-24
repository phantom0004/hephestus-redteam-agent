# Hephestus Agent

<img width="1270" height="715" alt="Hephestus Logo" src="https://github.com/user-attachments/assets/958a405c-36ca-488d-8f69-786f9ed6a84f" />

A supervised local LLM command agent developed as a master's cybersecurity project.

Hephestus explores how a local language model can propose security research commands while an operator reviews each proposal. It combines local GGUF inference, configurable policy checks, explicit approval, local or SSH execution, and JSONL audit logs. A terminal interface and a local browser interface use the same agent loop.

```text
Goal + context → Local model → Command proposals → Policy checks
                                                    ↓
Observations ← Local shell or SSH ← Operator approval
```

This is a lab research prototype. Commands run with your local or SSH account's permissions. Pattern checks and approval support supervision; use an isolated lab and assess every proposal. A model's completion claim is recorded as **unverified**.

## What the project demonstrates

- In-process inference through `llama-cpp-python`, with configurable model and hardware settings.
- A plan, propose, approve, execute, and observe loop with bounded output and failure handling.
- Per-command console approval and a browser interface with one-use proposals and extra confirmation for detected privilege commands.
- Local Windows PowerShell or POSIX `/bin/sh` execution, plus SSH with known-host verification.
- Structured logs and descriptive metrics that distinguish execution, approval, and model claims from independently verified outcomes.

The original work observed Linux enumeration and partial Windows information gathering. Privileged shell and protected-file goals were not verified. Invalid syntax, missing tools, timeouts, refusals, and unsupported completion claims limited the experiments. This release improves the software and reporting; it does not rerun the thesis experiments or change their outcomes. See [research notes](docs/research.md).

## Install

Use Python **3.10 or newer**; **3.12** is recommended for wheel availability. Installation requires internet access and Python's `venv` module. Memory and disk requirements depend on your model.

```sh
git clone https://github.com/phantom0004/hephestus-agent.git
cd hephestus-agent
python setup.py
```

Setup creates `.venv`, installs the pinned direct dependencies and project, checks dependencies, and verifies runtime imports. It then offers an optional download:

```text
Download the quick-start model? [y/N]:
```

The optional model is the official **Qwen2.5-Coder-1.5B-Instruct Q4_K_M GGUF**, approximately **1.12 GB**. Setup downloads a pinned revision and checks its size, GGUF header, and SHA-256 before installing it. The model has its own Apache-2.0 license. Weights are absent from this release.

| Setup option | Behavior |
| --- | --- |
| `python setup.py --no-model` | Install the environment and use a model you supply. |
| `python setup.py --download-model` | Install and download the pinned model without prompting. |
| `python setup.py --model-only --download-model` | Download the model without installing dependencies. |
| `python setup.py --venv PATH` | Choose the environment directory. |
| `python setup.py --llama-backend cpu` | Select the upstream inference wheel backend; CPU is the default. |

Noninteractive setup skips the model unless `--download-model` is supplied. Backend choices are `cpu`, `cu130`, `cu132`, `metal`, `vulkan`, and `rocm72`. The chosen backend needs a compatible binary wheel, operating system, Python version, and drivers/runtime. Setup fails if no matching inference wheel is available; it does not silently build a GPU runtime. Installing a GPU wheel does not enable offload: configure `llm.gpu_layers`, which defaults to `0`. See [model and hardware setup](docs/models.md).

Activate in **Windows PowerShell**:

```powershell
.\.venv\Scripts\Activate.ps1
```

Or in a **POSIX shell**:

```sh
. .venv/bin/activate
```

If activation is unavailable, invoke `.venv\Scripts\python.exe` on Windows or `.venv/bin/python` on POSIX directly. On Windows, `py -3.12 setup.py` can select Python 3.12 when the `py` launcher is installed.

## First run

Start with a dry run. Planning and approval still happen, but proposed commands do not execute; SSH dry runs do not connect.

```sh
hephestus "List the current directory" --dry-run
```

For your own instruct GGUF:

```sh
hephestus "List the current directory" --dry-run --model "/path/to/instruct-model.gguf"
```

Use a single GGUF file with an architecture and chat template supported by the installed llama.cpp runtime. GGUF is a container format; arbitrary GGUF models are not guaranteed to work. You can also select the model with `LLM_MODEL` or YAML.

Console decisions are `y` to approve, Enter to reject, `q` to abort, or `u` to change guidance. Detected `sudo`, `su`, `runas`, and `doas` commands require `yes` when enhanced approval is enabled. Review the entire proposal: chains such as `&&` and `||` remain one shell command and one approval.

Use `hephestus --help` for CLI options. Omit `--dry-run` only when ready to execute approved commands in your lab.

## Browser interface

```sh
python web.py
```

Open the **complete private capability URL** printed by the launcher, or let it open automatically. Its token is in the URL fragment and is exchanged for a local operator session. Keep the URL private; each server launch generates a new token. `--no-browser` disables automatic opening, `--port 5001` changes the port, and `--config config.local.yaml` loads configuration.

The UI serves one operator on the same computer. It permits loopback clients only, checks request origins, requires a session and token for changes, binds approval to a one-use proposal, and displays model output as escaped text. Public hosting, reverse-proxy exposure, multi-user operation, and debug mode are unsupported. Execution defaults to the local host, with dry run off unless configured.

## Configure a lab run

Copy [config.example.yaml](config.example.yaml) to `config.local.yaml`, edit it, then run:

```sh
hephestus "Inspect the authorized lab host" --config config.local.yaml --dry-run
python web.py --config config.local.yaml
```

Precedence is **CLI overrides → environment → YAML → defaults**. YAML paths resolve beside the YAML file; environment and CLI paths resolve from the working directory. See [all configuration settings](docs/configuration.md).

For SSH, set `runtime.backend: ssh`, `ssh.host`, and `ssh.username`. Independently verify the server fingerprint before enrolling its key in normal `known_hosts` or a file selected by `SSH_KNOWN_HOSTS`. Unknown or changed keys are rejected. Use an SSH agent or `ssh.key_path`; keep private keys and local configuration out of Git. Backend selection is shared by CLI, web, and batch runs.

## Logs and experiments

New runs write ignored JSONL files under `logs/` by default. Events include plans, proposals, approvals, execution results, and termination reasons. Extra context and updated guidance are not logged verbatim, but can influence plans and output. Known credential patterns and selected environment secrets are redacted; arbitrary sensitive target data can remain. Inspect logs before sharing.

```sh
python -m hephestus.tools.metrics logs
```

Metrics read nested run directories, deduplicate run IDs, and export `metrics_summary.csv` and `metrics_summary.json`. Counts describe recorded behavior. A zero exit code or model claim does not establish task success. [Research notes](docs/research.md) cover batch runs, VM snapshots, and interpretation.

The stop button, Ctrl+C, and a `HEPHESTUS_STOP` file provide stop paths. Local execution polls for stop/timeout and attempts process-tree termination. SSH closes its channel, which cannot guarantee remote-process termination. In-process inference may finish before the agent acts on a stop request. Remove a stop file only after confirming the run ended. See [safety and limitations](docs/safety.md).

## Project map

| Path | Purpose |
| --- | --- |
| `hephestus/src/` | Agent loop, inference, policy, approval, execution, observation, logging. |
| `hephestus/web/` | Local Flask API, worker coordination, browser UI. |
| `hephestus/tools/` | Batch, metrics, VM, and backend utilities. |
| `hephestus/utils/` | Shared metrics, optional connectivity observations, VM management. |
| `models/README.md` | External model provisioning; no weights shipped. |
| `docs/` | [Architecture](docs/architecture.md), [configuration](docs/configuration.md), [models](docs/models.md), [research](docs/research.md), [safety](docs/safety.md). |

The release excludes model weights, run logs, experimental test material, and caches. Use the software only on systems you own or are authorized to assess. See [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md), and [CITATION.cff](CITATION.cff).

The dependency inventory includes a documented [diskcache advisory with its cache feature disabled](SECURITY.md#known-dependency-limitation); the dependency audit is not claimed clean.

Code is licensed under [MIT](LICENSE). External models and dependencies retain their own licenses.
