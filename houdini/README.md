# houdini/

Houdini content root for the `a3ob_houdini` package. The package file
(`packages/a3ob_houdini.json`) adds this folder to `HOUDINI_PATH`, so Houdini
looks here for the usual subfolders. They come in later milestones:

| Folder        | Holds                                               |
| ------------- | --------------------------------------------------- |
| `otls/`       | Digital assets (HDAs), once the Indie `.hdalc` question (D3) is settled |
| `toolbar/`    | Shelf tools (`.shelf`)                              |
| `python3.13libs/` | Houdini startup scripts (`pythonrc.py`, `uiready.py`), if needed |
| `MainMenuCommon.xml`, `radialmenu/` | Menu entries                  |
| `config/Icons/` | Icons                                             |

Python modules do not go here: they live in `../python/` (`a3ob_core`,
`a3ob_houdini`), which the package puts on `PYTHONPATH`.
