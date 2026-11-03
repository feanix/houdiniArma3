# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Plain-language text about a node, shared by the HTML reference and the in-Houdini Help section.

``scripts/build_user_docs.py`` (the HTML pages in docs/user/reference) and ``hda_source`` (the "Help" section of each
HDA) both read the node's ``NodeSpec`` through these helpers, so the two always say the same thing. This module does
not import ``hou``.
"""

import re

__all__ = ["NODE_INTRO", "KIND_TEXT", "FOLDER_TEXT", "number_text", "default_text", "type_text", "range_text",
           "collapse_inputs", "input_sentence", "split_menu_help", "menu_texts", "inputs_text", "wiki_escape",
           "condition_text", "parm_index_for"]

#: A short introduction for each node page, in plain words. The parameter list below it comes from the node's table.
NODE_INTRO = {
    "assemble_lods": (
        "A P3D file keeps several LODs (versions of the model for different viewing distances) together. After you "
        "edit LODs one at a time, this node puts them back together.",
        "Input 1 is optional: it takes the original P3D geometry, so that the LODs you did not touch stay as they "
        "were. Every other input takes one LOD geometry.",
    ),
    "extract_lod": (
        "P3D Import gives you each LOD of a model packed into one primitive. This node unpacks the LOD you pick, so "
        "you can edit its points and polygons like any other Houdini geometry.",
    ),
    "lod_properties": (
        "Every LOD has a type (for example Visual or Geometry), a resolution number and a list of named properties. "
        "This node sets them on the LOD geometry that comes in.",
    ),
    "mass": (
        "Arma needs a mass for each point of a Geometry LOD to work out how a vehicle or object moves. This node "
        "sets that mass in one of several ways.",
    ),
    "material_assign": (
        "A polygon in a P3D file has a texture and a material (an rvmat file). This node sets them for the polygons "
        "in a group, or for all polygons.",
    ),
    "p3d_export": (
        "This node is the end of a network. It checks the geometry the way Validate does and, when you press Save "
        "to Disk, writes a .p3d file you can use in Arma. The output is the same as the input, so you can keep "
        "working after it.",
        "The options are in tabs. A new node starts from the preset that is active in the Project root and presets "
        "tool. In Selection Mask, type group names separated by spaces; a name may use * and ?, and ^ before a name "
        "leaves it out. The menu lists the input's point groups: picking one adds it to the mask or removes it. The "
        "info line on the node says how many selections will be written.",
    ),
    "p3d_import": (
        "Use this node to bring an existing Arma 3 model into Houdini. A P3D file holds one or more LODs (versions "
        "of the model for different viewing distances). The node outputs one packed primitive for each LOD. Wire "
        "it into Extract LOD to edit one of them.",
    ),
    "proxy": (
        "A proxy marks a place where Arma puts another model, such as a weapon on a vehicle or a seat for a "
        "crew member. This node adds proxies to a LOD geometry.",
        "With one input, the node adds a single proxy. With a second input, it adds one proxy for each point of "
        "that input.",
    ),
    "validate": (
        "Run this node before you export. It looks for problems that Arma would reject or that break the model, "
        "and lists them as messages on the node. It does not change the geometry.",
    ),
}

#: Words for the parameter kinds.
KIND_TEXT = {
    "string": "Text",
    "file": "File path",
    "toggle": "On or off",
    "button": "Button",
    "int": "Whole number",
    "float": "Number",
    "menu": "Menu",
    "multiparm": "List of rows",
    "dir": "Folder path",
    "color": "Colour with alpha",
    "folder": "Group",
}

#: What the three folder looks are called for an artist.
FOLDER_TEXT = {"simple": "group box", "collapsible": "group box you can fold", "tabs": "tab"}

#: Characters that make Houdini's help wiki read the text as markup (bold, italics, links, keys, code, variables).
_WIKI_MARKUP = re.compile(r"[*`\[\]]|<<|>>|\(\(|\)\)|^_|_$|__|\{\{\{")


def wiki_escape(text):
    """Text for the Help section that the wiki shows exactly as written.

    The wiki has no backslash escape, but it shows the text of a code span (between backticks) as it is. So every run
    of words that would read as markup (``* ^__*``) goes into one code span. A backtick in the text becomes a quote."""
    parts = re.split(r"(\s+)", text.replace("`", "'"))
    out, i = [], 0
    while i < len(parts):
        if parts[i] and not parts[i].isspace() and _WIKI_MARKUP.search(parts[i]):
            end = i
            while end + 2 < len(parts) and _WIKI_MARKUP.search(parts[end + 2]):
                end += 2
            run = "".join(parts[i:end + 1])
            tail = re.search(r"[.,;:]+$", run)  # punctuation after the run is the sentence's, not the markup's
            if tail and tail.start() > 0:
                run, tail = run[:tail.start()], tail.group(0)
            else:
                tail = ""
            out.append("`" + run + "`" + tail)
            i = end + 1
        else:
            out.append(parts[i])
            i += 1
    return "".join(out)


def _value_words(parm, value):
    if parm is None:
        return value
    if parm.kind == "toggle":
        return "on" if value in ("1", "true", "True") else "off"
    for token, label in parm.items:
        if token == value:
            return label
    return value


def condition_text(index, conditions):
    """A ``show_when`` / ``enable_when`` in words: ``Mode is Uniform total or Set on each and Set Texture is on``.

    ``index`` maps parameter names to parameters (``NodeSpec.parm_index``, plus the row's own parameters)."""
    terms = []
    for name, values in conditions:
        parm = index.get(name)
        label = parm.label if parm is not None else name
        words = [_value_words(parm, value) for value in values]
        terms.append("%s is %s" % (label, " or ".join(words)))
    return " and ".join(terms)


def parm_index_for(spec, parm=None):
    """The parameters a condition may name: the node's, and (for a multiparm row parameter) the row's own."""
    index = dict(spec.parm_index)
    if parm is not None:
        index.update({child.name: child for child in parm.children})
    return index


def menu_texts(parm):
    """``(intro, {token: text})`` of a menu: the per-choice texts of ``item_help``, or the ones split out of its help.

    Every text starts with a capital letter."""
    intro, texts = split_menu_help(parm)
    texts = dict(texts)
    texts.update(dict(parm.item_help))
    return intro, {token: text[:1].upper() + text[1:] for token, text in texts.items() if text}


def number_text(value):
    value = float(value)
    return "%d" % value if value == int(value) else repr(value)


def default_text(parm):
    """The default of a parameter in words, or ``None`` for a button."""
    if parm.kind in ("button", "folder"):
        return None
    if parm.kind == "color":
        return "%s (red, green, blue, alpha)" % ", ".join(number_text(v) for v in parm.default)
    if parm.kind == "multiparm":
        return "%d rows" % int(parm.default)
    if parm.kind == "toggle":
        return "On" if parm.default else "Off"
    if parm.kind == "menu":
        for token, label in parm.items:
            if token == parm.default:
                return label
        return str(parm.default) if parm.default != "" else "(none)"
    if parm.size > 1:
        return ", ".join(number_text(v) if parm.kind in ("int", "float") else str(v) for v in parm.default)
    if parm.kind in ("int", "float"):
        return number_text(parm.default)
    return str(parm.default) if parm.default != "" else "(empty)"


def type_text(parm):
    text = KIND_TEXT[parm.kind]
    if parm.kind == "menu" and parm.menu_toggle:
        text = "Menu (pick several; each pick adds or removes an entry in the text)"
    if parm.kind == "folder":
        text = "Group (%s)" % FOLDER_TEXT[parm.folder_type]
    elif parm.size > 1 and parm.kind != "color":
        text = "%d %s" % (parm.size, "whole numbers" if parm.kind == "int" else "numbers")
    if parm.kind == "file" and parm.file_mode == "write":
        text += " (can name a new file)"
    return text


def range_text(parm):
    low, high = parm.minimum, parm.maximum
    if low is not None and high is not None:
        return "From %s to %s" % (number_text(low), number_text(high))
    if low is not None:
        return "At least %s; no upper limit" % number_text(low)
    if high is not None:
        return "At most %s; no lower limit" % number_text(high)
    return None


def collapse_inputs(spec):
    """Input labels as ``[(first number, last number, label)]``. Labels that only differ by "(input N)" join up."""
    groups = []
    for number, label in enumerate(spec.input_labels, start=1):
        base = re.sub(r" \(input \d+\)$", "", label)
        if groups and groups[-1][2] == base and re.search(r" \(input \d+\)$", label):
            groups[-1][1] = number
        else:
            groups.append([number, number, base])
    return groups


def input_sentence(spec, first):
    """The one-line description of input ``first`` (1-based): ``spec.input_help``, or the label as a sentence."""
    if first - 1 < len(spec.input_help) and spec.input_help[first - 1]:
        return spec.input_help[first - 1]
    return "Connect %s here." % spec.input_labels[first - 1]


def inputs_text(spec):
    """The input count in words: ``None``, ``2``, ``1 to 3``."""
    low, high = spec.inputs
    if low == high:
        return "%d" % low if low else "None"
    return "%d to %d" % (low, high)


def split_menu_help(parm):
    """Split a menu's help of the form ``Intro. Label A: text; Label B: text`` into ``(intro, {token: text})``.

    Some menus describe every choice inside the help text. If each choice label is found in order, the text of each
    choice goes with its item; otherwise the whole help is the intro and no item has a text."""
    help_text, positions, start = parm.help, [], 0
    for token, label in parm.items:
        found = help_text.find(label + ": ", start)
        if found < 0:
            return parm.help, {}
        positions.append(found)
        start = found + len(label) + 2
    if not positions:
        return parm.help, {}
    texts = {}
    for index, (token, label) in enumerate(parm.items):
        begin = positions[index] + len(label) + 2
        end = positions[index + 1] if index + 1 < len(positions) else len(help_text)
        texts[token] = help_text[begin:end].rstrip().rstrip(";").rstrip()
    return help_text[:positions[0]].strip(), texts
