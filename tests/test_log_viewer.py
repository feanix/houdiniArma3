"""The a3ob logger goes to Houdini's Log Viewer, never to stderr or stdout (task 46).

Purpose: catch a handler that is missing (Houdini on Windows then opens its console for each warning), a second handler
after Reload A3OB (every line twice), a cut-off line that still says "console", and a node that re-sends its full
list on every re-cook.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import contextlib
import importlib
import io
import logging
import os
import subprocess
import sys
import unittest

from a3ob_houdini import hda_source, log_viewer, messages, nodes

from tests.hou_support import FIXTURES, HAS_HOU, REPO_ROOT, hou, install_hdas_for_class, uninstall_hdas

OTLS_DIR = os.path.join(REPO_ROOT, "houdini", "otls")


class TestPlainPython(unittest.TestCase):
    @unittest.skipIf(HAS_HOU, "plain Python only (hython prints its own start-up lines)")
    def test_importing_the_package_writes_nothing(self):
        script = ("import logging\nimport a3ob_houdini\n"
                  "logging.getLogger('a3ob').warning('quiet')\n"
                  "handlers = logging.getLogger('a3ob').handlers\n"
                  "assert [type(h).__name__ for h in handlers] == ['NullHandler'], handlers\n")
        env = dict(os.environ, PYTHONPATH=os.path.join(REPO_ROOT, "python"))
        done = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((done.stdout, done.stderr), ("", ""))

    def test_the_cut_off_line_says_where_the_rest_is(self):
        report = messages.Report()
        for number in range(5):
            report.warning("w%d" % number)
        last = report.lines(3)[-1][1]
        self.assertEqual(last, "3 more messages are not shown. See Windows > Log Viewer (source A3OB) for the full list.")
        self.assertNotIn("console", last.lower())

    def test_install_twice_keeps_one_handler(self):
        before = list(logging.getLogger("a3ob").handlers)
        log_viewer.install()
        log_viewer.install()
        self.assertEqual(logging.getLogger("a3ob").handlers, before)


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestInHython(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        paths = [os.path.join(OTLS_DIR, hda_source.hda_folder_name(nodes.load(kind).SPEC))
                 for kind in ("validate", "p3d_import")]
        cls.installed = install_hdas_for_class(paths)
        cls.container = hou.node("/obj").createNode("geo", "log_viewer_tests")

    @classmethod
    def tearDownClass(cls):
        cls.container.destroy()
        uninstall_hdas(cls.installed)

    def setUp(self):
        self.sink = hou.logging.MemorySink()
        self.sink.connect(log_viewer.SOURCE)
        self.sink.stealLogEntries()

    def tearDown(self):
        self.sink.disconnect(log_viewer.SOURCE)

    def validate(self, max_messages):
        imported = self.container.createNode("a3ob_p3d_import")
        imported.parm("file").set(os.path.join(FIXTURES, "sample_2_crate.p3d"))
        node = self.container.createNode("a3ob_validate")
        node.setInput(0, imported)
        node.parm("maxmessages").set(max_messages)
        return node

    def cook(self, node):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                node.cook(force=True)
            except hou.OperationFailed:
                pass
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        return [entry.message() for entry in self.sink.stealLogEntries()]

    def test_the_handler_is_the_log_viewer_one(self):
        names = [type(handler).__name__ for handler in logging.getLogger("a3ob").handlers]
        self.assertEqual(names, ["_LogViewerHandler"])
        self.assertIn(log_viewer.SOURCE, hou.logging.sources())

    def test_a_cook_with_warnings_writes_nothing_and_logs_nothing_when_nothing_was_cut(self):
        node = self.validate(25)
        self.assertEqual(self.cook(node), [])
        self.assertEqual(len(node.warnings()), 1)

    def test_the_full_list_is_sent_once_when_messages_were_cut(self):
        node = self.validate(1)
        first = self.cook(node)
        self.assertEqual(len(first), 2, first)  # the warning and the summary line, both in full
        self.assertTrue(all(text.startswith(node.path()) for text in first))
        self.assertTrue(any("more messages are not shown" in text for text in node.messages()))
        self.assertEqual(self.cook(node), [])  # an identical re-cook sends nothing
        report = messages.Report()
        report.warning("a new problem")
        report.warning("another one")

        class FakeNode:  # a node accepts messages only while it cooks
            path = staticmethod(lambda: node.path())
            addError = addWarning = addMessage = staticmethod(lambda text: None)

        messages.show_on_node(FakeNode, report, 1)
        self.assertEqual(len(self.sink.stealLogEntries()), 2)  # a different list is sent again

    def test_reload_does_not_add_a_second_handler(self):
        logger = logging.getLogger("a3ob")
        before = list(logger.handlers)
        importlib.reload(log_viewer)
        log_viewer.install()
        self.assertEqual(logger.handlers, before)
        self.assertEqual(len(self.cook(self.validate(1))), 2)  # one line per message, not two


if __name__ == "__main__":
    unittest.main()
