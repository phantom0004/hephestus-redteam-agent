"""Compact command results into high-signal observations for the local model.

The model never sees raw multi-megabyte tool output: that is captured for the
operator (bounded by ``OUTPUT_MAX_CHARS``) and exported separately. What the
model receives is a bounded window of each command's output, sized by
``OBSERVATION_MAX_CHARS``, plus a few structured facts for common tools. For
long output the window keeps the head and the tail, because the decisive
evidence in security tooling is often at the end (summaries, prompts, errors).
"""

import re

from hephestus import config


def _clip(text, limit):
    """Return (clipped_text, truncated) keeping the head and tail of long text."""
    text = text.strip()
    if len(text) <= limit:
        return text, False
    head_len = max(1, limit * 60 // 100)
    tail_len = max(1, limit - head_len)
    omitted = len(text) - head_len - tail_len
    # No ellipsis characters: a model that sees "…" in its context tends to copy
    # that style into commands, which then fail as abbreviated.
    middle = f"\n-- controller note: {omitted} characters of output not shown here (kept for the operator) --\n"
    return text[:head_len] + middle + text[-tail_len:], True


def _extract_info(command: str, stdout: str) -> dict[str, object]:
    """Pull structured, low-token facts for common reconnaissance tools.

    These hints supplement (never replace) the output window:
      - nmap / masscan   -> open ports
      - ls / dir / gci   -> file count + names hinting at sensitive content
      - cat / type / gc  -> line count for long files
      - netstat          -> count of listening sockets
      - whoami / id      -> the resolved identity
      - ps / tasklist    -> process count
    Unknown tools simply get no extra hints; the output window still applies.
    """
    info: dict[str, object] = {}
    cmd_lower = command.strip().split()[0].lower() if command.strip() else ""

    if cmd_lower in ("nmap", "masscan"):
        ports = re.findall(r"(\d+/(?:tcp|udp)\s+open\s+\S+)", stdout)
        if ports:
            info["open_ports"] = ports[:40]

    elif cmd_lower in ("ls", "dir", "get-childitem", "gci"):
        lines = [line for line in stdout.strip().splitlines() if line.strip()]
        info["file_count"] = len(lines)
        suspicious = [
            line
            for line in lines
            if any(
                k in line.lower()
                for k in ("flag", "password", "passwd", "shadow", "secret",
                          "cred", "key", "token", ".bak", "config", ".kdbx")
            )
        ]
        if suspicious:
            info["notable_entries"] = suspicious[:20]

    elif cmd_lower in ("cat", "type", "get-content", "gc", "more", "head", "tail"):
        lines = stdout.strip().splitlines()
        if len(lines) > 6:
            info["total_lines"] = len(lines)

    elif cmd_lower == "netstat":
        listening = re.findall(r".*LISTEN.*", stdout)
        if listening:
            info["listening_sockets"] = len(listening)

    elif cmd_lower in ("whoami", "id"):
        first = stdout.strip().splitlines()
        info["identity"] = first[0] if first else ""

    elif cmd_lower in ("ps", "tasklist", "get-process"):
        lines = [line for line in stdout.strip().splitlines() if line.strip()]
        info["process_count"] = max(0, len(lines) - 1)  # drop the header row

    return info


def summarise_result(
    command: str, stdout: str, stderr: str, exit_code: int
) -> dict[str, object]:
    """Summarize a command result into a bounded, high-signal observation."""
    limit = max(200, int(getattr(config, "OBSERVATION_MAX_CHARS", 2400)))
    stdout_window, out_trunc = _clip(stdout, limit)
    stderr_window, err_trunc = _clip(stderr, max(200, limit // 2))

    result: dict[str, object] = {
        "command": command,
        "exit_code": exit_code,
        "stdout_preview": stdout_window,
        "stderr_preview": stderr_window,
    }
    if out_trunc or err_trunc:
        result["output_truncated"] = True
        result["stdout_total_chars"] = len(stdout)
        result["stderr_total_chars"] = len(stderr)

    extracted = _extract_info(command, stdout)
    if extracted:
        result["extracted_info"] = extracted

    return result
