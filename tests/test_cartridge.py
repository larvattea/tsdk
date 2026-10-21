import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tsdk.cartridge import extract_cartridge, inspect_cartridge, localized
from tsdk import cartridge


class CartridgeBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = {
            "id": "example.boundary",
            "version": "1.0.0",
            "entrypoint": "game:Game",
            "tracking_profile": "body",
            "game_api": 1,
            "requires_tsdk": ">=0.2,<0.3",
            "name": {"en": "Boundary"},
            "assets": [],
            "licenses": ["LICENSE"],
            "format_version": 1,
            "builder": {"version": "0.1.0"},
            "build": {"source_commit": "a" * 40, "dirty": False},
        }

    def archive(self, entries):
        path = self.root / "game.ttea"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps(self.manifest))
            archive.writestr("game.py", "class Game: pass\n")
            archive.writestr("LICENSE", "Synthetic test fixture.\n")
            for name, content in entries:
                archive.writestr(name, content)
        return path

    def test_traversal_never_writes_outside_destination(self):
        archive = self.archive([("../escaped.py", "bad")])
        with self.assertRaises(ValueError):
            extract_cartridge(archive, self.root / "installed")
        self.assertFalse((self.root / "escaped.py").exists())

    def test_portable_source_and_notices_extract_intact(self):
        archive = self.archive([("assets/level.json", '{"level": 1}')])
        self.assertEqual(inspect_cartridge(archive)["id"], "example.boundary")
        destination = self.root / "installed"
        extract_cartridge(archive, destination)
        self.assertEqual((destination / "game.py").read_text(), "class Game: pass\n")
        self.assertEqual((destination / "LICENSE").read_text(), "Synthetic test fixture.\n")

    def test_symlink_is_not_extracted(self):
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive = self.archive([(link, "../outside")])
        with self.assertRaises(ValueError):
            extract_cartridge(archive, self.root / "installed")
        self.assertFalse((self.root / "installed" / "link").is_symlink())

    def test_case_collisions_are_rejected_on_every_platform(self):
        archive = self.archive([("Asset.txt", "one"), ("asset.txt", "two")])
        with self.assertRaises(ValueError):
            inspect_cartridge(archive)

    def test_native_extension_is_rejected(self):
        archive = self.archive([("extension.so", b"native")])
        with self.assertRaises(ValueError):
            inspect_cartridge(archive)

    def test_incompatible_runtime_is_rejected(self):
        self.manifest["requires_tsdk"] = ">=99"
        with self.assertRaises(ValueError):
            inspect_cartridge(self.archive([]))

    def test_resolution_is_two_positive_pixel_sizes(self):
        self.manifest["resolution"] = [800, 600]
        self.assertEqual(inspect_cartridge(self.archive([]))["resolution"], [800, 600])
        for resolution in ([800], [800, 0], [800.0, 600], [True, 600], "800x600", [800, 600, 1]):
            with self.subTest(resolution=resolution):
                self.manifest["resolution"] = resolution
                with self.assertRaises(ValueError):
                    inspect_cartridge(self.archive([]))

    def test_archive_resource_limits(self):
        cases = [
            ("MAX_FILES", 2, []),
            ("MAX_FILE_SIZE", 10, []),
            ("MAX_TOTAL_SIZE", 10, []),
            ("MAX_MANIFEST_SIZE", 10, []),
        ]
        for setting, limit, entries in cases:
            with self.subTest(setting=setting), patch.object(cartridge, setting, limit):
                with self.assertRaises(ValueError):
                    extract_cartridge(self.archive(entries), self.root / "installed")
                self.assertFalse((self.root / "installed").exists())
        compressed = zipfile.ZipInfo("bomb.txt")
        compressed.compress_type = zipfile.ZIP_DEFLATED
        with self.assertRaisesRegex(ValueError, "ratio"):
            inspect_cartridge(self.archive([(compressed, b"0" * 100_000)]))

    def test_collisions_and_nonportable_names(self):
        cases = [
            [("folder", "file"), ("folder/data", "child")],
            [("Folder/a", "a"), ("folder/b", "b")],
            [("caf\u00e9.txt", "a"), ("cafe\u0301.txt", "b")],
        ]
        cases += [[(name, "bad")] for name in (
            "CON.txt", "COM¹.txt", "LPT²", "CONIN$", "CONOUT$",
            "trailing.", "trailing ", "stream:part", "question?.txt",
        )]
        # zipfile rewrites "\\" to "/" on Windows unless the name is set after ZipInfo creation.
        backslash = zipfile.ZipInfo("placeholder")
        backslash.filename = "back\\slash"
        cases.append([(backslash, "bad")])
        for entries in cases:
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                inspect_cartridge(self.archive(entries))

    def test_unsupported_compression_and_special_entries(self):
        compressed = zipfile.ZipInfo("unsupported.txt")
        compressed.compress_type = zipfile.ZIP_BZIP2
        fifo = zipfile.ZipInfo("pipe")
        fifo.create_system = 3
        fifo.external_attr = (stat.S_IFIFO | 0o600) << 16
        for entry in (compressed, fifo):
            with self.subTest(entry=entry.filename), self.assertRaises(ValueError):
                inspect_cartridge(self.archive([(entry, "content")]))

    def test_corrupt_member_preserves_destination(self):
        archive = self.archive([("payload.txt", "unique payload marker")])
        data = archive.read_bytes().replace(b"unique payload marker", b"broken payload marker")
        archive.write_bytes(data)
        destination = self.root / "installed"
        destination.mkdir()
        with self.assertRaisesRegex(ValueError, "CRC"):
            extract_cartridge(archive, destination)
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(list(self.root.glob(".ttea-extract-*")), [])

    def test_interrupted_extraction_cleans_private_tree(self):
        archive = self.archive([])
        with patch.object(cartridge.shutil, "copyfileobj", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                extract_cartridge(archive, self.root / "installed")
        self.assertFalse((self.root / "installed").exists())
        self.assertEqual(list(self.root.glob(".ttea-extract-*")), [])


class LocalizedTest(unittest.TestCase):
    def test_localized_prefers_exact_then_language_then_first(self):
        texts = {"en": "Kart", "pt-BR": "Carrinho", "es": "Coche"}
        self.assertEqual(localized(texts, "pt_BR"), "Carrinho")
        self.assertEqual(localized({"en": "Kart", "pt": "Carro"}, "pt_BR"), "Carro")
        self.assertEqual(localized(texts, "de_DE"), "Kart")
        self.assertEqual(localized({}, "en", "fallback"), "fallback")


if __name__ == "__main__":
    unittest.main()
