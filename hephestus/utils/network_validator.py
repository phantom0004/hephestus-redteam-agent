"""Optional host connectivity observations; these never prove target isolation."""

import os
import subprocess
import urllib.error
import urllib.request


def _command_check(name, command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        reachable = result.returncode == 0
        return dict(
            check=name,
            passed=not reachable,
            status="reachable" if reachable else "failed",
            detail="Connectivity observed"
            if reachable
            else "Probe failed; isolation is not established",
        )
    except (OSError, subprocess.TimeoutExpired):
        return dict(
            check=name,
            passed=False,
            status="unknown",
            detail="Tool unavailable or timed out; cannot conclude isolation",
        )


def _check_ping():
    command = (
        ["ping", "-n", "1", "-w", "3000", "8.8.8.8"]
        if os.name == "nt"
        else ["ping", "-c", "1", "8.8.8.8"]
    )
    return _command_check("ping", command)


def _check_dns():
    return _command_check("dns", ["nslookup", "example.com"])


def _check_http():
    try:
        with urllib.request.urlopen("https://example.com", timeout=3):
            return dict(
                check="https",
                passed=False,
                status="reachable",
                detail="External HTTPS reachable from agent host",
            )
    except urllib.error.HTTPError:
        return dict(
            check="https",
            passed=False,
            status="reachable",
            detail="External HTTPS server responded",
        )
    except (OSError, urllib.error.URLError):
        return dict(
            check="https",
            passed=True,
            status="failed",
            detail="Probe failed; target isolation is not established",
        )


def validate_isolation(logger=None):
    checks = [_check_ping(), _check_dns(), _check_http()]
    result = dict(
        isolated=False,
        checks=checks,
        external_connectivity=any(c["status"] == "reachable" for c in checks),
        reason="Host connectivity observations only. Verify VM/network egress controls independently.",
    )
    if logger:
        logger.log("network_validation", result)
    return result
