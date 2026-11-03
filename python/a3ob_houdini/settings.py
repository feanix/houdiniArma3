# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# The project root default (``P:\``) and the common data lists come from A3OB ``__init__.py`` (preferences) and
# ``utilities/data.py`` (``common_data``), v2.5.1 (9f2b8bc). A3OB: MrClock (present add-on), Hans-Joerg "Alwarren"
# Frieden (original ArmaToolbox). Modified: one JSON file instead of Blender preferences.
"""User settings, export presets, project root and common data (ux-tools.md sections 7.1 and 7.2, D14 Q-5 and Q-7).

No ``hou`` is needed. The settings are one JSON file in the Houdini user preferences folder:

    {"project_root": "P:\\", "active_preset": "Lossless", "common_data": "", "presets": {"mine": {"lowercase": true}}}

Where the file lives, in this order: the environment variable ``A3OB_SETTINGS`` (a full file path); else
``HOUDINI_USER_PREF_DIR`` (the variable, or Houdini's own value when ``hou`` is there); else ``~/.a3ob``. The
environment variable ``A3OB_PROJECT_ROOT`` overrides the stored project root.

Loading never raises. A missing file gives the defaults. A file that is not valid JSON gives the defaults and a warning
(the bad file is left alone until the artist saves). Unknown keys are dropped from presets with a warning; unknown
top-level keys are kept and written back, so a newer file is not damaged.
"""

import dataclasses
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from .errors import UserError
from .messages import format_problem
from .p3d_export.options import ExportOptions

__all__ = [
    "ENV_FILE", "ENV_ROOT", "DEFAULT_ROOT", "BUILTIN_PRESETS", "LOSSLESS", "RELEASE", "Settings", "settings_path",
    "load", "save", "preset_names", "get_preset", "options_for", "effective_root", "set_project_root", "save_preset",
    "delete_preset", "set_active_preset", "common_data",
]

ENV_FILE = "A3OB_SETTINGS"
ENV_ROOT = "A3OB_PROJECT_ROOT"
FILE_NAME = "a3ob_settings.json"
DEFAULT_ROOT = "P:\\"

LOSSLESS = "Lossless"
RELEASE = "Arma 3 release"

#: The option names a preset may hold: the fields of ``ExportOptions`` except the project root (that is a setting).
PRESET_KEYS = tuple(field.name for field in dataclasses.fields(ExportOptions) if field.name != "project_root")

_ALL_OFF = {"lowercase": False, "relative_paths": False, "sort_sections": False, "sort_lods": False,
            "auto_components": False}

#: Q-5, D14. Lossless is all off (the ExportOptions defaults). Arma 3 release is A3OB's defaults (ux-tools.md 5.9):
#: relative paths, sort sections, lowercase and generate components on; duplicate LODs stay on Fail.
BUILTIN_PRESETS: Dict[str, Dict[str, Any]] = {
    LOSSLESS: dict(_ALL_OFF),
    RELEASE: dict(_ALL_OFF, lowercase=True, relative_paths=True, sort_sections=True, auto_components=True),
}

#: A3OB ``utilities/data.py`` ``common_data``, as it is used here. Paths keep A3OB's text, including ``P:``.
BUILTIN_COMMON: Dict[str, Dict[str, str]] = {
    "namedprops": {
        "autocenter": "0", "buoyancy": "1", "class": "house", "forcenotalpha": "1", "lodnoshadow": "1",
        "map": "house", "prefershadowvolume": "1",
    },
    "materials": {
        "Glass": "\\a3\\data_f\\glass_veh.rvmat",
        "Collimator": "\\a3\\weapons_f\\acc\\data\\collimdot_cshader.rvmat",
        "VR Armor Emissive": "\\a3\\characters_f_bootcamp\\common\\data\\vrarmoremmisive.rvmat",
        "Penetration: Armor": "\\a3\\data_f\\penetration\\armour.rvmat",
        "Penetration: Armor Plate": "\\a3\\data_f\\penetration\\armour_plate.rvmat",
        "Penetration: Engine": "\\a3\\data_f\\penetration\\engine.rvmat",
        "Penetration: Fuel tank": "\\a3\\data_f\\penetration\\fueltank.rvmat",
        "Penetration: Glass": "\\a3\\data_f\\penetration\\glass.rvmat",
        "Penetration: Interior": "\\a3\\data_f\\penetration\\vehicle_interior.rvmat",
        "Penetration: Meat (limbs)": "\\a3\\data_f\\penetration\\meat.rvmat",
        "Penetration: Meat + Bones (body)": "\\a3\\data_f\\penetration\\meatbones.rvmat",
        "Penetration: Plastic": "\\a3\\data_f\\penetration\\plastic.rvmat",
    },
    "procedurals": {"PIP": "#(argb,512,512,1)r2t(rendertarget0,1.0)"},
}
COMMON_SECTIONS = ("proxies", "namedprops", "materials", "procedurals")


@dataclasses.dataclass
class Settings:
    project_root: str = DEFAULT_ROOT
    active_preset: str = LOSSLESS
    common_data: str = ""
    presets: Dict[str, Dict[str, Any]] = dataclasses.field(default_factory=dict)
    #: Top-level keys this version does not know, kept for the next save.
    extra: Dict[str, Any] = dataclasses.field(default_factory=dict)


def settings_path() -> str:
    """The settings file: ``A3OB_SETTINGS``, else the user preferences folder."""
    explicit = os.environ.get(ENV_FILE, "").strip()
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    folder = os.environ.get("HOUDINI_USER_PREF_DIR", "").strip()
    if not folder:
        try:
            import hou
            folder = hou.text.expandString("$HOUDINI_USER_PREF_DIR")
        except Exception:  # no hou, or a Houdini without that variable
            folder = ""
    if not folder or "$" in folder:
        folder = os.path.join(os.path.expanduser("~"), ".a3ob")
    return os.path.join(folder, FILE_NAME)


def _clean_preset(name: str, raw: Any, problems: List[str]) -> Dict[str, Any]:
    """Keep the known options of a stored preset that have the right type; say what was dropped."""
    if not isinstance(raw, dict):
        problems.append("The preset %r is not a set of options and was ignored" % name)
        return {}
    defaults = ExportOptions()
    clean: Dict[str, Any] = {}
    for key, value in raw.items():
        if key not in PRESET_KEYS:
            problems.append("The preset %r has an unknown option %r, which was ignored" % (name, key))
            continue
        default = getattr(defaults, key)
        if isinstance(default, tuple):
            # Tuple options (rule ids in rules_off) come back from JSON as lists.
            if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
                clean[key] = tuple(value)
            else:
                problems.append("The preset %r has a bad value for %r, which was ignored" % (name, key))
            continue
        if type(value) is not type(default):
            problems.append("The preset %r has a bad value for %r, which was ignored" % (name, key))
            continue
        clean[key] = value
    try:
        ExportOptions(**clean)
    except ValueError as ex:
        problems.append("The preset %r is not valid (%s) and was ignored" % (name, ex))
        return {}
    return clean


def load(path: Optional[str] = None) -> Tuple[Settings, List[str]]:
    """``(settings, problems)``. ``problems`` are plain sentences for a message; nothing is raised."""
    path = path or settings_path()
    settings = Settings()
    problems: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as file:
            raw = json.load(file)
    except FileNotFoundError:
        return settings, problems
    except (OSError, ValueError) as ex:  # ValueError covers bad JSON and bad encoding
        return settings, ["The settings file %s cannot be read (%s). The defaults are used" % (path, ex)]
    if not isinstance(raw, dict):
        return settings, ["The settings file %s does not hold a JSON object. The defaults are used" % path]
    for key, value in raw.items():
        if key in ("project_root", "active_preset", "common_data"):
            if isinstance(value, str):
                setattr(settings, key, value)
            else:
                problems.append("The setting %r must be text and was ignored" % key)
        elif key == "presets":
            if isinstance(value, dict):
                for name, options in value.items():
                    if name in BUILTIN_PRESETS:
                        problems.append("The preset name %r is built in and was ignored" % name)
                    else:
                        settings.presets[name] = _clean_preset(name, options, problems)
            else:
                problems.append("The setting 'presets' must be an object and was ignored")
        else:
            settings.extra[key] = value
    return settings, problems


def save(settings: Settings, path: Optional[str] = None) -> str:
    """Write the file through a temporary file. Returns the path. Raises ``UserError`` when it cannot be written."""
    path = path or settings_path()
    data = dict(settings.extra)
    data.update(project_root=settings.project_root, active_preset=settings.active_preset,
                common_data=settings.common_data, presets=settings.presets)
    temp = "%s.%d.tmp" % (path, os.getpid())
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(temp, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, sort_keys=True)
            file.write("\n")
        os.replace(temp, path)
    except OSError as ex:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise UserError(format_problem(None, "the settings file %s cannot be written (%s)" % (path, ex.strerror or ex),
                                       "Check that the folder is writable, or set %s to another file" % ENV_FILE)) from None
    return path


def preset_names(settings: Settings) -> List[str]:
    """The built-in presets first, then the artist's own in name order."""
    return list(BUILTIN_PRESETS) + sorted(settings.presets)


def get_preset(settings: Settings, name: str) -> Dict[str, Any]:
    """The option values of a preset (a copy). An unknown name raises ``UserError``."""
    if name in BUILTIN_PRESETS:
        return dict(BUILTIN_PRESETS[name])
    if name in settings.presets:
        return dict(settings.presets[name])
    raise UserError(format_problem(None, "there is no preset called %r" % name,
                                   "Choose one of: %s" % ", ".join(preset_names(settings))))


def effective_root(settings: Settings) -> str:
    """The project root in use: the environment variable, else the stored value, else ``P:\\``."""
    return os.environ.get(ENV_ROOT, "").strip() or settings.project_root or DEFAULT_ROOT


def options_for(settings: Settings, name: Optional[str] = None) -> ExportOptions:
    """``ExportOptions`` for a preset (default: the active one), with the project root filled in."""
    values = get_preset(settings, name or settings.active_preset)
    return ExportOptions(project_root=effective_root(settings), **values)


def set_project_root(settings: Settings, root: str) -> None:
    root = root.strip()
    if not root:
        raise UserError(format_problem(None, "the project root is empty", "Type a folder such as %s" % DEFAULT_ROOT))
    settings.project_root = root


def save_preset(settings: Settings, name: str, options: Any) -> None:
    """Store a user preset from an ``ExportOptions`` or a dict. Built-in names cannot be replaced."""
    name = name.strip()
    if not name:
        raise UserError(format_problem(None, "the preset name is empty", "Type a name"))
    if name in BUILTIN_PRESETS:
        raise UserError(format_problem(None, "%r is a built-in preset and cannot be replaced" % name,
                                       "Use another name"))
    values = dataclasses.asdict(options) if isinstance(options, ExportOptions) else dict(options)
    values.pop("project_root", None)  # the root is a setting, not part of a preset
    problems: List[str] = []
    clean = _clean_preset(name, values, problems)
    if problems:
        raise UserError(format_problem(None, problems[0], None))
    settings.presets[name] = clean


def delete_preset(settings: Settings, name: str) -> None:
    if name in BUILTIN_PRESETS:
        raise UserError(format_problem(None, "%r is a built-in preset and cannot be deleted" % name, None))
    settings.presets.pop(name, None)
    if settings.active_preset == name:
        settings.active_preset = LOSSLESS


def set_active_preset(settings: Settings, name: str) -> None:
    get_preset(settings, name)  # raises for an unknown name
    settings.active_preset = name


def common_data(settings: Settings) -> Tuple[Dict[str, Dict[str, str]], List[str]]:
    """The "Common" lists: the built-in ones, with the entries of the common data file added (the file wins on a
    clash). ``(data, problems)``; a missing or bad file is a problem and the built-in lists are returned."""
    data = {section: dict(BUILTIN_COMMON.get(section, {})) for section in COMMON_SECTIONS}
    problems: List[str] = []
    path = settings.common_data.strip()
    if not path:
        return data, problems
    try:
        with open(os.path.expandvars(path), "r", encoding="utf-8") as file:
            raw = json.load(file)
    except (OSError, ValueError) as ex:
        return data, ["The common data file %s cannot be read (%s)" % (path, ex)]
    if not isinstance(raw, dict):
        return data, ["The common data file %s does not hold a JSON object" % path]
    for section in COMMON_SECTIONS:
        entries = raw.get(section)
        if entries is None:
            continue
        if not isinstance(entries, dict):
            problems.append("The common data section %r must be an object and was ignored" % section)
            continue
        for key, value in entries.items():
            if isinstance(value, str):
                data[section][str(key)] = value
            else:
                problems.append("The common data entry %s/%s must be text and was ignored" % (section, key))
    return data, problems
