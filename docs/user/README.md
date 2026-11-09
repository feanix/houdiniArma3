<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# User docs: how the pages are laid out

These are the pages for people who use the add-on. Open `index.html` in a browser. They work offline, from a
`file://` address. There is no JavaScript, no web font and no outside link needed to read them.

Two kinds of page live here:

| Kind | Files | Who writes it |
| ---- | ----- | ------------- |
| Hand-written | `index.html`, `install.html`, `quick-start-arma.html`, `quick-start-houdini.html`, `concepts.html` | A person. Only the navigation bar and the footer are kept up to date by the script. |
| Generated | `about.html`, everything in `reference/` | `scripts/build_user_docs.py`. Never edit these by hand. |

`style.css` is the one stylesheet. It is written by hand.

## Rebuild after a change

    python scripts/build_user_docs.py            write the generated pages
    python scripts/build_user_docs.py --check    only report pages that are out of date

Run it after you change a node's parameter table, a node's text, a `.shelf` file, the navigation list or a text in
the script. A test fails when the checked-in pages differ from what the script makes, and when a link points
nowhere.

## Writing a hand-written page

Copy `install.html` and change the title, the `<h1>` and the content of `<main>`. Keep this frame:

    <!DOCTYPE html>
    <html lang="en">
    <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Page title - A3OB for Houdini</title>
    <link rel="stylesheet" href="style.css">
    </head>
    <body>
    <header class="site">
    <div class="inner">
    <a class="site-title" href="index.html">A3OB for Houdini</a>
    <!-- nav:start -->
    x
    <!-- nav:end -->
    </div>
    </header>
    <main>
    ...your content...
    </main>
    <footer class="site">
    <div class="inner">
    <!-- footer:start -->
    x
    <!-- footer:end -->
    </div>
    </footer>
    </body>
    </html>

Rules:

- Put the file next to `index.html`. Link with relative paths only (`reference/index.html`, `concepts.html`).
- Leave the text between the `nav:` and `footer:` marker comments alone. The script replaces it, so the navigation
  is the same on every page.
- A new top-level page needs an entry in `NAV` and, if it is hand-written, in `HAND_PAGES` in
  `scripts/build_user_docs.py`. Then run the script.
- Use one `<h1>` per page, then `<h2>` and `<h3>`.
- Write in plain words, with short sentences. Explain a term the first time it appears on a page. Do not mention
  task numbers, decision numbers or design document sections.
- No screenshots yet. Use text and the simple network diagram below.

## Link to a reference page

A node page is `reference/<Houdini type name>.html`, for example `reference/a3ob_p3d_import.html`. A shelf tool
page is `reference/tool-<tool name>.html`, for example `reference/tool-a3ob_new_p3d.html`. A parameter has an anchor
that is its internal name: `reference/a3ob_mass.html#mode`.

## Classes

    <p class="note">A hint or a side remark.</p>
    <p class="warning">Something that can lose work or break a file.</p>
    <p class="todo">A placeholder. Remove it when the page is written.</p>

`note`, `warning` and `todo` add their own lead-in word ("Note.", "Careful.", "Still to write."). Do not type it.

Code and file names. Use `<code>` inside a sentence, and `<pre><code>` for a block:

    <p>Open <code>$HIP/model.p3d</code>.</p>
    <pre><code>python scripts/build_user_docs.py</code></pre>

A simple network diagram. Boxes in a row with an arrow between them. Add `class="optional"` to a box that is not
always needed (it gets a dashed border):

    <ol class="network">
    <li>P3D Import</li>
    <li>Extract LOD</li>
    <li class="optional">Your edits</li>
    <li>P3D Export</li>
    </ol>

A table of key facts (two columns, the left one is the heading):

    <table class="facts">
    <tr><th>Houdini name</th><td><code>a3ob_mass</code></td></tr>
    </table>

A grid of links to other pages:

    <ul class="cards">
    <li><a href="install.html">Install</a><span>Put the add-on in Houdini.</span></li>
    </ul>

`section.parm` (one parameter) is used only by the generated reference.

`gui` and `outside` mark a step that needs the Houdini window, or a program other than Houdini. They add their own
lead-in ("Houdini window.", "Outside Houdini."). The tests cannot run such a step.

## Quoted interface text (checked by tests)

When a guide quotes text that the reader will see in Houdini, mark it with one of these tags and classes. `tests/test_user_guides.py`
finds them and checks each against the real tools, so a rename in the code breaks the test instead of the guide.

| Mark-up | What it quotes | Checked against |
| ------- | -------------- | --------------- |
| `<kbd class="ui-node">P3D Import</kbd>` | A node label (A3OB or built-in, such as Box) | The node's label in Houdini (the parameter table for A3OB nodes) |
| `<code class="ui-type">a3ob_p3d_import</code>` | A node type name | The installed node types |
| `<kbd class="ui-parm" data-node="a3ob_mass">Mode</kbd>` | A parameter label. `data-node` is the node type name. Leave it out to accept any A3OB node. | The labels of that node's parameters |
| `<kbd class="ui-menu">Uniform total</kbd>` | A menu entry | The menu labels of the A3OB nodes, including the LOD menu of Extract LOD for the example file |
| `<kbd class="ui-shelf">New P3D</kbd>` | A shelf tab or shelf tool label | The `.shelf` files in `houdini/toolbar` |
| `<kbd class="ui-dialog">Name of the new Geometry object</kbd>` | A dialog title, prompt, button or status line (or the fixed start of one) | The text of `python/a3ob_houdini/*_dialog.py` and `nodes/p3d_export.py` |
| `<samp class="ui-msg">3 LODs checked: 0 errors, 0 warnings.</samp>` | A message on a node (or the fixed start of one) | The messages of the nodes while the test runs the two quick start jobs |

Quote the part of a message that does not change. A message with a time or a file name in it is quoted up to that point.
A preset table row carries `data-option` and its cells `data-preset`; a test compares them with the built-in presets.
A sentence about how to open a node's help is wrapped in `<span class="node-help">` so it is easy to find and update.

## F1 in Houdini

Houdini's help browser does not open local `file://` pages, so the nodes do not link to these HTML pages. Each A3OB
node has a `Help` section (Houdini help wiki markup) inside its HDA. `scripts/build_hdas.py` writes it from the same
node table as the reference pages here (`python/a3ob_houdini/docs_common.py` holds the shared text helpers). The user
clicks the `?` icon at the top right of the parameter pane (or presses F1 over the node) to read it. Its last line
names the full page, `docs/user/reference/<type name>.html` in the A3OB folder, as plain text.
