# Research context and interpretation

Hephestus Agent originated as a master's cybersecurity project exploring supervised local-model assistance in a controlled lab. The software makes planning, policy decisions, operator intervention, and command execution inspectable. It does not establish autonomous penetration-testing capability.

## Original observations and limits

The original work observed Linux enumeration and partial Windows information gathering. Privileged shell and protected-file objectives were not verified. Invalid syntax, missing tools, timeouts, refusals, and unsupported completion claims limited progress.

These observations are contextual, not a new benchmark. This release does not publish thesis content, raw experiment records, bundled test scenarios, or model weights. It does not rerun the original experiments or retroactively change their outcomes. The smaller optional quick-start model and the repaired runtime should not be treated as the original experimental configuration.

## What a run records

The agent records plans, command proposals, policy decisions, approval decisions, bounded execution results, observations, and a termination reason. A standalone completion marker records a `model_completion_claim` with an unverified outcome. A command returning zero, a run reaching an end state, or a plan asserting completion is insufficient evidence that its objective was achieved.

JSONL entries contain a timestamp, run ID, event name, redacted data, and dry-run flag. Additional context and updated guidance are represented by presence flags rather than logged verbatim. They can still appear indirectly in generated plans or command output. Logs can contain private target information and local paths despite redaction.

## Descriptive metrics

```sh
python -m hephestus.tools.metrics logs
```

The utility recursively reads JSONL files, keeps unique run IDs, and writes `metrics_summary.csv` and `metrics_summary.json` in the supplied directory. Legacy completion events are treated as unverified model claims.

| Measure | Interpretation |
| --- | --- |
| Proposed commands | Recorded executable proposals, including proposals later rejected. |
| Approved commands | Commands accepted by the operator. |
| Executed commands | Execution events with `executed=true`; excludes dry-run simulations. |
| Approval events | Operator decision events, including rejection, update, or abort. |
| HIR | Approval-decision events divided by proposed commands. This is an event/proposal ratio, not a fraction of autonomous capability. |
| Duration / attempts | Descriptive timings and planning counts from available events. |
| Completion | The run ended; this is distinct from its goal being met. |
| Outcome | Unverified unless a separate evidence-bearing outcome review was recorded. |

The parser recognizes explicit `outcome_review` events, but it does not independently validate their evidence or authenticate a reviewer. Agent runs do not create a verified outcome automatically. Dry runs never qualify as verified success. Local log files are editable; treat metrics as descriptions of those files.

No financial cost is inferred from local inference, and no inferential confidence intervals are produced. Repeated trials on selected goals do not establish a general success rate or independence. Keep model claims, operator-reviewed outcomes, environment failures, and partial progress separate when writing results.

## Supervised batch runs

Provide your own goals YAML outside the published source. Each top-level section contains a list of goals:

```yaml
local_lab:
  - id: directory_listing
    goal: "List the current directory"
```

IDs must be unique and use 1–64 letters, digits, underscores, or hyphens. Goal text must contain 5–5000 characters. This example is a usage illustration, not experimental evidence.

```sh
python -m hephestus.tools.batch_runner --goals my-goals.yaml --trials 3 --config config.local.yaml --output-dir batch_results
```

Set `runtime.dry_run: true` in the configuration for a first pass. Batches remain supervised: each command still requires approval. Trial counts are limited to 1–1000. Each trial gets a unique log directory under `<output-dir>/logs/`, avoiding accidental reuse of earlier logs. Batch CSVs contain descriptive trial data and mark goal success as unverified; inspect individual outcomes before making claims.

## Optional VirtualBox snapshots

The operator utility requires VirtualBox and `VBoxManage` on PATH:

```sh
python -m hephestus.tools.vm_manager snapshot list "Lab VM"
python -m hephestus.tools.vm_manager snapshot create "Lab VM" baseline
python -m hephestus.tools.vm_manager snapshot restore "Lab VM" baseline
```

Snapshot mutations require a stopped, saved, or aborted VM. Confirm the target VM and snapshot before restoring: restoration discards subsequent VM state.

Batch accepts `--vm-name` and `--snapshot` together. These do not start or boot a VM. The operator must arrange its lifecycle so restoration is permitted and the configured SSH target becomes ready; no automatic start feature is provided. A failed restore or readiness check aborts the batch rather than continuing into a trial. Readiness uses the configured verified SSH backend. Do not interpret a successful readiness command as proof of isolation.

## Planning a new evaluation

Record software revision, dependency environment, model source/revision/hash, quantization, hardware settings, target OS, shell, installed tools, authorized scope, network controls, and snapshot lifecycle. Define task-specific evidence before execution and review both outputs and side effects afterward. Use consistent review criteria for partial, failed, refused, timed-out, and unverified runs.

Keep raw records privately; publish only deliberately reviewed, de-identified material. Release hardening checks validate current behavior, not the original thesis results.
