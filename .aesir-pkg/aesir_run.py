import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aesir.cli import launcher_main

sys.exit(launcher_main(Path(__file__).resolve().parent))
