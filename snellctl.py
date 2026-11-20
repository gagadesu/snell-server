#!/usr/bin/env python3
"""Local menu for building and preparing the Snell deployment."""

import hashlib
import io
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import termios
import time
import tty
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = Path.home() / ".config" / "snell-server"
RELEASES = "https://kb.nssurge.com/surge-knowledge-base/release-notes/snell.md"
VERSION = r"v\d+\.\d+\.\d+"
IMAGE = r"docker\.io/[a-z0-9_-]+/[a-z0-9._-]+@sha256:[a-f0-9]{64}"
WORKFLOW = "build.yml"


def menu_key(fd):
    key = os.read(fd, 1)
    if not key or key == b"\x04":
        raise EOFError
    if key != b"\x1b":
        return key.decode("ascii", errors="ignore")
    if not select.select([fd], [], [], 0.1)[0]:
        return "escape"
    if os.read(fd, 1) not in (b"[", b"O"):
        return ""
    for _ in range(16):
        if not select.select([fd], [], [], 0.1)[0]:
            break
        suffix = os.read(fd, 1)
        if not suffix or b"@" <= suffix <= b"~":
            return {b"A": "up", b"B": "down"}.get(suffix, "")
    return ""


def choose(title, choices, *, default):
    keys = list(choices)
    current = keys.index(default)
    print(f"\n  {title}")
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or os.environ.get("TERM") == "dumb":
        for key, label in choices.items():
            print(f"  {key}  {label}")
        while True:
            answer = input(f"  Select [{default}]: ").strip() or default
            if answer in choices:
                return answer
            print("  Choose one of the listed options.")

    terminal = shutil.get_terminal_size()
    rows = min(len(keys), max(1, terminal.lines - 6))
    color = "\x1b[1;36m" if "NO_COLOR" not in os.environ else ""
    reset = "\x1b[0m" if color else ""
    print("  ↑ ↓ Move   ·   Enter Select   ·   Esc Back"[:terminal.columns - 1])

    fd = sys.stdin.fileno()
    settings = termios.tcgetattr(fd)
    rendered = False
    top = 0
    try:
        tty.setcbreak(fd)
        while True:
            top = max(current - rows + 1, min(top, current))
            if rendered:
                sys.stdout.write(f"\x1b[{rows}F")
            for index in range(top, top + rows):
                key = keys[index]
                selected = index == current
                marker = "›" if selected else " "
                line = f"  {marker}  {key}  {choices[key]}"[:terminal.columns - 1]
                sys.stdout.write(f"\x1b[2K{color if selected else ''}{line}{reset if selected else ''}\n")
            sys.stdout.flush()
            rendered = True

            while True:
                key = menu_key(fd)
                if key in ("\r", "\n"):
                    return keys[current]
                if key == "escape":
                    return None
                if key in ("up", "down"):
                    current = (current + (-1 if key == "up" else 1)) % len(keys)
                    break
                if key and key in choices:
                    current = keys.index(key)
                    break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, settings)


def public_env():
    return {key: value for key, value in os.environ.items() if not key.startswith("OP_")}


def output(argv, *, stdin=None):
    return subprocess.run(
        argv, input=stdin, stdout=subprocess.PIPE, check=True, env=public_env()
    ).stdout


def fetch(url):
    return output([
        "curl", "-fsSL", "--proto", "=https", "--proto-redir", "=https",
        "--max-time", "60", url,
    ])


def source_url(version):
    if not re.fullmatch(VERSION, version):
        raise ValueError("Expected an exact version such as v5.0.1.")
    return f"https://dl.nssurge.com/snell/snell-server-{version}-linux-amd64.zip"


def version_key(version):
    return tuple(map(int, version[1:].split(".")))


def read_releases():
    print("  Checking the official Snell release notes…", flush=True)
    notes = fetch(RELEASES).decode()
    headings = list(re.finditer(rf"(?m)^### ({VERSION})[ \t]*$", notes))
    if not headings:
        raise ValueError("No version headings found in the official release notes.")
    releases = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(notes)
        section = notes[heading.end():end]
        version = heading[1]
        if re.search(r"\b(?:stable release|generally available|out of beta)\b|正式发布", section, re.I):
            releases[version] = "stable"
        elif re.search(r"\bbeta\b|\bpre-release\b|release candidate", section, re.I):
            releases[version] = "beta"
        elif version_key(version) <= (5, 0, 1):
            # v5.0.1 is the last independently confirmed stable baseline.
            releases[version] = "stable"
        else:
            releases[version] = "unconfirmed"
    return dict(sorted(releases.items(), key=lambda item: version_key(item[0]), reverse=True))


def latest_release(releases, channel):
    return next((version for version, status in releases.items() if status == channel), None)


def show_releases():
    releases = read_releases()
    print(f"  Latest stable  {latest_release(releases, 'stable') or 'Unavailable'}")
    print(f"  Latest beta    {latest_release(releases, 'beta') or 'Unavailable'}")
    unknown = [version for version, channel in releases.items() if channel == "unconfirmed"]
    if unknown:
        print(f"  Unconfirmed    {', '.join(unknown)}")
    print(f"  {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}  ·  {RELEASES}")


def select_release(action):
    releases = read_releases()
    stable = latest_release(releases, "stable")
    beta = latest_release(releases, "beta")
    versions = {str(index): version for index, version in enumerate(releases, 1)}
    choices = {}
    default = "0"
    for key, version in versions.items():
        label = version
        if version == stable:
            label += " (latest stable)"
            default = key
        elif version == beta:
            label += " (latest beta)"
        elif releases[version] != "stable":
            label += f" ({releases[version]})"
        choices[key] = label
    return versions.get(choose(f"{action} / Select a version", {**choices, "0": "Back"}, default=default))


def load(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(handle.name, path)


def build(version, *, reuse=False):
    source = source_url(version)
    settings = load(STATE / "settings.json", {})
    repo = settings.get("github_repo", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Set github_repo to OWNER/REPO in the local settings.json.")
    scope = settings.get("dockerhub_repository", "")
    if not re.fullmatch(r"[a-z0-9_-]+/[a-z0-9._-]+", scope):
        raise ValueError("Set dockerhub_repository to namespace/repository in the local settings.json.")
    dockerfile_hash = hashlib.sha256((ROOT / "Dockerfile").read_bytes()).hexdigest()
    print(f"  Calculating the SHA-256 of the official Snell {version} archive…", flush=True)
    try:
        archive_hash = hashlib.sha256(fetch(source)).hexdigest()
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"Could not download the official archive for {version}. See {source}") from exc
    path = STATE / "builds" / f"{version}.json"
    record = load(path, {})
    if reuse and valid_build(record, version, archive_hash, dockerfile_hash, scope):
        print(f"  Reusing the saved image for {version}.")
        return record

    request = uuid.uuid4().hex
    run_title = f"Snell {version} ({request})"
    response = output([
        "gh", "workflow", "run", WORKFLOW, "--repo", repo,
        "-f", f"version={version}", "-f", f"archive_sha256={archive_hash}",
        "-f", f"dockerfile_sha256={dockerfile_hash}",
        "-f", f"request_id={request}",
    ]).decode()
    print(response.strip() or f"Build requested: {run_title}", flush=True)
    match = re.search(r"https://github\.com/[^/]+/[^/]+/actions/runs/(\d+)", response)
    run_id = match[1] if match else None
    if run_id is None:
        for _ in range(20):
            runs = json.loads(output([
                "gh", "run", "list", "--repo", repo, "--workflow", WORKFLOW,
                "--event", "workflow_dispatch", "--json", "databaseId,displayTitle",
            ]))
            run_id = next(
                (str(run["databaseId"]) for run in runs if run["displayTitle"] == run_title),
                None,
            )
            if run_id:
                break
            time.sleep(3)
    if not run_id:
        raise ValueError(f"Build requested. Search GitHub Actions for {request} to check its status.")
    run_url = f"https://github.com/{repo}/actions/runs/{run_id}"
    if not match:
        print(run_url, flush=True)
    subprocess.run(
        ["gh", "run", "watch", run_id, "--repo", repo, "--compact", "--exit-status"],
        check=True, env=public_env(),
    )
    with tempfile.TemporaryDirectory() as folder:
        subprocess.run([
            "gh", "run", "download", run_id, "--repo", repo,
            "--name", "build-record", "--dir", folder,
        ], check=True, env=public_env())
        record = load(Path(folder) / "build-record.json", {})
    if not valid_build(record, version, archive_hash, dockerfile_hash, scope):
        raise ValueError(f"Build record does not match the requested version, archive, Dockerfile, or registry. {run_url}")
    save(path, record)
    return record


def valid_build(record, version, archive_hash, dockerfile_hash, scope):
    image = record.get("image", "")
    return (
        record.get("version") == version
        and record.get("source") == source_url(version)
        and record.get("archive_sha256") == archive_hash
        and record.get("dockerfile_sha256") == dockerfile_hash
        and record.get("platform") == "linux/amd64"
        and re.fullmatch(IMAGE, image) is not None
        and image.startswith(f"docker.io/{scope}@")
    )


def checked_target(target):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", target["ssh"]):
        raise ValueError("Invalid SSH alias.")
    legacy = target.get("legacy_project", "snell")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", legacy) or legacy == "snell-managed":
        raise ValueError("Invalid existing Compose project name.")
    port = int(target["port"])
    if not 1 <= port <= 65535:
        raise ValueError("The public TCP port must be between 1 and 65535.")
    endpoint = target["tls"]
    if not re.fullmatch(r"[A-Za-z0-9.-]+(?::\d{1,5})?", endpoint):
        raise ValueError("Enter a handshake hostname or hostname:port.")
    if ":" in endpoint and not 1 <= int(endpoint.rsplit(":", 1)[1]) <= 65535:
        raise ValueError("The handshake port must be between 1 and 65535.")
    for key in ("psk", "password"):
        if not target[key].startswith("op://"):
            raise ValueError("Enter a 1Password secret reference beginning with op://.")
    return port


def select_target():
    path = STATE / "servers.json"
    targets = load(path, [])
    choices = {str(index): target["ssh"] for index, target in enumerate(targets, 1)}
    choice = choose(
        "Deploy / Select a server",
        {**choices, "0": "Add a server", "b": "Back"},
        default="1" if targets else "0",
    )
    if choice in (None, "b"):
        return None
    if choice == "0":
        target = {
            "ssh": input("  SSH alias: ").strip(),
            "port": int(input("  Existing public TCP port: ")),
            "tls": input("  ShadowTLS handshake target (hostname:port): ").strip(),
            "legacy_project": input("  Existing Compose project [snell]: ").strip() or "snell",
            "psk": input("  Snell PSK secret reference: ").strip(),
            "password": input("  ShadowTLS password secret reference: ").strip(),
        }
        checked_target(target)
        if any(existing["ssh"] == target["ssh"] for existing in targets):
            raise ValueError("That SSH alias is already saved. Edit the local servers.json to change it.")
        save(path, targets + [target])
        return target
    return targets[int(choice) - 1]


def secret(reference):
    value = subprocess.run(
        ["op", "read", "--no-newline", reference],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    if value.returncode:
        raise ValueError("1Password could not read the secret. Check CLI authorization and the secret reference.")
    if not value.stdout or b"\0" in value.stdout:
        raise ValueError("1Password returned an empty secret or one containing a NUL byte.")
    return value.stdout.decode()


def shadow_config(endpoint, password):
    return {
        "v3": True,
        "strict": False,
        "server": {
            "listen": "0.0.0.0:8443",
            "server_addr": "snell:28261",
            "tls_addr": {
                "wildcard_sni": "off",
                "dispatch": {},
                "fallback": endpoint if ":" in endpoint else f"{endpoint}:443",
            },
            "password": password,
            "wildcard_sni": "off",
        },
    }


def bundle(record, target, port):
    psk = secret(target["psk"])
    if not re.fullmatch(r"[!-~]+", psk) or "#" in psk or ";" in psk:
        raise ValueError("The Snell PSK contains whitespace, non-ASCII characters, or INI comment delimiters.")
    files = {
        "compose.yaml": (ROOT / "deploy" / "compose.yaml").read_bytes(),
        ".env": f"SNELL_IMAGE={record['image']}\nPUBLIC_PORT={port}\n".encode(),
        "snell.conf": f"[snell-server]\nlisten = 0.0.0.0:28261\npsk = {psk}\n".encode(),
        "shadow-tls.json": json.dumps(shadow_config(target["tls"], secret(target["password"]))).encode(),
        "build-record.json": json.dumps(record, indent=2).encode(),
    }
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, contents in files.items():
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(contents), 0o600
            tar.addfile(entry, io.BytesIO(contents))
    return archive.getvalue()


def remote(target, script, *args, stdin=None):
    command = shlex.join(["sh", "-ec", script, "snellctl", *args])
    return output([
        "ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "UpdateHostKeys=no", "-o", "ConnectTimeout=12",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
        "--", target["ssh"], command,
    ], stdin=stdin).decode().strip()


def deploy():
    version = select_release("Deploy")
    if version is None:
        return
    target = select_target()
    if target is None:
        return
    port = checked_target(target)
    legacy = target.get("legacy_project", "snell")
    script = (ROOT / "deploy" / "remote.sh").read_text()
    previous = remote(target, script, "preflight", legacy)
    print(f"  Server       {target['ssh']}\n  Old project  {legacy}\n{previous}")
    record = build(version, reuse=True)
    print(f"  New release  {version}\n  Public port  {port}/tcp\n  Image        {record['image']}")
    print("  Rollback uses the original Compose project. See the preflight output above.")
    if input("  Read the 1Password secrets and prepare the deployment? Type stage: ").strip() != "stage":
        print("  Deployment canceled.")
        return
    stage = remote(target, script, "stage", stdin=bundle(record, target, port))
    if not re.fullmatch(r"stage\.[A-Za-z0-9]+", stage):
        raise ValueError("The server did not return a valid staging directory. Check the remote state.")
    try:
        print(f"  Deployment prepared. Confirm the old {legacy} project is stopped on {target['ssh']}.")
        if input("  Verify the port and start the new services? Type start: ").strip() != "start":
            print("  Deployment canceled.")
            return
        print(remote(target, script, "start", stage, str(port), legacy))
    finally:
        remote(target, script, "discard", stage)
    print("  Containers started. Test the proxy connection from your client.")


def main():
    actions = {
        "1": "Check official releases",
        "2": "Build an image",
        "3": "Prepare and deploy",
        "0": "Quit",
    }
    while True:
        try:
            choice = choose("SNELL SERVER", actions, default="1")
            if choice in (None, "0"):
                return
            if choice == "1":
                show_releases()
            elif choice == "2":
                version = select_release("Build")
                if version is None:
                    continue
                if input(f"  Start a GitHub Actions build for {version}? [y/N]: ").strip().lower() not in ("y", "yes"):
                    print("  Build canceled.")
                    continue
                record = build(version)
                print(f"\n  Image published  {record['tag']}")
                print(f"  Image            {record['image']}")
                print(f"  Build record     {STATE / 'builds' / (version + '.json')}")
            elif choice == "3":
                deploy()
        except (EOFError, KeyboardInterrupt):
            return
        except subprocess.CalledProcessError as exc:
            print(f"  {Path(exc.cmd[0]).name} exited with status {exc.returncode}. See the output above.")
        except (OSError, ValueError, KeyError) as exc:
            print(f"  Error: {exc}")


if __name__ == "__main__":
    main()
