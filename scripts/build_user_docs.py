"""Build the generated parts of the user docs in docs/user/.

    python scripts/build_user_docs.py            write the generated files
    python scripts/build_user_docs.py --check    change nothing; exit 1 if a checked-in file is out of date

What it writes:

- ``reference/<node type name>.html``: one page per node, made from the node's parameter table (``SPEC``).
- ``reference/tool-<tool name>.html``: one page per shelf tool, made from the ``.shelf`` files in houdini/toolbar.
- ``reference/index.html`` and ``about.html``.
- The navigation bar and the footer of the hand-written pages (``index.html`` and the four guides). Only the text
  between the ``nav:start`` / ``nav:end`` and ``footer:start`` / ``footer:end`` marker comments is replaced. The rest
  of those pages is for people to write.

``style.css`` and the hand-written pages are never overwritten. The page layout is described in docs/user/README.md.

The output has no dates and a fixed order, so running the script twice gives the same files. Standard library only.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import glob
import html
import os
import re
import sys
import xml.etree.ElementTree as ElementTree

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(REPO_ROOT, "docs", "user")
SHELF_GLOB = os.path.join(REPO_ROOT, "houdini", "toolbar", "*.shelf")

#: Pages written by people. The script only keeps their navigation bar and footer up to date.
HAND_PAGES = ("index.html", "install.html", "quick-start-arma.html", "quick-start-houdini.html", "concepts.html")

#: The navigation bar: (path from docs/user, label). The same list is on every page.
NAV = (
    ("index.html", "Home"),
    ("install.html", "Install"),
    ("quick-start-arma.html", "Quick start: Arma modders"),
    ("quick-start-houdini.html", "Quick start: Houdini users"),
    ("concepts.html", "Concepts"),
    ("reference/index.html", "Reference"),
    ("about.html", "About"),
)

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

#: A short introduction for each shelf tool page.
TOOL_INTRO = {
    "a3ob_batch_convert": (
        "Use this to check or update many models at once. It reads every .p3d file in a folder, writes each one "
        "again to an output folder, and reports whether the new file is identical, different or failed.",
    ),
    "a3ob_new_p3d": (
        "Use this to start a new model. It builds a small network for you: two LODs, a node to put them together "
        "and a node to export.",
    ),
    "a3ob_project_settings": (
        "Use this to tell the tools where your Arma project lives and to manage export presets. A preset is a "
        "saved set of export options that you can pick again later.",
    ),
    "a3ob_quick_export": (
        "Use this for a fast export without adding a node to your network. Pick a preset and a file name.",
    ),
    "a3ob_reload": (
        "Use this after you update the add-on. It loads the new code without restarting Houdini.",
    ),
    "a3ob_selection_browser": (
        "In a P3D file, a selection (a named set of points or faces) has a name that Houdini group names cannot "
        "always hold as typed. This tool lists the groups of the selected node by their P3D name.",
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
}


# ---------------------------------------------------------------------------------------------------------------
# Page frame


def esc(text):
    return html.escape(str(text), quote=True)


def prefix_for(relative_path):
    """``../`` for each folder level of a page path inside docs/user."""
    return "../" * relative_path.count("/")


def nav_html(relative_path):
    """The navigation bar for a page. The page's own entry is marked as the current page."""
    prefix = prefix_for(relative_path)
    section = relative_path.startswith("reference/")
    lines = ['<nav class="site" aria-label="Main">', "<ul>"]
    for href, label in NAV:
        current = href == relative_path or (section and href == "reference/index.html")
        mark = ' aria-current="page"' if current else ""
        lines.append('<li><a href="%s%s"%s>%s</a></li>' % (prefix, href, mark, esc(label)))
    lines += ["</ul>", "</nav>"]
    return "\n".join(lines)


def footer_html(relative_path):
    prefix = prefix_for(relative_path)
    return "\n".join([
        "<p>Houdini tools for Arma 3 models. Based on Arma 3 Object Builder by MrClock, "
        "which continues ArmAToolbox by Alwarren.</p>",
        '<p>Licence: GPL-3.0-or-later. <a href="%sabout.html">About these tools and credits</a>.</p>' % prefix,
    ])


def header_block(relative_path):
    prefix = prefix_for(relative_path)
    return "\n".join([
        '<header class="site">',
        '<div class="inner">',
        '<a class="site-title" href="%sindex.html">A3OB for Houdini</a>' % prefix,
        "<!-- nav:start -->",
        nav_html(relative_path),
        "<!-- nav:end -->",
        "</div>",
        "</header>",
    ])


def footer_block(relative_path):
    return "\n".join([
        '<footer class="site">',
        '<div class="inner">',
        "<!-- footer:start -->",
        footer_html(relative_path),
        "<!-- footer:end -->",
        "</div>",
        "</footer>",
    ])


def page(relative_path, title, body):
    """A whole generated page. ``body`` is the HTML that goes inside ``<main>``."""
    prefix = prefix_for(relative_path)
    return "\n".join([
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>%s - A3OB for Houdini</title>" % esc(title),
        '<link rel="stylesheet" href="%sstyle.css">' % prefix,
        "</head>",
        "<body>",
        header_block(relative_path),
        "<main>",
        body.rstrip("\n"),
        "</main>",
        footer_block(relative_path),
        "</body>",
        "</html>",
        "",
    ])


def refresh_hand_page(relative_path, text):
    """Replace the navigation bar and the footer text of a hand-written page; keep the rest."""
    for name, new in (("nav", nav_html(relative_path)), ("footer", footer_html(relative_path))):
        pattern = re.compile(r"(<!-- %s:start -->\n).*?(\n<!-- %s:end -->)" % (name, name), re.S)
        if not pattern.search(text):
            raise ValueError("%s: the <!-- %s:start --> and <!-- %s:end --> markers are missing" % (
                relative_path, name, name))
        text = pattern.sub(lambda match: match.group(1) + new + match.group(2), text, count=1)
    return text


# ---------------------------------------------------------------------------------------------------------------
# Text helpers


def paragraphs(texts):
    return "\n".join("<p>%s</p>" % esc(text) for text in texts)


def number_text(value):
    value = float(value)
    return "%d" % value if value == int(value) else repr(value)


def default_text(parm):
    """The default of a parameter in words, or ``None`` for a button."""
    if parm.kind == "button":
        return None
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
    if parm.size > 1:
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


# ---------------------------------------------------------------------------------------------------------------
# Node pages


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


def parm_html(parm, id_prefix=""):
    """One ``<section class="parm">`` for a parameter."""
    anchor = id_prefix + parm.name.replace("#", "")
    lines = ['<section class="parm" id="%s">' % esc(anchor),
             "<h3>%s <code>%s</code></h3>" % (esc(parm.label), esc(parm.name))]
    intro, item_text = (split_menu_help(parm) if parm.kind == "menu" and parm.items else (parm.help, {}))
    if intro:
        lines.append("<p>%s</p>" % esc(intro))
    facts = [("Type", type_text(parm))]
    default = default_text(parm)
    if default is not None:
        facts.append(("Default", default))
    allowed = range_text(parm)
    if allowed:
        facts.append(("Allowed", allowed))
    if parm.kind == "file" and parm.file_pattern:
        facts.append(("Files", parm.file_pattern))
    lines.append("<dl>")
    for name, value in facts:
        lines.append("<dt>%s</dt><dd>%s</dd>" % (esc(name), esc(value)))
    lines.append("</dl>")
    if parm.kind == "menu" and parm.items:
        lines.append("<p>Choices:</p>")
        lines.append("<ul>")
        for token, label in parm.items:
            more = ": %s" % esc(item_text[token]) if item_text.get(token) else ""
            lines.append("<li>%s <code>%s</code>%s</li>" % (esc(label), esc(token), more))
        lines.append("</ul>")
    elif parm.kind == "menu":
        lines.append("<p>The choices are made when you open the menu, from the geometry or the settings.</p>")
    if parm.kind == "multiparm":
        lines.append("<p>Each row has these parameters. The number in the row replaces the <code>#</code> in the "
                     "name.</p>")
        lines.append('<div class="rows">')
        lines.extend(parm_html(child, anchor + "-") for child in parm.children)
        lines.append("</div>")
    lines.append("</section>")
    return "\n".join(lines)


def node_page(spec):
    relative = "reference/%s.html" % spec.type_name
    lines = ['<p class="crumbs"><a href="index.html">Reference</a> / Nodes</p>', "<h1>%s</h1>" % esc(spec.label),
             '<p class="lead">%s</p>' % esc(spec.summary)]
    lines.append(paragraphs(NODE_INTRO.get(spec.kind, ())))
    low, high = spec.inputs
    if low == high:
        inputs = "%d" % low if low else "None"
    else:
        inputs = "%d to %d" % (low, high)
    lines += ['<table class="facts">',
              "<tr><th>Houdini name</th><td><code>%s</code></td></tr>" % esc(spec.type_name),
              "<tr><th>Where to find it</th><td>Tab menu, %s, %s</td></tr>" % (esc(spec.submenu), esc(spec.label)),
              "<tr><th>Inputs</th><td>%s</td></tr>" % esc(inputs),
              "</table>"]
    groups = collapse_inputs(spec)
    if groups:
        lines += ["<h2>Inputs</h2>", "<ol>"]
        for first, last, label in groups:
            where = "Input %d" % first if first == last else "Inputs %d to %d" % (first, last)
            lines.append("<li value=\"%d\">%s: %s</li>" % (first, esc(where), esc(label)))
        lines.append("</ol>")
        if low == 0:
            lines.append('<p class="note">The node also works with no input connected.</p>')
    lines.append("<h2>Parameters</h2>")
    lines.append("<p>Parameters are in the order they appear on the node.</p>")
    lines.extend(parm_html(parm) for parm in spec.parms)
    return relative, page(relative, spec.label, "\n".join(lines))


# ---------------------------------------------------------------------------------------------------------------
# Shelf tool pages


def read_tools():
    """``[(name, label, help text)]`` for every tool in the .shelf files, in shelf order."""
    tools = {}
    order = []
    for path in sorted(glob.glob(SHELF_GLOB)):
        root = ElementTree.parse(path).getroot()
        for shelf in root.findall("toolshelf"):
            order.extend(member.get("name") for member in shelf.findall("memberTool"))
        for tool in root.findall("tool"):
            help_text = (tool.findtext("helpText") or "").strip()
            help_text = re.sub(r"\s*The work is in [\w/.]+\.py\.?", "", help_text)
            tools[tool.get("name")] = (tool.get("label") or tool.get("name"), help_text)
    names = [name for name in order if name in tools] + sorted(name for name in tools if name not in order)
    seen = set()
    result = []
    for name in names:
        if name not in seen:
            seen.add(name)
            result.append((name, tools[name][0], tools[name][1]))
    return result


def tool_page_path(name):
    return "reference/tool-%s.html" % name


def tool_page(name, label, help_text):
    relative = tool_page_path(name)
    lines = ['<p class="crumbs"><a href="index.html">Reference</a> / Shelf tools</p>', "<h1>%s</h1>" % esc(label),
             '<p class="lead">%s</p>' % esc(help_text)]
    lines.append(paragraphs(TOOL_INTRO.get(name, ())))
    lines += ['<table class="facts">',
              "<tr><th>Where to find it</th><td>The A3OB shelf, button <strong>%s</strong></td></tr>" % esc(label),
              "<tr><th>Tool name</th><td><code>%s</code></td></tr>" % esc(name),
              "</table>"]
    lines.append('<p class="note">A shelf tool works on your scene or your settings when you click it. '
                 'It is not a node, so it does not stay in your network.</p>')
    return relative, page(relative, label, "\n".join(lines))


# ---------------------------------------------------------------------------------------------------------------
# Reference index and About


def reference_index(specs, tools):
    lines = ["<h1>Reference</h1>",
             '<p class="lead">Every node and shelf tool, with all of its parameters. Press F1 on a node in '
             "Houdini to open its page here.</p>",
             "<h2>A typical network</h2>",
             "<p>Most models follow this chain. Import a model, take one LOD out, edit it, put the LODs back "
             "together, check them and export.</p>",
             '<ol class="network">',
             "<li>P3D Import</li>", "<li>Extract LOD</li>", '<li class="optional">Your edits</li>',
             "<li>Assemble LODs</li>", "<li>Validate</li>", "<li>P3D Export</li>", "</ol>",
             "<h2>Nodes</h2>",
             "<p>Add these from the Tab menu, in the A3OB submenu.</p>", '<ul class="cards">']
    for spec in specs:
        lines.append('<li><a href="%s.html">%s</a><span>%s</span></li>' % (esc(spec.type_name), esc(spec.label),
                                                                          esc(spec.summary)))
    lines += ["</ul>", "<h2>Shelf tools</h2>", "<p>These are the buttons on the A3OB shelf.</p>",
              '<ul class="cards">']
    for name, label, help_text in tools:
        lines.append('<li><a href="tool-%s.html">%s</a><span>%s</span></li>' % (esc(name), esc(label),
                                                                              esc(help_text)))
    lines.append("</ul>")
    return "reference/index.html", page("reference/index.html", "Reference", "\n".join(lines))


def about_page():
    body = "\n".join([
        "<h1>About</h1>",
        '<p class="lead">A3OB for Houdini brings the Arma 3 model tools to Houdini.</p>',
        "<h2>What this is</h2>",
        "<p>A set of Houdini nodes and shelf tools to read, edit and write the .p3d model files of Arma 3. It is a "
        "port of Arma 3 Object Builder (A3OB), which does the same job in Blender.</p>",
        "<h2>Credits</h2>",
        "<ul>",
        "<li><strong>Arma 3 Object Builder</strong> by MrClock. The ideas, the file format code and many of the "
        "options in these tools come from this Blender add-on.</li>",
        "<li><strong>ArmAToolbox</strong> by Hans-J&ouml;rg &quot;Alwarren&quot; Frieden. A3OB continues this earlier "
        "Blender add-on.</li>",
        "</ul>",
        "<p>Thank you to both of them for the work and for sharing it under a free licence.</p>",
        "<h2>Licence</h2>",
        "<p>This add-on is free software under the GNU General Public License, version 3 or (at your option) any "
        "later version (GPL-3.0-or-later). You may use it, change it and share it, as long as you pass on the "
        "same freedoms. The full text is in the file <code>LICENSE</code> in the add-on folder.</p>",
        "<p>Arma and Arma 3 are trademarks of Bohemia Interactive. Houdini is a trademark of SideFX. This project "
        "is not made or endorsed by either company.</p>",
    ])
    return "about.html", page("about.html", "About", body)


# ---------------------------------------------------------------------------------------------------------------
# Whole build


def render_all(docs_dir=DOCS_DIR):
    """``{path from docs/user, with "/": text}`` for every file the script owns, and the refreshed hand pages."""
    sys.path.insert(0, os.path.join(REPO_ROOT, "python"))
    from a3ob_houdini import nodes

    specs = sorted(nodes.all_specs().values(), key=lambda spec: spec.label.lower())
    tools = read_tools()
    files = {}
    for spec in specs:
        relative, text = node_page(spec)
        files[relative] = text
    for name, label, help_text in tools:
        relative, text = tool_page(name, label, help_text)
        files[relative] = text
    relative, text = reference_index(specs, tools)
    files[relative] = text
    relative, text = about_page()
    files[relative] = text
    for relative in HAND_PAGES:
        with open(os.path.join(docs_dir, *relative.split("/")), "r", encoding="utf-8", newline=None) as file:
            files[relative] = refresh_hand_page(relative, file.read())
    return files


def stale_reference_files(files, docs_dir):
    """Reference pages on disk that the script no longer makes."""
    found = glob.glob(os.path.join(docs_dir, "reference", "*.html"))
    wanted = set(os.path.join(docs_dir, *path.split("/")) for path in files)
    return sorted(path for path in found if path not in wanted)


def differences(files, docs_dir=DOCS_DIR):
    """Paths whose checked-in text differs from the generated text (line endings ignored), or that should go."""
    bad = []
    for relative in sorted(files):
        path = os.path.join(docs_dir, *relative.split("/"))
        try:
            with open(path, "r", encoding="utf-8", newline=None) as file:
                actual = file.read()
        except OSError:
            bad.append(relative)
            continue
        if actual != files[relative]:
            bad.append(relative)
    bad.extend(os.path.relpath(path, docs_dir).replace(os.sep, "/") for path in stale_reference_files(files, docs_dir))
    return bad


def build(docs_dir=DOCS_DIR):
    """Write every generated file under ``docs_dir`` and remove reference pages that are no longer made."""
    files = render_all(docs_dir)
    for path in stale_reference_files(files, docs_dir):
        os.remove(path)
    for relative in sorted(files):
        path = os.path.join(docs_dir, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as file:
            file.write(files[relative])
    return sorted(files)


def main(argv):
    if "--check" in argv:
        bad = differences(render_all())
        for relative in bad:
            print("differs: docs/user/%s" % relative)
        if bad:
            print("run: python scripts/build_user_docs.py")
        return 1 if bad else 0
    for relative in build():
        print("wrote docs/user/%s" % relative)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
