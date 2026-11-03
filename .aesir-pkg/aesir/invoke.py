"""How to invoke Aesir from here: `python -m aesir` in the Aesir repo, the vendored launcher in a client."""
import re
from pathlib import Path

DEV = "python -m aesir"
_DEV_RE = re.compile(re.escape(DEV) + r"(?![\w-])")  # whole tokens only: `python -m aesirfoo` must stay untouched
CLIENT = "python .aesir-pkg/aesir_run.py"


def invocation(pkg_file=None):
    """The command prefix for hints: the launcher when this package lives in a `.aesir-pkg` folder."""
    return CLIENT if ".aesir-pkg" in Path(pkg_file or __file__).resolve().parts else DEV


def cmd(text):
    """Rewrite a 'python -m aesir ...' command string (or any text containing it) for this install."""
    inv = invocation()
    return text if inv == DEV or text is None else _DEV_RE.sub(lambda _m: inv, text)
