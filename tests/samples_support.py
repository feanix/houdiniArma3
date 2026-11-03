"""Where the Arma 3 Samples are, for the real-file tests (D6, task #29). Not a test file: the name does not start with
``test``.

The Samples are Bohemia content under the Arma Public License (non-commercial, Arma only). They stay on the owner's disk:
nothing here copies a Samples file, and no test writes Samples bytes anywhere but a temporary folder.

Environment:
    A3OB_SAMPLES        the Samples root (the folder that holds ``Addons``). Default: the Steam path below when it exists.
    A3OB_SAMPLES_FULL=1 run the Houdini tests on all files instead of one file per Addons folder.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import array
import collections
import hashlib
import os
import shutil
import struct
import tempfile
import time

DEFAULT_ROOT = "E:/SteamLibrary/steamapps/common/Arma 3 Samples"


def samples_root():
    """The Samples root, or None when it is not there."""
    root = os.environ.get("A3OB_SAMPLES") or DEFAULT_ROOT
    return root if os.path.isdir(os.path.join(root, "Addons")) else None


ROOT = samples_root()
AVAILABLE = ROOT is not None
SKIP_REASON = "Arma 3 Samples not found (set A3OB_SAMPLES to the Samples folder; default %s)" % DEFAULT_ROOT
FULL = os.environ.get("A3OB_SAMPLES_FULL") == "1"


def all_files():
    """Every .p3d under ``Addons``, as paths relative to the root with forward slashes, sorted."""
    if not AVAILABLE:
        return []
    found = []
    for folder, _, names in os.walk(os.path.join(ROOT, "Addons")):
        for name in names:
            if name.lower().endswith(".p3d"):
                found.append(os.path.relpath(os.path.join(folder, name), ROOT).replace("\\", "/"))
    return sorted(found)


def one_per_folder():
    """The smallest file of each Addons folder that has files: the default-run subset (it keeps the run short)."""
    chosen = {}
    for rel in all_files():
        folder = os.path.dirname(rel)
        if folder not in chosen or os.path.getsize(path(rel)) < os.path.getsize(path(chosen[folder])):
            chosen[folder] = rel
    return sorted(chosen.values())


def houdini_files():
    """The files the Houdini tests use: all with ``A3OB_SAMPLES_FULL=1``, else one per Addons folder."""
    return all_files() if FULL else one_per_folder()


def path(rel):
    return os.path.join(ROOT, rel.replace("/", os.sep))


# ---------------------------------------------------------------------------------------------------------------------
# Houdini level: real nodes (P3D Import, Extract LOD, Assemble LODs, P3D Export) wired in a scratch network.
# ---------------------------------------------------------------------------------------------------------------------


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def first_difference(a, b):
    """The offset of the first byte where ``a`` and ``b`` differ (the shorter length when one is a prefix), or None."""
    for offset, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return offset
    return None if len(a) == len(b) else min(len(a), len(b))


def _tagg_view(tagg):
    return (type(tagg).__name__, tagg.name, tagg.active, tagg.payload())


def _zeroed(values):
    """Float32 bytes with -0.0 turned into 0.0."""
    return array.array("f", [value + 0.0 for value in values]).tobytes()


def _bits(values):
    """The float32 bits of some numbers, so that two NaNs with other payloads are not the same and NaN equals itself."""
    return struct.pack("<%df" % len(values), *[value + 0.0 for value in values])  # + 0.0: -0.0 and 0.0 are one value


def lod_content(lod):
    """A description of a LOD that does not depend on the order of points and faces, the layout of the normal table, the
    unused slot of triangles or the order of sharp edge pairs: points by position and flags, faces by the positions of
    their corners with UVs, normal vectors, flags, texture and material, and each TAGG by what it says about those."""
    point_keys = [(_bits(lod.point(i)), lod.point_flags[i]) for i in range(lod.point_count)]
    corner = 0
    face_keys = []
    corner_start = []
    for i in range(lod.face_count):
        face = lod.face(i)
        face_keys.append((tuple(_bits(lod.point(v)) for v in face.vertices), tuple(_bits(uv) for uv in face.uvs),
                          tuple(_bits(lod.normal(n)) for n in face.normals), face.flags, face.texture,
                          face.material))
        corner_start.append(corner)
        corner += len(face.vertices)
    taggs = []
    for tagg in lod.taggs:
        name = (type(tagg).__name__, tagg.name, tagg.active)
        if hasattr(tagg, "vertex_weights"):
            members = sorted((point_keys[i], w) for i, w in enumerate(tagg.vertex_weights) if w)
            faces = sorted((face_keys[i], w) for i, w in enumerate(tagg.face_weights) if w)
            taggs.append((name, members, faces))
        elif hasattr(tagg, "vertex_bytes"):
            members = sorted((point_keys[i], w) for i, w in enumerate(tagg.vertex_bytes) if w)
            faces = sorted((face_keys[i], w) for i, w in enumerate(tagg.face_bytes) if w)
            taggs.append((name, members, faces))
        elif tagg.name == "#Hide#" and len(tagg.payload()) == lod.point_count + lod.face_count:
            # Object Builder's hide state: one byte per point, then one per face (task #36 treats it as a mask).
            raw = tagg.payload()
            members = sorted((point_keys[i], w) for i, w in enumerate(raw[:lod.point_count]) if w)
            faces = sorted((face_keys[i], w) for i, w in enumerate(raw[lod.point_count:]) if w)
            taggs.append((name, members, faces))
        elif hasattr(tagg, "masses"):
            taggs.append((name, sorted((point_keys[i], m) for i, m in enumerate(tagg.masses))))
        elif hasattr(tagg, "edges"):
            # Points that share a position cannot be told apart once the order changes (the proxy points of several
            # proxies, say), so a pair that touches one counts only as "ambiguous".
            where = collections.Counter(_bits(lod.point(i)) for i in range(lod.point_count))
            pairs = []
            for a, b in tagg.pairs():
                if max(a, b) >= lod.point_count:
                    pairs.append((a, b))
                elif where[_bits(lod.point(a))] >= 2 or where[_bits(lod.point(b))] >= 2:
                    pairs.append("ambiguous")
                else:
                    pairs.append(tuple(sorted((_bits(lod.point(a)), _bits(lod.point(b))))))
            taggs.append((name, sorted(pairs, key=repr)))
        elif hasattr(tagg, "uvs") and hasattr(tagg, "set_id"):
            per_face = sorted((face_keys[i], tuple(tagg.uvs[2 * corner_start[i]:2 * (corner_start[i] + lod.face_sides[i])]))
                              for i in range(lod.face_count))
            taggs.append((name, tagg.set_id, per_face))
        elif hasattr(tagg, "positions"):
            taggs.append((name, tagg.frame_time, sorted(zip(point_keys, [tuple(tagg.positions[3 * i:3 * i + 3])
                                                                       for i in range(lod.point_count)]))))
        else:
            taggs.append((name, tagg.payload()))
    return {"header": (lod.flags, lod.resolution_signature, lod.eof_active), "points": sorted(point_keys),
            "faces": sorted(face_keys), "taggs": sorted(taggs, key=repr)}


def lod_differences(a, b):
    """The names of the parts in which two M1 LODs differ, as a list (empty: the same data).

    Houdini geometry cannot hold some things a file has, so four kinds of difference are named instead of being called
    a change of the mesh: ``padding`` (the unused fourth slot of a triangle's vertex, normal or UV record is not zero),
    ``normal table`` (other table entries or indices, every corner has the same vector), ``sharp edge order`` (the same
    pairs in another order) and ``point and face order`` (the same points and faces in another order, which is what
    interleaved proxies cause). ``content`` means a difference that none of these explains."""
    found = []
    content_a, content_b = lod_content(a), lod_content(b)
    if repr(content_a) != repr(content_b):  # repr: NaN equals NaN
        parts = [key for key in ("header", "points", "faces") if repr(content_a[key]) != repr(content_b[key])]
        in_b = set(map(repr, content_b["taggs"]))
        in_a = set(map(repr, content_a["taggs"]))
        parts += sorted({t[0][0] for t in content_a["taggs"] if repr(t) not in in_b}
                        | {t[0][0] for t in content_b["taggs"] if repr(t) not in in_a})
        return ["content: " + ", ".join(parts)]
    if _zeroed(a.points) != _zeroed(b.points) or list(a.point_flags) != list(b.point_flags) or list(a.face_sides) != list(
            b.face_sides) or list(a.face_flags) != list(b.face_flags) or a.face_textures != b.face_textures or             a.face_materials != b.face_materials or list(a.face_corner_indices()[1]) != list(
            b.face_corner_indices()[1]) or a.corner_uvs().tobytes() != b.corner_uvs().tobytes():
        found.append("point and face order")
    elif (list(a.face_vertices) != list(b.face_vertices) or a.face_uvs.tobytes() != b.face_uvs.tobytes()
          or (list(a.face_normals) != list(b.face_normals)
              and list(a.face_corner_normal_indices()) == list(b.face_corner_normal_indices()))):
        found.append("padding")
    if "point and face order" not in found and a.points.tobytes() != b.points.tobytes():
        found.append("signed zero")
    if "point and face order" not in found:
        if a.normals.tobytes() != b.normals.tobytes() or list(a.face_corner_normal_indices()) != list(
                b.face_corner_normal_indices()):
            found.append("normal table")
        sharp_a, sharp_b = a.sharp_edges(True), b.sharp_edges(True)
        if sharp_a != sharp_b:
            found.append("sharp edge order")
        elif [_tagg_view(t) for t in a.taggs] != [_tagg_view(t) for t in b.taggs]:
            found.append("tagg bytes")
    else:
        found.append("normal table")
    return found


class Rig:
    """A scratch ``/obj`` container and the four node types. ``close`` removes it and the temporary folder. Installs the
    assets into the running session only (no Houdini preferences are written)."""

    KINDS = ("p3d_import", "extract_lod", "assemble_lods", "p3d_export")

    def __init__(self):
        import hou
        from tests import hou_support as hs
        from a3ob_houdini import hda_source, nodes, settings
        self.hou = hou
        self.nodes = nodes
        self.settings = settings.Settings()  # defaults: never read the user's preferences
        paths = [os.path.join(hs.OTLS_DIR, hda_source.hda_folder_name(nodes.load(kind).SPEC)) for kind in self.KINDS]
        self.held = hs.install_hdas_for_class(paths)
        self.container = hou.node("/obj").createNode("geo", "samples_rig")
        self.folder = tempfile.mkdtemp(prefix="a3ob_samples_")

    def close(self):
        from tests import hou_support as hs
        self.container.destroy()
        hs.uninstall_hdas(self.held)
        shutil.rmtree(self.folder, ignore_errors=True)

    def cook(self, node):
        """Cook and return ``(error texts, seconds)``."""
        started = time.perf_counter()
        try:
            node.cook(force=True)
        except self.hou.OperationFailed:
            pass
        return list(node.errors()), time.perf_counter() - started

    def importer(self, rel):
        """``(node, errors, seconds)`` of P3D Import on a Samples file."""
        node = self.container.createNode("a3ob_p3d_import")
        node.parm("file").set(path(rel))
        errors, seconds = self.cook(node)
        return node, errors, seconds

    def exporter(self, source, out_file, preset):
        """A P3D Export node on ``source`` with a built-in preset applied (project root P: drive)."""
        node = self.container.createNode("a3ob_p3d_export")
        node.setInput(0, source)
        node.parm("file").set(out_file)
        node.parm("projectroot").set("P:\\")
        node.parm("maxmessages").set(100000)
        self.nodes.load("p3d_export").apply_preset(node, preset, self.settings)
        return node

    def save(self, node):
        """Save to Disk. Returns ``(report, seconds)``; ``report.has_errors`` tells whether nothing was written."""
        started = time.perf_counter()
        report = self.nodes.load("p3d_export").run_save(node)
        return report, time.perf_counter() - started

    def out_file(self, rel, tag):
        folder = os.path.join(self.folder, tag)
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, os.path.basename(rel))

    def lod_count(self, imported):
        return len(imported.geometry().prims())

    def roundtrip(self, rel, preset="Lossless", via_assemble=False):
        """Import a Samples file, optionally send every LOD through Extract LOD and Assemble LODs (base = the import, no
        edits), and save with ``preset``. Returns a dict: ``import_seconds``, ``export_seconds``, ``import_errors``,
        ``report`` (the save report), ``written`` (bytes or None), ``source`` (bytes)."""
        imported, import_errors, import_seconds = self.importer(rel)
        result = {"import_errors": import_errors, "import_seconds": import_seconds, "lods": self.lod_count(imported),
                  "import_warnings": list(imported.warnings())}
        with open(path(rel), "rb") as file:
            result["source"] = file.read()
        head = imported
        if via_assemble and not import_errors:
            assemble = self.container.createNode("a3ob_assemble_lods")
            assemble.setInput(0, imported)
            for k in range(result["lods"]):
                extract = self.container.createNode("a3ob_extract_lod")
                extract.setInput(0, imported)
                extract.parm("lod").set(str(k))
                assemble.setInput(k + 1, extract)
            result["assemble_errors"], result["assemble_seconds"] = self.cook(assemble)
            head = assemble
        out = self.out_file(rel, "assemble" if via_assemble else preset.replace(" ", "_"))
        export = self.exporter(head, out, preset)
        result["report"], result["export_seconds"] = self.save(export)
        result["written"] = None
        if os.path.isfile(out):
            with open(out, "rb") as file:
                result["written"] = file.read()
        return result


# ---------------------------------------------------------------------------------------------------------------------
# Judging a round trip that is not byte-identical.
# ---------------------------------------------------------------------------------------------------------------------

#: Differences of a LOD that the Houdini geometry cannot hold. They are findings, not test failures: see
#: docs/design/evidence/samples/samples_findings.md. A LOD whose differences are all here still has the same content.
KNOWN_L1 = ("normal table", "padding", "sharp edge order", "signed zero")
#: What differs in a LOD with interleaved proxies (the import warns about it): its points and faces are written in
#: another order. Since task #36 the records that hold point or face numbers (editor masks, sharp pairs) follow the move,
#: so the order is the only difference (the content check in ``lod_content`` compares them by position).
INTERLEAVED_L1 = ("point and face order",)


def interleaved_lod_names(warnings):
    """The names of the LODs the importer warned about ("the proxies are not the last points and faces")."""
    names = set()
    for text in warnings:
        marker = ": the proxies are not the last points and faces"
        if marker in text:
            names.add(text.split(marker)[0].rsplit(": ", 1)[-1])
    return names


def compare_files(source, written, interleaved=()):
    """Compare a source file and a file written from it (bytes). Returns ``(rows, unexplained)``: ``rows`` is one
    ``(lod index, lod name, [difference names])`` for each LOD that differs, ``unexplained`` the rows with a
    difference that is neither in ``KNOWN_L1`` nor (for a LOD in ``interleaved``) in ``INTERLEAVED_L1``."""
    from a3ob_core import p3d
    model_a = p3d.read_p3d(source, strict=False)
    model_b = p3d.read_p3d(written, strict=False)
    if len(model_a.lods) != len(model_b.lods):
        return [(-1, "", ["LOD count %d -> %d" % (len(model_a.lods), len(model_b.lods))])], [(-1, "", ["LOD count"])]
    rows, unexplained = [], []
    for index, (a, b) in enumerate(zip(model_a.lods, model_b.lods)):
        found = lod_differences(a, b)
        if not found:
            continue
        rows.append((index, a.name, found))
        allowed = set(KNOWN_L1) | (set(INTERLEAVED_L1) if a.name in interleaved else set())
        if any(item not in allowed for item in found):
            unexplained.append((index, a.name, found))
    if model_a.trailing != model_b.trailing:
        unexplained.append((-1, "", ["trailing bytes"]))
    return rows, unexplained


def locate(source, offset):
    """Where a byte offset of a source file lies: ``"LOD k (name): section"``. Sections: header, points[i], normals[i],
    faces[i], TAGG name, signature."""
    from a3ob_core import p3d
    model = p3d.read_p3d(source, strict=False)
    if offset < 12:
        return "file header"
    position = 12
    for k, lod in enumerate(model.lods):
        size = len(p3d.write_p3d(p3d.P3DModel([lod]))) - 12
        if offset >= position + size:
            position += size
            continue
        local = offset - position
        where = "LOD %d (%s): " % (k, lod.name)
        if local < 28:
            return where + "header"
        local -= 28
        for label, count, width in (("points", lod.point_count, 16), ("normals", lod.normal_count, 12)):
            if local < count * width:
                return where + "%s[%d]" % (label, local // width)
            local -= count * width
        for face in range(lod.face_count):
            width = 4 + 4 * 16 + 4 + len(lod.face_textures[face]) + 1 + len(lod.face_materials[face]) + 1
            if local < width:
                return where + "faces[%d]" % face
            local -= width
        for tagg in lod.taggs:
            width = 4 + 1 + len(tagg.name) + 1 + 4 + len(tagg.payload())
            if local < width:
                return where + "TAGG %s" % tagg.name
            local -= width
        return where + "end of LOD (end tagg, signature)"
    return "after the last LOD"
