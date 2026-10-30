"""Decode whole proposals without repairing or rewriting executable text.

A model reply may contain one or more fenced shell blocks, optionally with
prose around them. Blocks are found with a line-based parser rather than a
regular expression so that a fence-like line inside a PowerShell here-string
(for example a Markdown file being written) never ends the block early. A block
that is never closed means the generation was cut off, so the whole reply is
rejected; nothing is stitched together from fragments.
"""

import json
import re

# Most commands a single proposal may contain; each runs as its own step.
MAX_COMMANDS_PER_PROPOSAL = 6
SHELL_LANGUAGES = {"powershell", "pwsh", "ps1", "ps", "posh", "sh", "bash", "shell", "zsh", "console", ""}
_OPEN_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*([A-Za-z0-9_+-]*)[ \t]*$")
_HERE_OPEN = re.compile(r"@(['\"])[ \t]*$")
_PROMPT_PREFIX = re.compile(r"^(?:PS [^>\n]{0,200}> |\$ )")

# Placeholders and elisions a model writes when it abbreviates instead of
# producing the whole command. Any of these means the proposal is unusable.
_TRUNCATION_MARKERS = [
    (re.compile(r"\.\.\.|…"), "an ellipsis"),
    (re.compile(r"<\s*(?:incomplete|truncated|omitted|placeholder|insert|your|rest|remaining|more|command|path|value|todo|snip)[^<>\n]{0,40}>", re.I),
     "an angle-bracket placeholder"),
    (re.compile(r"\[\s*(?:truncated|omitted|snip(?:ped)?|rest of|remaining|content|more)[^\]\n]{0,40}\]", re.I),
     "a bracketed omission"),
    (re.compile(r"#\s*(?:rest of|remaining (?:code|lines|content|commands|steps)|and so on|same as above|"
                r"(?:code|content|output|lines) (?:omitted|truncated)|etc\.?\s*$)", re.I | re.M),
     "an omission comment"),
]


_FP_BACKTICK = re.compile(r"`+")
_FP_WHITESPACE = re.compile(r"\s+")
_FP_PAREN_WRAP = re.compile(r"\(([^()]+)\)")
_FP_QUOTE_STYLE = re.compile(r"""['"]""")


def command_fingerprint(command: str) -> str:
    """Semantic fingerprint for near-duplicate detection across replans.

    A local model under temperature noise often resubmits the same injection
    pattern with trivial surface shuffles — a backtick added before a quote,
    ``(Get-Date ...)`` instead of ``Get-Date ...``, single vs double quotes,
    an extra space, lowercase vs mixed case cmdlets. Byte equality misses all
    of these and the duplicate-skip never fires, so the loop keeps iterating.

    Normalisation here collapses:
      - case (PowerShell / cmd are case-insensitive),
      - all whitespace runs to a single space,
      - PowerShell escape backticks,
      - redundant single-expression parenthesisation,
      - quote style (``'`` vs ``"``) around identical content.

    The raw command text is kept everywhere else; this value is only used to
    compare proposals in ``memory.attempted``.
    """
    if not isinstance(command, str):
        return ""
    text = command.strip().lower()
    text = _FP_BACKTICK.sub("", text)
    text = _FP_QUOTE_STYLE.sub('"', text)
    # Peel one layer of (expr) wrappers; two passes catches ((expr)) too.
    text = _FP_PAREN_WRAP.sub(r"\1", text)
    text = _FP_PAREN_WRAP.sub(r"\1", text)
    text = _FP_WHITESPACE.sub(" ", text)
    return text.strip()


def truncation_problem(command: str):
    """Describe why a command looks abbreviated or cut off, or return None."""
    if not isinstance(command, str) or not command.strip():
        return "the command is empty"
    if command.strip().splitlines()[0].strip().lower() in SHELL_LANGUAGES - {""}:
        return "a misplaced shell language label; put the language on the opening code fence, not inside the command"
    for pattern, label in _TRUNCATION_MARKERS:
        if pattern.search(command):
            return f"the command contains {label}, so part of it was left out"
    stripped = command.rstrip()
    if stripped.endswith(("|", "`", "-and", "-or", "&&", "||", "{", "(")):
        return "the command stops mid-expression"
    for opener, closer in (("@'", "'@"), ('@"', '"@')):
        if command.count(opener) > command.count(closer):
            return f"the here-string opens {opener} without a closing {closer}"
    return None


def _strip_prompts(body):
    """Drop copied shell prompts ("PS C:\\> ", "$ ") that a model sometimes prepends."""
    lines = body.split("\n")
    if lines and all(_PROMPT_PREFIX.match(line) for line in lines if line.strip()):
        lines = [_PROMPT_PREFIX.sub("", line, count=1) for line in lines]
    return "\n".join(lines)


def parse_blocks(model_text: str):
    """Return (blocks, problem). ``blocks`` are (language, body) in order.

    ``problem`` is a description when the reply is structurally unusable (for
    example an unclosed fence), else None.
    """
    lines = str(model_text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks, index = [], 0
    while index < len(lines):
        opened = _OPEN_FENCE.match(lines[index])
        if not opened:
            index += 1
            continue
        fence, language = opened.group(1), opened.group(2).lower()
        body, here_closer, index = [], None, index + 1
        closed = False
        while index < len(lines):
            line = lines[index]
            if here_closer is None:
                stripped = line.strip()
                if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
                    closed = True
                    index += 1
                    break
                match = _HERE_OPEN.search(line)
                if match and language in {"powershell", "pwsh", "ps1", "ps", "posh", ""}:
                    here_closer = match.group(1) + "@"
            elif line.startswith(here_closer):
                here_closer = None
            body.append(line)
            index += 1
        if not closed:
            return blocks, "a code block was opened but never closed (the reply was cut off)"
        blocks.append((language, "\n".join(body).strip("\n")))
    return blocks, None


def extract_commands(model_text: str) -> list[str]:
    """Every complete shell block in order, TASK_COMPLETE alone, or legacy strict JSON."""
    if not isinstance(model_text, str):
        return []
    text = model_text.strip()
    if text == "TASK_COMPLETE":
        return [text]
    blocks, problem = parse_blocks(text)
    if problem:
        return []
    shell = [body for language, body in blocks if language in SHELL_LANGUAGES and body.strip()]
    if shell:
        commands = [_strip_prompts(body) for body in shell]
        # A bare TASK_COMPLETE inside a block is still a completion signal.
        return [c.strip() if c.strip() == "TASK_COMPLETE" else c for c in commands]
    json_blocks = [body for language, body in blocks if language == "json"]
    candidate = json_blocks[0] if len(json_blocks) == 1 else text
    try:
        data = json.loads(candidate)
    except ValueError:
        return []
    if isinstance(data, dict) and set(data) == {"command"}:
        command = data["command"]
        return [command] if isinstance(command, str) and command.strip() else []
    if isinstance(data, dict) and set(data) == {"commands"} and isinstance(data["commands"], list):
        return [c for c in data["commands"] if isinstance(c, str) and c.strip()]
    return []
