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


_MAX_DETAIL = 80


def format_invocation(tool_name: str, tool_input: dict | None) -> str:
    """Compact one-shot summary of a tool call, e.g. for an Approve label.

    Shows the actual command/path/pattern, unlike `format_rule` which
    widens to a pattern.
    """
    tool_input = tool_input or {}
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", "")).strip()
        if cmd:
            return f"Bash({_truncate(cmd)})"
        return "Bash"
    if tool_name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "").strip()
        if path:
            return f"{tool_name}({_truncate(path)})"
        return tool_name
    if tool_name in ("Glob", "Grep"):
        pattern = str(tool_input.get("pattern", "")).strip()
        if pattern:
            return f"{tool_name}({_truncate(pattern)})"
        return tool_name
    return tool_name


def _truncate(s: str) -> str:
    return s if len(s) <= _MAX_DETAIL else s[: _MAX_DETAIL - 1] + "\u2026"
