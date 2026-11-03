"""PreToolUse guard: refuse a folder change chained to another command in a subagent's shell call.

Agents sometimes run 'cd X && ...' or 'cd X; ...' as one call. Claude Code's auto mode reads such chains as suspect,
and in Voxel Terrarium (2026-10-31) one locked an agent out of every later command. Only calls from a subagent (the
payload carries agent_id) are refused; lead and advisor sessions are not. A plain 'cd X' on its own passes.

Adapted from VoxelTerrarium/.claude/hooks/block-shell-slips.py (its cargo-test rule is VT-only and left out).
"""
import json
import re
import sys

# A shell command starts a segment after ; & | or a newline, so quoted words in a commit message are not a call.
SEG = r"(?:^|[;&|\n])\s*"
# cd / Set-Location / pushd to one folder, then another command after &&, ||, ; or a newline.
CD_CHAIN = re.compile(
    SEG + r"(?:cd|set-location|sl|pushd)\s+(?:\"[^\"]*\"|'[^']*'|[^\s&;|]+)\s*(?:&&|\|\||;|\n)\s*\S",
    re.IGNORECASE,
)

try:
    payload = json.load(sys.stdin)
    command = str(payload.get("tool_input", {}).get("command", ""))
    in_agent = bool(payload.get("agent_id"))
except (ValueError, AttributeError):
    # Not a shell tool payload we understand; leave the decision to the other hooks.
    sys.exit(0)

if in_agent and CD_CHAIN.search(command):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "One simple command per call: no 'cd X && ...' or 'cd X; ...'. Use git -C <dir>, absolute paths "
                "or a tool's own directory flag, and run each command as its own call. This refusal is for this "
                "command only: do not rephrase it to get past this check."
            ),
        }
    }))
