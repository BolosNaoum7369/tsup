#!/usr/bin/env python3
"""Upload the go-tunnel client snapshot to the PAT owner's gost repository."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


UPLOAD_FILES = ("client.go", "client.yaml", "common.go", "go.mod", "go.sum")
COMMIT_MESSAGE = "Add files via upload"


class DeployError(Exception):
    pass


class ApiError(DeployError):
    def __init__(self, method, path, status, detail=""):
        self.status = status
        super().__init__(f"GitHub {method} {path}: HTTP {status}. {detail}")


def redact(text, token):
    return text.replace(token, "[REDACTED]") if token else text


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Do not forward credentials to another host or silently change the target.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, method, path, body=None):
        request = urllib.request.Request(
            "https://api.github.com" + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2026-03-10",
                "User-Agent": "go-tunnel-deploy",
            },
            method=method,
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                data = response.read()
                return json.loads(data) if data else {}
        except urllib.error.HTTPError as exc:
            try:
                detail = str(json.loads(exc.read()).get("message", ""))
            except (ValueError, UnicodeError):
                detail = ""
            raise ApiError(method, path, exc.code, redact(detail, self.token)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise DeployError(
                f"Network error during {method} {path}; check remote state before retrying."
            ) from None


def read_token(root, required=True):
    path = root / "GH_PAT"
    if not path.is_file():
        if required:
            raise DeployError("Missing GH_PAT file in the source directory.")
        return ""
    token = path.read_text(encoding="utf-8-sig").strip()
    if not token and required:
        raise DeployError("GH_PAT is empty. Fill the local file with a valid PAT first.")
    if token and (not token.isascii() or any(c.isspace() for c in token)):
        raise DeployError("GH_PAT must contain one token, without quotes or embedded whitespace.")
    return token


def check_path(path, root):
    flags = getattr(path.lstat(), "st_file_attributes", 0)
    if path.is_symlink() or flags & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise DeployError(f"Upload paths must not be links: {path.relative_to(root)}")
    if not path.resolve().is_relative_to(root):
        raise DeployError("An upload path resolves outside the source directory.")


def snapshot(root, token):
    paths = [root / name for name in UPLOAD_FILES]
    github = root / ".github"
    if not github.is_dir():
        raise DeployError("Missing .github directory.")
    check_path(github, root)
    github_files = []
    for directory, dirs, files in os.walk(github, followlinks=False):
        for name in dirs + files:
            check_path(Path(directory) / name, root)
        github_files.extend(Path(directory) / name for name in sorted(files))
    if not github_files:
        raise DeployError("The .github directory contains no files.")
    paths.extend(github_files)
    result = {}
    for path in sorted(paths):
        if not path.is_file():
            raise DeployError(f"Missing upload file: {path.relative_to(root)}")
        check_path(path, root)
        name = path.relative_to(root).as_posix()
        if path.name.casefold() == "gh_pat":
            raise DeployError(f"Credential file found in upload paths: {name}")
        data = path.read_bytes()
        if token and token.encode("ascii") in data:
            raise DeployError(f"GH_PAT value found inside upload file: {name}")
        result[name] = data
    return result


def git_blob_sha(data):
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def validate_repo(repo, target):
    if repo.get("full_name", "").casefold() != target.casefold():
        raise DeployError(f"GitHub returned a repository other than {target}.")
    if repo.get("archived") or repo.get("disabled"):
        raise DeployError(f"Repository {target} is archived or disabled.")


def resolve_repo(api, target, upstream):
    path = f"/repos/{target}"
    created = False
    try:
        repo = api.request("GET", path)
    except ApiError as exc:
        if exc.status != 404:
            raise
        if not upstream:
            raise DeployError(
                f"{target} was not found. Specify --upstream OWNER/REPO to fork it; "
                "also check that this PAT can access the target."
            ) from None
        print(f"Forking {upstream} into {target} ...", flush=True)
        repo = api.request("POST", f"/repos/{upstream}/forks", {"name": "gost"})
        validate_repo(repo, target)
        created = True
    deadline = time.monotonic() + 120
    while True:
        try:
            repo = api.request("GET", path) if created else repo
            validate_repo(repo, target)
            branch = repo["default_branch"]
            ref = api.request("GET", f"{path}/git/ref/heads/{urllib.parse.quote(branch, safe='')}")
            return repo, branch, ref["object"]["sha"]
        except ApiError as exc:
            if not created or exc.status not in (404, 409) or time.monotonic() >= deadline:
                raise
            print("Waiting for the fork's default branch ...", flush=True)
            time.sleep(3)


def set_secret(token, target, gh):
    env = dict(os.environ)
    for key in list(env):
        if key.upper() in {
            "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
            "GH_DEBUG", "GH_HOST", "GH_REPO", "GH_FORCE_TTY",
        }:
            del env[key]
    env.update(GH_TOKEN=token, GH_HOST="github.com", GH_PROMPT_DISABLED="1")
    try:
        result = subprocess.run(
            [gh, "secret", "set", "GH_PAT", "--app", "actions", "--repo", f"github.com/{target}"],
            input=token.encode("ascii"), capture_output=True, env=env, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise DeployError("Setting GH_PAT timed out; check the remote secret before retrying.") from None
    if result.returncode:
        detail = redact(result.stderr.decode("utf-8", errors="replace"), token).strip()
        raise DeployError(f"Could not set Actions secret GH_PAT: {detail}")


def deploy(api, token, files, upstream, gh):
    owner = api.request("GET", "/user")["login"]
    target = f"{owner}/gost"
    _, branch, parent = resolve_repo(api, target, upstream)
    prefix = f"/repos/{target}"
    print(f"Target: https://github.com/{target} (branch: {branch})", flush=True)
    base = api.request("GET", f"{prefix}/git/commits/{parent}")["tree"]["sha"]
    set_secret(token, target, gh)
    if api.request("GET", f"{prefix}/actions/secrets/GH_PAT").get("name") != "GH_PAT":
        raise DeployError("Actions secret metadata verification failed.")
    print("Actions secret GH_PAT: updated and metadata verified.", flush=True)
    entries = []
    for name, data in files.items():
        blob = api.request("POST", f"{prefix}/git/blobs", {
            "content": base64.b64encode(data).decode("ascii"), "encoding": "base64",
        })
        if blob["sha"] != git_blob_sha(data):
            raise DeployError(f"Uploaded blob verification failed: {name}")
        entries.append({"path": name, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree = api.request("POST", f"{prefix}/git/trees", {"base_tree": base, "tree": entries})
    if tree["sha"] == base:
        print("Files already match; no empty commit created.")
        return
    commit = api.request("POST", f"{prefix}/git/commits", {
        "message": COMMIT_MESSAGE, "tree": tree["sha"], "parents": [parent],
    })
    ref = f"heads/{urllib.parse.quote(branch, safe='')}"
    api.request("PATCH", f"{prefix}/git/refs/{ref}", {"sha": commit["sha"], "force": False})
    current = api.request("GET", f"{prefix}/git/ref/{ref}")
    if current["object"]["sha"] != commit["sha"]:
        raise DeployError("The branch changed during verification; inspect the remote commit.")
    verified = api.request("GET", f"{prefix}/git/commits/{commit['sha']}")
    if verified["tree"]["sha"] != tree["sha"] or verified["message"] != COMMIT_MESSAGE:
        raise DeployError("Remote commit verification failed.")
    print(f"Commit verified: https://github.com/{target}/commit/{commit['sha']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd(), help="Project directory (default: current directory)")
    parser.add_argument("--upstream", default="go-gost/gost", help="Fork source if gost is missing (default: go-gost/gost)")
    parser.add_argument("--dry-run", action="store_true", help="Validate local files only; no network access or mutations")
    args = parser.parse_args(argv)
    token = ""
    try:
        if args.upstream and not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", args.upstream):
            raise DeployError("--upstream must be OWNER/REPO, without a URL or credentials.")
        root = args.source.resolve()
        token = read_token(root, required=not args.dry_run)
        files = snapshot(root, token)
        print(f"Source: {root}\nCommit message: {COMMIT_MESSAGE}")
        for name, data in files.items():
            print(f"  {name} ({len(data)} bytes)")
        if args.dry_run:
            print(f"Local preview complete. GH_PAT: {'present (not authenticated)' if token else 'missing or empty'}. No network requests made.")
            return 0
        gh = shutil.which("gh")
        if not gh:
            raise DeployError("GitHub CLI (gh) is required to encrypt and upload the Actions secret.")
        deploy(GitHub(token), token, files, args.upstream, gh)
        return 0
    except (DeployError, OSError, UnicodeError, ValueError, KeyError) as exc:
        # Never dump API bodies, subprocess environments or tracebacks containing secrets.
        print("Deployment failed: " + redact(str(exc), token), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
