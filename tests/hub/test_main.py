import os
import unittest
from unittest import mock

from hub import __main__ as hub_main


class HubMainTest(unittest.TestCase):
    def test_port_defaults_to_9999(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HUB_PORT", None)
            self.assertEqual(hub_main.hub_port(), 9999)

    def test_port_from_env(self):
        with mock.patch.dict(os.environ, {"HUB_PORT": "18999"}):
            self.assertEqual(hub_main.hub_port(), 18999)

    def test_ffmpeg_on_path(self):
        with mock.patch.object(hub_main.shutil, "which", return_value="/usr/bin/ffmpeg"):
            self.assertTrue(hub_main.ensure_ffmpeg())

    def test_ffmpeg_next_to_interpreter_is_added_to_path(self):
        def which(name, path=None):
            return "found" if path is not None else None

        with mock.patch.dict(os.environ, {"PATH": "orig"}), \
                mock.patch.object(hub_main.shutil, "which", side_effect=which):
            self.assertTrue(hub_main.ensure_ffmpeg())
            self.assertTrue(os.environ["PATH"].endswith(os.pathsep + "orig"))

    def test_missing_ffmpeg_exits_1_without_starting(self):
        with mock.patch.object(hub_main, "ensure_ffmpeg", return_value=False), \
                mock.patch("uvicorn.run") as run, \
                mock.patch("logging.basicConfig"), \
                mock.patch.object(hub_main.sys, "stdout"), \
                mock.patch.object(hub_main.sys, "stderr"):
            self.assertEqual(hub_main.main(), 1)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
