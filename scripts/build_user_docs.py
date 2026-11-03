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

import argparse
import glob
import html
import os
import re
import sys
import xml.etree.ElementTree as ElementTree

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "python"))

# The text helpers are shared with the Help section of each HDA (python/a3ob_houdini/hda_source.py).
from a3ob_houdini.docs_common import (FOLDER_TEXT, NODE_INTRO, collapse_inputs, condition_text,  # noqa: E402
                                      default_text, input_sentence, inputs_text, menu_texts, parm_index_for,
                                      range_text, split_menu_help, type_text)

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


# ---------------------------------------------------------------------------------------------------------------
# Node pages


def folder_html(spec, parm, scope=None):
    """A folder: a heading that says what it is, then its parameters. Folders are only a grouping on the node."""
    lines = ['<section class="folder" id="%s">' % esc(parm.name),
             "<h3>%s <code>%s</code></h3>" % (esc(parm.label), esc(parm.name)),
             "<p>These parameters are in the %s %s.</p>" % (esc(parm.label), esc(FOLDER_TEXT[parm.folder_type]))]
    if parm.help:
        lines.append("<p>%s</p>" % esc(parm.help))
    lines.append('<div class="folder-body">')
    lines.extend(parm_html(child, "", spec, scope) for child in parm.children)
    lines += ["</div>", "</section>"]
    return "\n".join(lines)


def parm_html(parm, id_prefix="", spec=None, scope=None):
    """One ``<section class="parm">`` for a parameter (or ``folder_html`` for a folder)."""
    if parm.kind == "folder":
        return folder_html(spec, parm, scope)
    anchor = id_prefix + parm.name.replace("#", "")
    lines = ['<section class="parm" id="%s">' % esc(anchor),
             "<h3>%s <code>%s</code></h3>" % (esc(parm.label), esc(parm.name))]
    intro, item_text = (menu_texts(parm) if parm.kind == "menu" else (parm.help, {}))
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
    if spec is not None:
        index = parm_index_for(spec, scope)
        if parm.show_when:
            facts.append(("Shown when", condition_text(index, parm.show_when)))
        if parm.enable_when:
            facts.append(("Editable when", condition_text(index, parm.enable_when)))
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
    if parm.kind == "menu" and parm.menu_action:
        lines.append("<p>The choices are made when you open the menu, from the geometry or the settings.</p>")
    if parm.kind == "multiparm":
        lines.append("<p>Each row has these parameters. The number in the row replaces the <code>#</code> in the "
                     "name.</p>")
        lines.append('<div class="rows">')
        lines.extend(parm_html(child, anchor + "-", spec, parm) for child in parm.children)
        lines.append("</div>")
    lines.append("</section>")
    return "\n".join(lines)


def node_page(spec):
    relative = "reference/%s.html" % spec.type_name
    lines = ['<p class="crumbs"><a href="index.html">Reference</a> / Nodes</p>', "<h1>%s</h1>" % esc(spec.label),
             '<p class="lead">%s</p>' % esc(spec.summary)]
    lines.append(paragraphs(NODE_INTRO.get(spec.kind, ())))
    low = spec.inputs[0]
    inputs = inputs_text(spec)
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
            has_help = first - 1 < len(spec.input_help) and spec.input_help[first - 1]
            more = ". " + input_sentence(spec, first) if has_help else ""
            lines.append("<li value=\"%d\">%s: %s%s</li>" % (first, esc(where), esc(label), esc(more)))
        lines.append("</ol>")
        if low == 0:
            lines.append('<p class="note">The node also works with no input connected.</p>')
    lines.append("<h2>Parameters</h2>")
    lines.append("<p>Parameters are in the order they appear on the node.</p>")
    lines.extend(parm_html(parm, "", spec) for parm in spec.parms)
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
             '<p class="lead">Every node and shelf tool, with all of its parameters. In Houdini, click the '
             "<strong>?</strong> icon at the top right of the parameter pane (or press F1 over the node) to read "
             "its help. This site has the same text.</p>",
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
    parser = argparse.ArgumentParser(description="Build the generated parts of the user docs in docs/user/.")
    parser.add_argument("--check", action="store_true",
                        help="change nothing; exit 1 if a checked-in file is out of date")
    args = parser.parse_args(argv)  # --help and unknown arguments exit here, before anything is written
    if args.check:
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
