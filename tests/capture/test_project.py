import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.capture import ROOT
from memo_capture import project, settings


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class ResolveTest(unittest.TestCase):
    def setUp(self):
        project.resolve.cache_clear()
        self.base = Path(tempfile.mkdtemp(prefix="memo-project-test-"))

    def test_git_toplevel_name_from_subfolder(self):
        repo = self.base / "demo-repo"
        (repo / "src").mkdir(parents=True)
        git(repo, "init", "-q")
        p = project.resolve(str(repo / "src"))
        self.assertEqual((p.name, p.source), ("demo-repo", "git"))

    def test_worktree_is_parent_slash_child(self):
        repo = self.base / "main-repo"
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
        git(repo, "worktree", "add", "-q", str(self.base / "feature-x"))
        p = project.resolve(str(self.base / "feature-x"))
        self.assertEqual((p.name, p.source), ("main-repo/feature-x", "worktree"))

    def test_environment_mapping_wins(self):
        folder = self.base / "client-work" / "deep"
        folder.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"MEMO_PROJECT_ENVIRONMENTS": json.dumps({str(self.base / "client-work"): "acme"})}):
            settings.load(refresh=True)
            self.assertEqual(project.resolve(str(folder)).name, "acme")
        settings.load(refresh=True)

    def test_plain_folder_uses_its_name(self):
        folder = self.base / "notes"
        folder.mkdir()
        self.assertEqual(project.resolve(str(folder)).name, "notes")

    def test_origin_slug(self):
        self.assertEqual(project.parse_origin_slug("git@github.com:aboalrejalai/openmemo.git"), "aboalrejalai/openmemo")
        self.assertEqual(project.parse_origin_slug("https://github.com/aboalrejalai/openmemo"), "aboalrejalai/openmemo")
        self.assertIsNone(project.parse_origin_slug("/local/path.git"))

    def test_folder_key_is_safe(self):
        self.assertEqual(project.folder_key("main-repo/feature x"), "main-repo/feature-x")
        self.assertEqual(project.folder_key("../.."), "unknown-project/unknown-project")


class VaultNoteTest(unittest.TestCase):
    def test_real_vault_mapping(self):
        p = project.Project("demo-project", "git", "/x")
        self.assertEqual(project.vault_note(p, ROOT), "projects/demo-project.md")
        self.assertEqual(project.vault_note(project.Project("demo-project/feature-x", "worktree", "/x"), ROOT),
                         "projects/demo-project.md")
        self.assertIsNone(project.vault_note(project.Project("no-such-project-zz", "cwd", "/x"), ROOT))

    def test_repo_url_and_marker_key(self):
        vault = Path(tempfile.mkdtemp(prefix="memo-vault-test-"))
        (vault / "projects").mkdir()
        (vault / "projects" / "shop.md").write_text("---\ntitle: Shop\nrepo: https://github.com/me/store-web\n---\n")
        (vault / "projects" / "other.md").write_text("---\ntitle: O\nmemo_project: weird-name\n---\n")
        self.assertEqual(project.vault_note(project.Project("store-web", "git", "/x"), vault), "projects/shop.md")
        self.assertEqual(project.vault_note(project.Project("weird-name", "cwd", "/x"), vault), "projects/other.md")


if __name__ == "__main__":
    unittest.main()
