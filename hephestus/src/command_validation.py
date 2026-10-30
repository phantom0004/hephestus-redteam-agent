"""Parse proposals without evaluating any command on the controller."""

import base64
import functools
import os
from pathlib import Path
import shutil
import subprocess


@functools.lru_cache(maxsize=128)
def powershell_syntax_error(command):
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell and os.name == "nt":
        shell = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    if not shell:
        return "PowerShell parser is unavailable; cannot validate the proposal"
    parser = (
        "$source=[Console]::In.ReadToEnd(); $tokens=$null; $errors=$null; "
        "[void][System.Management.Automation.Language.Parser]::ParseInput($source,[ref]$tokens,[ref]$errors); "
        "if ($errors.Count) { [Console]::Out.Write(($errors | ForEach-Object {$_.Message}) -join '; '); exit 1 }"
    )
    encoded = base64.b64encode(parser.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(
            [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            input=command, text=True, encoding="utf-8", errors="replace", capture_output=True,
            timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return "PowerShell syntax validation could not finish"
    if result.returncode:
        return "Invalid PowerShell syntax: " + (result.stdout.strip() or "incomplete command")
    return None
