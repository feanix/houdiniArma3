# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Plain-language text about a node, shared by the HTML reference and the in-Houdini Help section.

``scripts/build_user_docs.py`` (the HTML pages in docs/user/reference) and ``hda_source`` (the "Help" section of each
HDA) both read the node's ``NodeSpec`` through these helpers, so the two always say the same thing. This module does
not import ``hou``.
"""

import re

__all__ = ["NODE_INTRO", "KIND_TEXT", "FOLDER_TEXT", "number_text", "default_text", "type_text", "range_text",
           "collapse_inputs", "input_sentence", "split_menu_help", "menu_texts", "inputs_text", "wiki_escape",
           "condition_text", "parm_index_for"]

#: A short introduction for each node page, in plain words. The parameter list below it comes from the node's table.
NODE_INTRO = {
    "model_cfg": (
        "A character P3D (uniform, vest, headgear and other gear on the man skeleton) needs a model.cfg next to it. It "
        "holds the man skeleton and one class per P3D file name. This node writes that file into a folder, so you do "
        "not copy and edit it by hand.",
        "Cooking only shows what Write model.cfg would do: the changes, any warnings and the whole text, as node "
        "messages. Write model.cfg then writes it. A model.cfg that is already there is updated in place: only the "
        "class of each model name is added or replaced (and Default, the base class and the skeleton if they are "
        "missing). Everything else in the file stays, and the old file is kept as model.cfg.bak. A binarized file, or "
        "a file that cannot be read, is refused with the line number.",
    ),
    "rig_skeleton": (
        "Builds the Arma 3 character skeleton as a KineFX skeleton (the kind that Rig Pose and Joint Deform use). The "
        "bones and their parents come from a model.cfg. Where each joint sits comes from a separate pivots .p3d file: "
        "the model.cfg only names the game's packed pivots file, which can't be read.",
        "The face bones (Face_*) and the eyes are left out. A bone with no pivot is left out and listed on the node. "
        "A new node starts from the Samples files when the Arma 3 Samples folder is set in the Project root and "
        "presets tool.",
    ),
    "weights_convert": (
        "A bone weight lives in two forms. In a P3D file it is a selection named like the bone, with a weight for each "
        "point. KineFX nodes such as Joint Deform use capture weights instead. This node converts one form into the "
        "other for the skeleton on input 2.",
        "Selections are matched to the joints ignoring upper and lower case. A selection that is not a bone (a "
        "component, a hidden selection) is left alone. A member with weight 0 (byte 255 in the file) counts as no "
        "weight.",
    ),
    "weight_cleanup": (
        "Arma allows at most 4 bones per point and reads weights in steps of 1/254. This node makes the bone "
        "selections follow that: it drops tiny weights, keeps the heaviest bones, scales each point to a sum of 1 and "
        "snaps the weights to the 1/254 grid so the sum is exactly 1.",
        "It writes a3ob_rig_changed on the points: how much the weights of that point changed. The node message "
        "gives the counts and lists the points with no weight. If the model.cfg class of the skeleton has isDiscrete "
        "= 1, every point keeps only its heaviest bone.",
    ),
    "weight_transfer": (
        "Copies the bone weights of a source body (for example Bohemia's sample character) onto your mesh. Each point "
        "of your mesh takes the weights of the closest point on the body when it is near enough and faces the same "
        "way. The points that don't match are filled in smoothly from their neighbours.",
        "The node weights only the body: the tight polygons that follow the skin (skin, undergarments, shoes). Put "
        "them in the Body group; empty means every point. Points outside the group keep the weights they have, none "
        "on a fresh mesh. Weight a helmet, pouches or straps with Accessory Weights after this node. The result is bone "
        "selections in the model.cfg spelling, with a3ob_rig_origin (where each weight came from). Run Weight Cleanup "
        "last.",
    ),
    "lod_weights": (
        "PolyReduce moves and merges points, so the weights it carries along no longer add up to 1. This node weights a "
        "reduced LOD again from the finished Res 1: each point takes the weights of the closest place on the Res 1 "
        "surface, interpolated across the Res 1 triangle, then limited to 4 bones and normalised like Weight Cleanup "
        "does.",
        "A rigid piece of Res 1 (a helmet or a plate from Accessory Weights in Rigid mode, or an island that follows one bone only) "
        "keeps its weights exactly: a point next to it takes the piece's weights, with no blend. A point farther from "
        "Res 1 than Match Distance is listed in a warning and filled in from its neighbours. The bone selections of "
        "input 1 are replaced; its other groups and attributes stay. The points get a3ob_rig_origin (1 copied, 2 filled "
        "in, 3 rigid piece). LOD chain: Unpose, PolyReduce, LOD Weights (input 2 is the weighted Res 1), LOD Properties.",
    ),
    "accessory_weights": (
        "Accessories do not follow the skin: a helmet turns with the head, a pouch with the belt, a bandolier bends with "
        "the chest. This node weights them from points that already have weights. Put the accessory points in the Target "
        "Group and the points that give the weights in the Source Group: the body, or another accessory (a pouch can "
        "take its weights from the belt). Chain one node per accessory after Weight Transfer, and run Weight Cleanup "
        "last.",
        "Rigid mode gives every connected piece of the target one bone or one fixed blend. Auto picks the bone with "
        "the most source weight under the piece (a twist bone counts for the next bone down the chain); Fixed blend "
        "gives every point the average; Bone gives every piece the bone you pick from the menu of the skeleton. "
        "Non-rigid mode lets each point take the source weights under it and smooths them along the piece. A target "
        "point with no source point within the distance limit takes the nearest weighted source point, and the node "
        "warns with the count. The result is bone selections, a3ob_rig_origin (3 rigid, 4 non-rigid) and a3ob_rig_piece "
        "(the number of the rigid or non-rigid piece, which Unpose uses).",
    ),
    "rig_landmarks": (
        "Rig Fit fits the skeleton to 18 landmarks of your character: the chin, the groin, and the left and right "
        "shoulder, elbow, wrist, hip, knee, ankle, middle finger and thumb. This node holds them as positions (X, Y, Z) "
        "that you place by hand. Put the node between your mesh and Rig Fit. Connect the skeleton and the sample body to "
        "its inputs 2 and 3 (the ones that go into Rig Fit) and press Guess Landmarks: it fills all 18 fields with the "
        "guess of Rig Fit. "
        "Then select the node, move the mouse over the viewport and press Enter: every landmark is a labelled marker "
        "that you drag. The markers are drawn on top of the mesh. A joint belongs inside the body; the chin and the "
        "groin are on the skin.",
        "Mirror is on by default. Left is the +X side. The right markers follow the left ones, mirrored about the X = 0 "
        "plane, so dragging a left marker moves its right partner (the right fields cannot be edited then). Switch "
        "Mirror off to place both sides freely. To put a landmark back to the guess, choose it in the Landmark menu and "
        "press Guess This Landmark. To set a landmark from the mesh, select points in the viewport, choose the "
        "landmark in the Landmark menu and press Set From Selected Points: it takes the average of the points. Only the "
        "position is kept, not the selection. This is handy for the chin and the groin.",
        "The node gives your mesh unchanged, with one detail vector per landmark that is set (a3ob_lm_chin and so on), "
        "which Rig Fit reads. A fresh node has no position set (all three numbers zero means not set); it warns and "
        "needs the guess. Rig Fit then guesses the landmarks that are not set.",
    ),
    "rig_fit": (
        "Your own character has its own size and proportions, and the Arma skeleton and the sample body do not match "
        "it. This node fits them to your mesh, so that Weight Transfer can copy the weights of the sample body onto "
        "your mesh, and Unpose can later bring your mesh back to the Arma rest pose. The Arma skeleton itself is never "
        "changed: the fitted skeleton is only a working copy.",
        "The fit starts from landmarks, 18 named points of the body. Your mesh should stand upright (Y up), face +Z like "
        "the sample, and be in an A-pose with the arms away from the body. Set the landmarks with a Rig Landmarks node "
        "in front of this one: you drag a marker for each in the viewport. The chin and the groin are points on the skin "
        "(the chin is the tip, the groin the highest point between the legs); the others are joints, inside the body. "
        "The hands have two each: the last joint of the middle finger gives the length and the direction of the hand, "
        "the last joint of the thumb says which way the palm faces; the other fingers follow the hand. "
        "Left is the +X side. Landmarks that Rig Landmarks does not set are guessed from the shape of the mesh, and a "
        "position you set always wins over the guess. The guess is a first try: show the landmarks and correct the ones "
        "that are wrong.",
        "The fit goes down the skeleton from the pelvis. Each joint is turned towards the landmarks below it and given "
        "the length that puts them on the landmarks. The message of the node says how far the joints were from their "
        "landmarks after the fit, and warns when that is more than 1 cm. The node has one output that holds three "
        "results as packed primitives, named by the prim string a3ob_rig_part: fitted_skeleton, fitted_body (the "
        "sample body posed to match your mesh, with its weights) and landmarks (points with a name). Connect it to "
        "input 2 of Weight Transfer as it is: Weight Transfer takes the fitted body from it. The Show menu only "
        "decides which results are drawn in the viewport; the output is the same for every choice. Refine (on by "
        "default) wraps the fitted body onto the points of the Body Group of your mesh with Topo Transfer, so the "
        "fitted body takes your proportions; the pelvis is placed from the torso, not from the groin.",
    ),
    "unpose": (
        "Weight Transfer and Weight Cleanup weight your mesh in the pose of the fitted skeleton, the one Rig Fit made "
        "inside your mesh. Arma needs the mesh in its own rest pose. This node moves every point back: it applies the "
        "inverse of the bone matrices that carry the Arma skeleton to the fitted one, blended with the weights of the "
        "point. The weights are not changed.",
        "Connect your weighted mesh to input 1, the output of Rig Fit as it is to input 2 (its fitted skeleton is used) "
        "and the Arma skeleton to input 3. The move of each bone is worked out from the joint positions only: how the "
        "bone turned, and how much longer or shorter it is along the bone (a longer arm is not a thicker arm). A perfect fit "
        "moves nothing. The message gives the mean and the largest move of the points, and the bones whose points "
        "moved most. The Arma rest pose has the pelvis at the origin, so these moves include the walk from where your "
        "character stands to there, about a metre.",
        "A rigid piece (a helmet or a pouch made by Accessory Weights in Rigid mode) is moved by one matrix for all its "
        "points, so it keeps its shape. The unpose can only remove what the fitted skeleton describes: a landmark that is "
        "a few centimetres off in Rig Fit leaves the mesh a few centimetres off here.",
    ),
    "rtm_import": (
        "Plays an Arma animation (a plain or binarised .rtm file, or one of the animations in the game's own PBO files) on the Arma skeleton, so you can see your rigged mesh move the way it "
        "will in the game. Connect the Arma skeleton from Rig Skeleton to the input. The output is the same skeleton with "
        "its joints posed for the current frame. Put it into the third input of Bone Deform (the Arma skeleton in the "
        "second) with your mesh from Weights Convert, then press Play.",
        "An RTM holds key frames with a phase from 0 to 1, not a speed: the speed is in the game's config. So you choose how "
        "long one cycle is, in timeline frames (the default is 24) or in seconds, and where it starts. At the end it loops "
        "or holds. The animation's displacement (how far a walk moves the character in one cycle) is left out unless you "
        "switch Apply Displacement on. Between two key frames the pose of each joint, relative to its parent, is blended.",
        "Bones in the file that the skeleton lacks are ignored (face bones, slots, camera, and Weapon and launcher, which "
        "carry the weapon model in the game). A joint the file lacks stays at rest. The message of the node lists both and "
        "shows the frame in the cycle; the same numbers are on the geometry as the details a3ob_rtm_frame, a3ob_rtm_phase "
        "and a3ob_rtm_cycle. The animation does not depend on how the joints' own "
        "axes are turned: the node multiplies the animation onto them, so the mesh moves the same with any skeleton frames.",
        "To use an animation of the game, set Animation From to Arma 3 game, type some words of its name in Filter and pick it "
        "in the Animation menu (it names the PBO each one is in). Arma 3 Folder can stay empty: the node then looks at the "
        "environment variable A3OB_ARMA3, the A3OB settings file and your Steam library. The game stores its animations in "
        "binarised form (BMTR), which is stored relative to the parent bone; the node uses the parents of the skeleton to make "
        "the poses. Nothing from the game is copied.",
    ),
    "rig_check": (
        "Rig Check finds the weak places of a rigged mesh before you export. Connect your weighted mesh to input 1 and the "
        "skeleton to input 2 (the Arma skeleton, or the output of Rig Fit as it is). The mesh keeps its geometry and gains "
        "point attributes that say what is wrong: where each weight came from, weight jumps between neighbours, more than "
        "4 bones, a weight sum that is not 1, rigid pieces that straddle a joint, and how far each joint sits inside the "
        "mesh. The message of the node counts each problem and warns above the limits.",
        "The pose test bends the mesh into built-in test poses (arms raised, arms forward, elbows, hips, knees, spine, neck, "
        "wrists, or one key frame of an Arma .rtm file) with linear blend skinning, the way Arma does it, and measures two things. Stress is how much the edges of a "
        "point stretch or shrink (red and blue). Volume is the volume around each point against the rest mesh: purple where "
        "it is lost (a pinched elbow or knee, which needs smoother weights or more edge loops), teal where it is gained. "
        "Volume is worked out for each point as the volume of the cone from the point's own blend of joints to the triangles "
        "around it, which grows and shrinks with the area of the skin and its distance from the bone. The message names the "
        "joints with the worst loss.",
        "Switch on Visualize to colour the points by the Show entry in the viewport. It is a viewport colour only: the "
        "geometry, its Cd and the P3D export are not changed. The Output menu shows the posed mesh or the skeleton instead "
        "(a node of this kind has one output): use it to look, not to export.",
    ),
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
    "components": (
        "A Geometry LOD is built of convex pieces, and each piece needs a point group called Component01, Component02 "
        "and so on. This node makes those groups for you. It can also replace each piece by its convex hull. The "
        "pieces are numbered in the order of their first polygon in the geometry.",
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
        "The options are in tabs. A new node starts from the preset that is active in the Project root and presets "
        "tool. In Selection Mask, type group names separated by spaces; a name may use * and ?, and ^ before a name "
        "leaves it out. The menu lists the input's point groups: picking one adds it to the mask or removes it. The "
        "info line on the node says how many selections will be written.",
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
    "dir": "Folder path",
    "color": "Colour with alpha",
    "folder": "Group",
    "group": "Group picker",
}

#: What the three folder looks are called for an artist.
FOLDER_TEXT = {"simple": "group box", "collapsible": "group box you can fold", "tabs": "tab"}

#: Characters that make Houdini's help wiki read the text as markup (bold, italics, links, keys, code, variables).
_WIKI_MARKUP = re.compile(r"[*`\[\]]|<<|>>|\(\(|\)\)|^_|_$|__|\{\{\{")


def _split_run(run):
    """``(head, markup, tail)``: the sentence's own punctuation around a run stays outside the code span.

    That is a ``(`` before it, and ``.,;:`` and one ``)`` after it. A bracket pair that is itself markup (``((``,
    ``))``) is kept inside."""
    head = ""
    if run.startswith("(") and not run.startswith("(("):
        head, run = "(", run[1:]
    tail = ""
    stripped = re.search(r"[.,;:]+$", run)
    if stripped and stripped.start() > 0:
        run, tail = run[:stripped.start()], stripped.group(0)
    if run.endswith(")") and not run.endswith("))") and len(run) > 1:
        run, tail = run[:-1], ")" + tail
    return head, run, tail


def wiki_escape(text):
    """Text for the Help section that the wiki shows exactly as written.

    The wiki has no backslash escape, but it shows the text of a code span (between backticks) as it is. So every run
    of words that would read as markup (``* ^__*``) goes into one code span. A backtick in the text becomes a quote."""
    parts = re.split(r"(\s+)", text.replace("`", "'"))
    out, i = [], 0
    while i < len(parts):
        if parts[i] and not parts[i].isspace() and _WIKI_MARKUP.search(parts[i]):
            end = i
            while end + 2 < len(parts) and _WIKI_MARKUP.search(parts[end + 2]):
                end += 2
            head, run, tail = _split_run("".join(parts[i:end + 1]))
            out.append(head + "`" + run + "`" + tail)
            i = end + 1
        else:
            out.append(parts[i])
            i += 1
    return "".join(out)


def _value_words(parm, value):
    if parm is None:
        return value
    if parm.kind == "toggle":
        return "on" if value in ("1", "true", "True") else "off"
    for token, label in parm.items:
        if token == value:
            return label
    return value


def condition_text(index, conditions):
    """A ``show_when`` / ``enable_when`` in words: ``Mode is Uniform total or Set on each and Set Texture is on``.

    ``index`` maps parameter names to parameters (``NodeSpec.parm_index``, plus the row's own parameters)."""
    terms = []
    for name, values in conditions:
        parm = index.get(name)
        label = parm.label if parm is not None else name
        words = [_value_words(parm, value) for value in values]
        terms.append("%s is %s" % (label, " or ".join(words)))
    return " and ".join(terms)


def parm_index_for(spec, parm=None):
    """The parameters a condition may name: the node's, and (for a multiparm row parameter) the row's own."""
    index = dict(spec.parm_index)
    if parm is not None:
        index.update({child.name: child for child in parm.children})
    return index


def menu_texts(parm):
    """``(intro, {token: text})`` of a menu: the per-choice texts of ``item_help``, or the ones split out of its help.

    Every text starts with a capital letter."""
    intro, texts = split_menu_help(parm)
    texts = dict(texts)
    texts.update(dict(parm.item_help))
    return intro, {token: text[:1].upper() + text[1:] for token, text in texts.items() if text}


def number_text(value):
    value = float(value)
    return "%d" % value if value == int(value) else repr(value)


def default_text(parm):
    """The default of a parameter in words, or ``None`` for a button."""
    if parm.kind in ("button", "folder"):
        return None
    if parm.kind == "color":
        return "%s (red, green, blue, alpha)" % ", ".join(number_text(v) for v in parm.default)
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
    if parm.kind == "menu" and parm.menu_toggle:
        text = "Menu (pick several; each pick adds or removes an entry in the text)"
    if parm.kind == "group":
        text = ("Group picker (text; the menu lists the %s of the input, the arrow button picks in the viewport)"
                % ("groups of the kind the Group Type menu sets" if parm.group_follow else
                   "point groups and primitive groups of the input that are yours, without the bone groups (a primitive "
                   "group counts as the points of its primitives; typing a bone group name still works)"
                   if parm.group_type == "points" else "primitive groups"))
    if parm.kind == "folder":
        text = "Group (%s)" % FOLDER_TEXT[parm.folder_type]
    elif parm.size > 1 and parm.kind != "color":
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


def input_sentence(spec, first):
    """The one-line description of input ``first`` (1-based): ``spec.input_help``, or the label as a sentence."""
    if first - 1 < len(spec.input_help) and spec.input_help[first - 1]:
        return spec.input_help[first - 1]
    return "Connect %s here." % spec.input_labels[first - 1]


def inputs_text(spec):
    """The input count in words: ``None``, ``2``, ``1 to 3``."""
    low, high = spec.inputs
    if low == high:
        return "%d" % low if low else "None"
    return "%d to %d" % (low, high)


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
