"""Compatibility tests for unrpyc's fake package import hook."""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from uuid import uuid4


def load_magic():
    project_root = Path(__file__).resolve().parents[1]
    unrpyc_dir = project_root / "unrpyc-master"
    if str(unrpyc_dir) not in sys.path:
        sys.path.insert(0, str(unrpyc_dir))
    return importlib.import_module("decompiler.magic")


class FakePackageLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.magic = load_magic()
        self.root = f"_fake_import_probe_{uuid4().hex}"

    def tearDown(self) -> None:
        if self.root in sys.modules:
            self.magic.remove_fake_package(self.root)

    def test_modern_protocol_supports_nested_packages_and_cleanup(self) -> None:
        loader = self.magic.FakePackageLoader(self.root)
        sys.meta_path.insert(0, loader)
        try:
            module = importlib.import_module(f"{self.root}.nested.leaf")

            self.assertIsInstance(module, self.magic.FakePackage)
            self.assertEqual(module.__name__, f"{self.root}.nested.leaf")
            self.assertIs(module, sys.modules[module.__name__])
            self.assertIs(module.__loader__, loader)
            self.assertEqual(module.__package__, module.__name__)
            self.assertEqual(module.__path__, [])
            self.assertEqual(module.__spec__.submodule_search_locations, [])
            self.assertIs(
                getattr(sys.modules[f"{self.root}.nested"], "leaf"),
                module,
            )
        finally:
            self.magic.remove_fake_package(self.root)

        self.assertNotIn(self.root, sys.modules)
        self.assertNotIn(f"{self.root}.nested", sys.modules)
        self.assertNotIn(f"{self.root}.nested.leaf", sys.modules)
        self.assertNotIn(loader, sys.meta_path)

    def test_legacy_methods_remain_available_for_supported_old_runtimes(self) -> None:
        loader = self.magic.FakePackageLoader(self.root)
        self.assertIs(loader.find_module(self.root), loader)

        module = loader.load_module(self.root)
        self.assertIsInstance(module, self.magic.FakePackage)
        self.assertIs(module, sys.modules[self.root])
