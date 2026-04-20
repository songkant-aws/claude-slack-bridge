"""Build CC permission-rule parameters for the Slack Trust button.

CC's PermissionRequest hook can return an `updatedPermissions`
directive with `addRules` → `{"toolName": ..., "ruleContent": ...}`.
This module maps a concrete tool call to that pair.
"""
from __future__ import annotations

import shlex
from pathlib import Path


def build_allow_rule(tool_name: str, tool_input: dict | None) -> tuple[str, str | None]:
    """Return (toolName, ruleContent) for the current tool invocation.

    Heuristics mirror how CC's own "Yes, don't ask again" button would
    phrase the rule:
    - Bash: first token as prefix, e.g. `sudo -n uptime` → ("Bash", "sudo:*").
    - Write / Edit / MultiEdit / Read / NotebookEdit: directory of the
      referenced file, e.g. `/tmp/foo.py` → ("Write", "/tmp/**").
    - Everything else: whole tool, ruleContent = None.
    """
    tool_input = tool_input or {}
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", "")).strip()
        if cmd:
            try:
                first = shlex.split(cmd)[0]
            except ValueError:
                first = cmd.split()[0]
            if first:
                return ("Bash", f"{first}:*")
        return ("Bash", None)
    if tool_name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "").strip()
        if path:
            directory = str(Path(path).parent)
            if directory and directory != "/":
                return (tool_name, f"{directory}/**")
        return (tool_name, None)
    return (tool_name, None)


def format_rule(tool_name: str, rule_content: str | None) -> str:
    """Human-readable rendering, e.g. `Bash(sudo:*)` or `TodoWrite`."""
    if rule_content:
        return f"{tool_name}({rule_content})"
    return tool_name
