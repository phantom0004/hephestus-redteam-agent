"""Extract explicit shell proposals without rewriting shell semantics."""

import re


def extract_commands(model_text: str) -> list[str]:
    commands = []
    lines = model_text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        match = re.match(r"^(?:run:|cmd:|shell:)\s*(.*)$", line, re.IGNORECASE)
        command = match.group(1) if match else None
        if command is None and line.startswith("$ "):
            command = line[2:]
        if command is None and line.startswith(("sudo ", "python ", "bash ", "sh ")):
            command = line
        if command:
            while command.endswith(" \\") and index + 1 < len(lines):
                next_line = lines[index + 1].strip()
                if (
                    not next_line
                    or next_line.startswith("```")
                    or re.match(r"^(?:run:|cmd:|shell:|\$ )", next_line, re.IGNORECASE)
                ):
                    break
                index += 1
                command = command[:-1] + " " + lines[index].strip()
            if command.strip():
                commands.append(command.strip())
        index += 1
    return commands
