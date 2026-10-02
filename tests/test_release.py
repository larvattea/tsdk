import importlib.metadata
from pathlib import Path
import subprocess
import tempfile
import unittest

from tsdk.checks import check
from tsdk.cli import build
from tsdk.template import new_project
from ttea.sdk.cartridge import inspect_cartridge


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = new_project(Path(self.temp.name) / 'game', 'example.release', 'Release', 'body')
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'Synthetic release fixture')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_release_records_installed_builder_and_exact_source(self):
        manifest = inspect_cartridge(build(self.root, release=True))
        self.assertEqual(manifest['builder']['version'], importlib.metadata.version('tsdk'))
        self.assertEqual(manifest['build'], {'source_commit': self.git('rev-parse', 'HEAD'), 'dirty': False})

    def test_dirty_release_does_not_replace_existing_artifact(self):
        artifact = build(self.root, release=True)
        before = artifact.read_bytes()
        with (self.root / 'game.py').open('a') as stream:
            stream.write('\n# Uncommitted fixture change\n')
        with self.assertRaisesRegex(ValueError, 'clean committed'):
            build(self.root, release=True)
        self.assertEqual(artifact.read_bytes(), before)

    def test_ignored_uncommitted_content_cannot_claim_clean_provenance(self):
        with (self.root / '.gitignore').open('a') as stream:
            stream.write('extra.py\n')
        self.git('add', '.gitignore')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'Ignore synthetic content')
        (self.root / 'extra.py').write_text('VALUE = 1\n')
        with self.assertRaisesRegex(ValueError, 'must be committed'):
            build(self.root, release=True)

    def test_output_cannot_overwrite_authored_metadata(self):
        for name in ('ttea.toml', 'pyproject.toml'):
            path = self.root / name
            before = path.read_bytes()
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'overwrite'):
                build(self.root, output=path)
            self.assertEqual(path.read_bytes(), before)

    def test_runtime_incompatible_callback_shapes_are_rejected(self):
        callbacks = [
            '    def update(self, context, *, required): pass\n',
            '    async def update(self, context): pass\n',
            '    @staticmethod\n    def update(self, context): pass\n',
        ]
        for callback in callbacks:
            (self.root / 'game.py').write_text('from ttea.sdk.api import Game as Base\nclass Game(Base):\n' + callback)
            with self.subTest(callback=callback), self.assertRaisesRegex(ValueError, 'update'):
                check(self.root)


if __name__ == '__main__':
    unittest.main()
