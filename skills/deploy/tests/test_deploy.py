"""Offline checks for file boundaries, token isolation and remote mutations."""

import base64
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

SPEC = importlib.util.spec_from_file_location("deploy", Path(__file__).parents[1] / "scripts" / "deploy.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)
TOKEN = "test-pat-value-1234567890"


class FakeGitHub:
    """Remote state with unrelated files and optional concurrent updates."""
    def __init__(self, missing=False, unchanged=False, conflict=False):
        self.calls = []
        self.missing = missing
        self.unchanged = unchanged
        self.conflict = conflict
        self.head = "parent"
        self.files = {"README.md": "keep-readme", ".github/workflows/upstream.yml": "keep-workflow"}
        self.repo = {"full_name": "pat-owner/gost", "default_branch": "release/client"}

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/user":
            return {"login": "pat-owner"}
        if path == "/repos/pat-owner/gost":
            if self.missing:
                raise deploy.ApiError(method, path, 404)
            return self.repo
        if path.endswith("/forks"):
            self.missing = False
            return self.repo
        if "/git/ref/" in path:
            return {"object": {"sha": self.head}}
        if path.endswith("/git/commits/parent"):
            return {"tree": {"sha": "base-tree"}}
        if path.endswith("/actions/secrets/GH_PAT"):
            return {"name": "GH_PAT"}
        if path.endswith("/git/blobs"):
            return {"sha": deploy.git_blob_sha(base64.b64decode(body["content"]))}
        if path.endswith("/git/trees"):
            if body.get("base_tree") != "base-tree":
                self.files.clear()
            self.files.update({entry["path"]: entry["sha"] for entry in body["tree"]})
            return {"sha": "base-tree" if self.unchanged else "new-tree"}
        if path.endswith("/git/commits"):
            self.commit = body
            return {"sha": "new-commit"}
        if method == "PATCH" and "/git/refs/" in path:
            if self.conflict:
                self.head = "concurrent-commit"
                raise deploy.ApiError(method, path, 422, "Update is not a fast forward")
            self.head = body["sha"]
            return {"object": {"sha": self.head}}
        if path.endswith("/git/commits/new-commit"):
            return {"tree": {"sha": self.commit["tree"]}, "message": self.commit["message"]}
        raise AssertionError(f"Unexpected API request: {method} {path}")


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for name in deploy.UPLOAD_FILES:
            (self.root / name).write_bytes(b"client snapshot\r\n")
        workflow = self.root / ".github" / "workflows"
        workflow.mkdir(parents=True)
        (workflow / "ci.yml").write_text("on: workflow_dispatch\n")
        (self.root / ".github" / ".hidden").write_text("included")
        (self.root / "GH_PAT").write_text(TOKEN)
        (self.root / "server.go").write_text("excluded")
        self.output = io.StringIO()

    def files(self):
        return deploy.snapshot(self.root, TOKEN)

    def run_deploy(self, api):
        with redirect_stdout(self.output), patch.object(deploy, "set_secret") as secret:
            deploy.deploy(api, TOKEN, self.files(), "go-gost/gost", "gh")
        secret.assert_called_once_with(TOKEN, "pat-owner/gost", "gh")

    def test_upload_scope_preserves_bytes_and_includes_hidden_files(self):
        files = self.files()
        self.assertEqual(set(files), set(deploy.UPLOAD_FILES) | {".github/.hidden", ".github/workflows/ci.yml"})
        self.assertEqual(files["client.go"], b"client snapshot\r\n")

    def test_missing_required_file_fails_before_network(self):
        (self.root / "go.sum").unlink()
        with patch.object(deploy, "GitHub") as api, redirect_stderr(self.output):
            self.assertEqual(deploy.main(["--source", str(self.root)]), 1)
        api.assert_not_called()

    def test_token_inside_upload_is_blocked(self):
        (self.root / "client.yaml").write_text("token: " + TOKEN)
        with self.assertRaises(deploy.DeployError) as exc:
            self.files()
        self.assertNotIn(TOKEN, str(exc.exception))

    def test_nested_credential_file_is_blocked(self):
        (self.root / ".github" / "GH_PAT").write_text("another-token")
        with self.assertRaises(deploy.DeployError):
            self.files()

    def test_symlink_is_blocked(self):
        target = self.root / ".github" / "linked"
        try:
            target.symlink_to(self.root / "server.go")
        except OSError:
            self.skipTest("Symlink creation unavailable")
        with self.assertRaises(deploy.DeployError):
            self.files()

    def test_dry_run_with_empty_token_is_offline(self):
        (self.root / "GH_PAT").write_text("")
        with patch.object(deploy, "GitHub") as api, redirect_stdout(self.output):
            self.assertEqual(deploy.main(["--source", str(self.root), "--dry-run"]), 0)
        api.assert_not_called()
        self.assertIn("missing or empty", self.output.getvalue())

    def test_empty_token_does_not_fall_back_to_environment(self):
        (self.root / "GH_PAT").write_text("")
        with patch.dict(os.environ, {"GH_TOKEN": TOKEN}), patch.object(deploy, "GitHub") as api, redirect_stderr(self.output):
            self.assertEqual(deploy.main(["--source", str(self.root)]), 1)
        api.assert_not_called()

    def test_secret_is_stdin_only_and_ambient_account_is_overridden(self):
        with patch.dict(os.environ, {"GH_TOKEN": "wrong", "GH_DEBUG": "api", "GH_HOST": "elsewhere"}), patch.object(deploy.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
            deploy.set_secret(TOKEN, "pat-owner/gost", "gh")
        args, kwargs = run.call_args
        self.assertNotIn(TOKEN, " ".join(args[0]))
        self.assertEqual(kwargs["input"], TOKEN.encode())
        self.assertEqual(kwargs["env"]["GH_TOKEN"], TOKEN)
        self.assertEqual(kwargs["env"]["GH_HOST"], "github.com")
        self.assertNotIn("GH_DEBUG", kwargs["env"])

    def test_secret_failure_redacts_token(self):
        with patch.object(deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", TOKEN.encode())):
            with self.assertRaises(deploy.DeployError) as exc:
                deploy.set_secret(TOKEN, "pat-owner/gost", "gh")
        self.assertNotIn(TOKEN, str(exc.exception))

    def test_existing_repo_single_commit_keeps_remote_files(self):
        api = FakeGitHub()
        self.run_deploy(api)
        self.assertEqual(api.files["README.md"], "keep-readme")
        self.assertEqual(api.files[".github/workflows/upstream.yml"], "keep-workflow")
        self.assertNotIn("GH_PAT", api.files)
        commits = [body for method, path, body in api.calls if method == "POST" and path.endswith("/git/commits")]
        self.assertEqual(commits, [{"message": "Add files via upload", "tree": "new-tree", "parents": ["parent"]}])
        self.assertFalse(any(path.endswith("/forks") for _, path, _ in api.calls))
        self.assertTrue(any(path.endswith("heads/release%2Fclient") for _, path, _ in api.calls))
        self.assertNotIn(TOKEN, self.output.getvalue())

    def test_absent_repo_forks_chosen_upstream(self):
        api = FakeGitHub(missing=True)
        self.run_deploy(api)
        self.assertIn(("POST", "/repos/go-gost/gost/forks", {"name": "gost"}), api.calls)

    def test_fork_waits_until_git_ref_exists(self):
        repo = {"full_name": "pat-owner/gost", "default_branch": "main"}
        api = Mock()
        api.request.side_effect = [
            deploy.ApiError("GET", "/repos/pat-owner/gost", 404), repo, repo,
            deploy.ApiError("GET", "/ref", 409), repo, {"object": {"sha": "ready"}},
        ]
        with patch.object(deploy.time, "sleep") as sleep, redirect_stdout(self.output):
            self.assertEqual(deploy.resolve_repo(api, "pat-owner/gost", "go-gost/gost")[2], "ready")
        sleep.assert_called_once_with(3)

    def test_unchanged_snapshot_updates_secret_without_commit(self):
        api = FakeGitHub(unchanged=True)
        self.run_deploy(api)
        self.assertFalse(any(method == "PATCH" or path.endswith("/git/commits") for method, path, _ in api.calls))

    def test_concurrent_update_stops_without_force_or_retry(self):
        api = FakeGitHub(conflict=True)
        with self.assertRaises(deploy.ApiError):
            self.run_deploy(api)
        updates = [body for method, _, body in api.calls if method == "PATCH"]
        self.assertEqual(updates, [{"sha": "new-commit", "force": False}])
        self.assertEqual(api.head, "concurrent-commit")

    def test_secret_failure_prevents_code_upload(self):
        api = FakeGitHub()
        with patch.object(deploy, "set_secret", side_effect=deploy.DeployError("permission denied")), redirect_stdout(self.output):
            with self.assertRaises(deploy.DeployError):
                deploy.deploy(api, TOKEN, self.files(), "go-gost/gost", "gh")
        self.assertFalse(any(method != "GET" for method, _, _ in api.calls))


if __name__ == "__main__":
    unittest.main()
