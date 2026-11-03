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

## F1 in Houdini

Each A3OB node has a `HelpUrl` section that points at its reference page:
`file:///$A3OB_HOUDINI/docs/user/reference/<type name>.html`. `scripts/build_hdas.py` writes it, so do not rename a
reference page by hand. `$A3OB_HOUDINI` is set by the package file `packages/a3ob_houdini.json`.
