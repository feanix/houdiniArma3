# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Error type for problems the artist can fix.

A ``UserError`` carries a finished message in the ux-tools.md section 2 form. The cook wrapper shows it as a node
error. Any other exception is a bug: the wrapper shows a short text and sends the traceback to the console.
"""


class UserError(Exception):
    """A problem with the input, a parameter or a file. The text is meant for the artist."""
