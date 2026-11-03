# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""What the Validate node and the P3D Export node share (ux-tools.md 5.8 and 5.9): the check parameters, the options
they make, the error level and the one function that runs the checks.

Both nodes call ``run_checks``, which calls the exporter's ``run`` once. That is why they give the same messages for
the same input. The module name starts with ``_`` so ``nodes.KINDS`` does not list it.
"""

from dataclasses import dataclass
from typing import Dict, List, Tuple

from .. import messages, p3d_export
from ..errors import UserError
from ..messages import ERROR, WARNING, Report, format_problem
from ..p3d_export import ExportOptions
from ..p3d_export.options import DEFAULT_SELECTION_MASK, DUPLICATES
from ..p3d_export.type_rules import RULES
from ..params import Parm

__all__ = [
    "LEVELS", "level_parms", "rule_parms", "checking_tab", "rules_tab", "rule_parm_name", "options_from", "apply_policy", "is_blocked", "Checked",
    "run_checks", "input_geometry", "DEFAULT_MAX_MESSAGES",
]

DEFAULT_MAX_MESSAGES = messages.MAX_MESSAGES
LEVELS = (("error", "Error (stop here)"), ("warning", "Warning (keep geometry)"))
_DUPES = tuple((token, token.capitalize()) for token in DUPLICATES)


def rule_parm_name(rule_id: str) -> str:
    """``T2-PC-EDGE`` -> ``t2pcedge``."""
    return rule_id.lower().replace("-", "")


def level_parms() -> Tuple[Parm, ...]:
    """Errors show as, Warnings count as errors, Duplicate LODs, Max messages (ux-tools.md 5.8)."""
    return (
        Parm("errorlevel", "Errors Show As", "menu", "error",
             "Error (stop here): the node turns red and the nodes below it stop. Warning (keep geometry): the node "
             "turns yellow and the geometry keeps flowing; the text still starts with Error:. Saving stops on errors "
             "either way.", items=LEVELS),
        Parm("strict", "Warnings Count As Errors", "toggle", False,
             "Treat every warning as an error. Off by default: a warning can be what you meant."),
        Parm("dupes", "Duplicate LODs", "menu", "fail",
             "What to do with a LOD whose type and resolution were already written: Fail, Skip the later one, or "
             "Ignore and write both.", items=_DUPES),
        Parm("maxmessages", "Max Messages", "int", DEFAULT_MAX_MESSAGES,
             "How many messages the node shows. The rest are counted in one line and listed in the console.",
             minimum=1),
    )


def rule_parms() -> Tuple[Parm, ...]:
    """The master switch and one switch per tier-2 rule (spec 17.1.1, on except the two of D24)."""
    parms = [Parm("typerules", "Check LOD-Type Rules", "toggle", True,
                  "Warn about what Object Builder's validator would: rules for geometry, shadow, roadway and other "
                  "LOD types. Each rule has its own switch below.")]
    for rule in RULES:
        # The rule id is not in the label; it ends the tooltip, and stays in the messages, so it can be searched for.
        parms.append(Parm(rule_parm_name(rule.id), rule.label, "toggle", rule.default_on,
                          "%s (Rule %s.)" % (rule.help, rule.id)))
    return tuple(parms)


def checking_tab(*first: Parm) -> Parm:
    """The "Error Checking" tab: ``first`` (P3D Export's Check menu), then the level parameters. Both nodes use it."""
    return Parm("tab_checking", "Error Checking", "folder", folder_type="tabs", children=tuple(first) + level_parms())


def rules_tab() -> Parm:
    """The "LOD Rules" tab: the master switch and one switch per rule. Both nodes use it."""
    return Parm("tab_rules", "LOD Rules", "folder", folder_type="tabs", children=rule_parms())


def options_from(values: Dict[str, object]) -> ExportOptions:
    """The exporter options from a node's parameter values. A node that lacks a parameter gets the default."""
    rules_off: List[str] = []
    if not values.get("typerules", True):
        rules_off = [rule.id for rule in RULES]
    else:
        rules_off = [rule.id for rule in RULES if not values.get(rule_parm_name(rule.id), rule.default_on)]
    return ExportOptions(
        selection_mask=str(values.get("selmask", DEFAULT_SELECTION_MASK)),
        lowercase=bool(values.get("lowercase", False)),
        relative_paths=bool(values.get("relpaths", False)),
        project_root=str(values.get("projectroot", "")),
        sort_sections=bool(values.get("sortsections", False)),
        sort_lods=bool(values.get("sortlods", False)),
        auto_components=bool(values.get("autocomponents", False)),
        duplicate_lods=str(values.get("dupes", "fail")),
        renumber_components=bool(values.get("renumber", False)),
        czech_names=bool(values.get("czech", False)),
        drop_uvs=bool(values.get("dropuvs", False)),
        rules_off=tuple(rules_off),
    )


def is_blocked(items, strict: bool) -> bool:
    """True when saving must stop: an error, or a warning while warnings count as errors (Q-13)."""
    return any(kind == ERROR or (strict and kind == WARNING) for kind, _ in items)


def apply_policy(items, level: str, strict: bool) -> List[Tuple[str, str]]:
    """The messages as the node shows them (ux-tools.md 5.8). With ``strict`` a warning becomes an error. With the level
    ``warning`` an error is shown as a warning and its text starts with ``Error:``."""
    result: List[Tuple[str, str]] = []
    for kind, text in items:
        if kind == WARNING and strict:
            kind, text = ERROR, "Warning as error: " + text
        if kind == ERROR and level == "warning":
            kind = WARNING
            if not text.startswith("Warning as error: "):
                text = "Error: " + text
        result.append((kind, text))
    return result


@dataclass
class Checked:
    """The result of ``run_checks``."""

    result: p3d_export.BuildResult
    #: The messages before the policy: what saving looks at.
    raw: List[Tuple[str, str]]

    @property
    def errors(self) -> int:
        return sum(1 for kind, _ in self.raw if kind == ERROR)

    @property
    def warnings(self) -> int:
        return sum(1 for kind, _ in self.raw if kind == WARNING)


def input_geometry(node, name: str):
    geo = node.inputGeometry(0)
    if geo is None:
        raise UserError(format_problem(name, "there is no input", "Connect a P3D geometry or a LOD geometry"))
    return geo


def show(report: Report, items, values: Dict[str, object]) -> None:
    """Put raw messages into the node's report through the error level and strict switch, and set the limit."""
    report.max_messages = int(values.get("maxmessages", DEFAULT_MAX_MESSAGES))
    for kind, text in apply_policy(items, str(values.get("errorlevel", "error")), bool(values.get("strict", False))):
        report.items.append((kind, text))


def run_checks(node, values: Dict[str, object], report: Report, name: str) -> Checked:
    """Collect and build the node's input with the node's options; put the messages on ``report``."""
    geo = input_geometry(node, name)
    scratch = Report()
    result = p3d_export.run([geo], options_from(values), scratch)
    show(report, scratch.items, values)
    return Checked(result, list(scratch.items))
