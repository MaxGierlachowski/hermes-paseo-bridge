import dataclasses
import os
import re
import unittest

import helpers  # noqa: F401  (sys.path)

from paseo_bridge.config import ENV_OVERRIDES, Settings
from paseo_bridge.version import VERSION

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with open(os.path.join(ROOT, name)) as fh:
        return fh.read()


class ReleaseTest(unittest.TestCase):
    def test_versions_agree(self):
        self.assertEqual(re.search(r'^version = "(.+)"$', read("pyproject.toml"), re.M).group(1), VERSION)
        self.assertEqual(re.search(r"^version: (.+)$", read("plugin.yaml"), re.M).group(1).strip(), VERSION)

    def test_every_documented_setting_is_in_the_manifest(self):
        manifest = set(re.findall(r"^  (\w+):", read("plugin.yaml"), re.M))
        internal = {"tolerance_seconds", "max_body_bytes"}
        settings = {f.name for f in dataclasses.fields(Settings)} - internal
        self.assertEqual(settings - manifest, set())
        self.assertEqual(manifest - settings - {"instructions"}, set())
        self.assertTrue(set(ENV_OVERRIDES) <= settings)

    def test_readme_documents_every_env_override(self):
        readme = read("README.md")
        for key in ENV_OVERRIDES:
            self.assertIn(f"HERMES_PASEO_BRIDGE_{key.upper()}", readme, key)


if __name__ == "__main__":
    unittest.main()
