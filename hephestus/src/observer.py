import re


def _extract_info(command: str, stdout: str, exit_code: int) -> dict[str, object]:
    """Extract structured information based on the type of command that was run.

    Instead of feeding the LLM raw terminal output (which can be very long),
    this function pulls out the most relevant facts per tool:
      - nmap   â†’ list of open ports
      - ls/dir â†’ file count + suspicious filenames (flag, password, etc.)
      - cat    â†’ first/last 3 lines for long files (avoids huge payloads)
      - netstatâ†’ count of listening ports
      - whoami â†’ extracted username
      - ps     â†’ process count

    The extracted info is appended to the observation dict so the LLM
    receives a compact, high-signal summary rather than raw output.
    """
    info = {}
    cmd_lower = command.strip().split()[0].lower() if command.strip() else ""

    if cmd_lower == "nmap":
        # Regex captures lines like "22/tcp open ssh"
        ports = re.findall(r"(\d+/tcp\s+open\s+\S+)", stdout)
        if ports:
            info["open_ports"] = ports

    elif cmd_lower in ("ls", "dir"):
        lines = [line for line in stdout.strip().splitlines() if line.strip()]
        info["file_count"] = len(lines)
        # Flag files whose names hint at sensitive content
        suspicious = [
            line
            for line in lines
            if any(
                k in line.lower()
                for k in ("flag", "password", "shadow", "secret", "key", ".bak")
            )
        ]
        if suspicious:
            info["suspicious_files"] = suspicious

    elif cmd_lower in ("cat", "type"):
        lines = stdout.strip().splitlines()
        # For long files, only keep the head and tail to save tokens
        if len(lines) > 6:
            info["first_lines"] = lines[:3]
            info["last_lines"] = lines[-3:]
            info["total_lines"] = len(lines)
        elif lines:
            info["content_lines"] = len(lines)

    elif cmd_lower == "netstat":
        listening = re.findall(r".*LISTEN.*", stdout)
        if listening:
            info["listening_ports"] = len(listening)

    elif cmd_lower in ("whoami", "id"):
        info["username"] = stdout.strip().splitlines()[0] if stdout.strip() else ""

    elif cmd_lower in ("ps", "tasklist"):
        lines = [line for line in stdout.strip().splitlines() if line.strip()]
        info["process_count"] = max(0, len(lines) - 1)  # subtract header row

    return info


def summarise_result(
    command: str, stdout: str, stderr: str, exit_code: int
) -> dict[str, object]:
    """Summarize command execution result with smart extraction."""
    extracted = _extract_info(command, stdout, exit_code)

    result = {
        "command": command,
        "exit_code": exit_code,
        "stdout_preview": stdout.strip()[:300],
        "stderr_preview": stderr.strip()[:300],
    }

    if extracted:
        result["extracted_info"] = extracted

    return result
