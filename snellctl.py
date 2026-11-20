#!/usr/bin/env python3
"""Local menu for building and preparing the Snell deployment."""

import fcntl
import getpass
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
import warnings
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = Path.home() / ".config" / "snell-server"
RELEASES = "https://kb.nssurge.com/surge-knowledge-base/release-notes/snell.md"
VERSION = r"v\d+\.\d+\.\d+"
IMAGE = r"docker\.io/[a-z0-9_-]+/[a-z0-9._-]+@sha256:[a-f0-9]{64}"
SSH_ALIAS = r"[A-Za-z0-9][A-Za-z0-9._-]*"
WORKFLOW = "build.yml"
PULL_AUTH_REQUIRED = 20  # Exit status reserved by deploy/remote.sh.


def menu_label(icon, label):
    if sys.stdout.isatty():
        return f"{icon}  {label}"
    return label


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
    handle = tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False)
    try:
        with handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(handle.name, path)
    finally:
        Path(handle.name).unlink(missing_ok=True)


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


def validate_target_secret(key, value):
    if key == "psk":
        if not re.fullmatch(r"[!-~]+", value) or "#" in value or ";" in value:
            raise ValueError("Enter a nonempty Snell PSK using printable ASCII without spaces, # or ;.")
    elif not value or "\0" in value:
        raise ValueError("Enter a nonempty ShadowTLS password without NUL characters.")


def checked_target(target):
    if not re.fullmatch(SSH_ALIAS, target["ssh"]):
        raise ValueError("Invalid SSH alias.")
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
            validate_target_secret(key, target[key])
    return port


def unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
        return value[1:-1].strip()
    return value


def prompt_target_secret(key, prompt, default):
    while True:
        if sys.stdin.isatty():
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                try:
                    value = getpass.getpass(prompt)
                except getpass.GetPassWarning as exc:
                    raise ValueError("Use a terminal that supports hidden password entry.") from exc
        else:
            value = input(prompt)
        value = value or default
        reference = unquote(value)
        if reference.startswith("op://"):
            return reference
        try:
            validate_target_secret(key, value)
        except ValueError as exc:
            print(f"  {exc}")
        else:
            return value


def add_target(targets):
    alias = input("  SSH alias: ").strip()
    if not re.fullmatch(SSH_ALIAS, alias):
        raise ValueError("Invalid SSH alias.")
    if any(target["ssh"].casefold() == alias.casefold() for target in targets):
        raise ValueError("That SSH alias is already saved. Select it from the server list.")

    fields = {
        "port": "Public TCP port",
        "tls": "ShadowTLS handshake target (hostname:port)",
        "psk": "Snell PSK",
        "password": "ShadowTLS password",
    }
    defaults = {"ssh": alias, "port": 8443}
    if targets:
        previous = targets[-1]
        for key in ("port", "tls"):
            if previous.get(key):
                defaults[key] = previous[key]
        for key, field in (("psk", "snell-psk"), ("password", "shadowtls-password")):
            scheme, _, uri = previous.get(key, "").partition("://")
            parts = uri.split("/")
            if (
                scheme == "op"
                and len(parts) == 3
                and parts[0]
                and parts[1:] == [f"snell-{previous['ssh']}", field]
            ):
                defaults[key] = f"op://{parts[0]}/snell-{alias}/{field}"

    customize = True
    if all(key in defaults for key in fields):
        print(f"\n  Defaults for {alias}")
        for key, label in fields.items():
            print(f"  {label:<41} {defaults[key]}")
        choice = choose(
            "Deploy / Server settings",
            {"1": menu_label("\uf00c", "Use defaults"), "2": menu_label("\uf013", "Customize"), "b": "Back"},
            default="1",
        )
        if choice in (None, "b"):
            return None
        customize = choice == "2"

    if customize:
        print("  Enter a password or an op:// reference for each credential.")
        for key, label in fields.items():
            default = defaults.get(key, "")
            hint = f" [{default}]" if default else ""
            prompt = f"  {label}{hint}: "
            if key in ("psk", "password"):
                defaults[key] = prompt_target_secret(key, prompt, default)
            else:
                defaults[key] = input(prompt).strip() or default
    defaults["port"] = checked_target(defaults)
    return defaults


def update_saved_targets(path, *, old=None, new=None):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Lock a separate file so replacing servers.json leaves the lock in place.
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        targets = load(path, [])
        if old is not None:
            if old not in targets:
                return False
            index = targets.index(old)
            targets[index:index + 1] = [] if new is None else [new]
        else:
            if any(saved["ssh"].casefold() == new["ssh"].casefold() for saved in targets):
                raise ValueError("That SSH alias is already saved. Select it from the server list.")
            targets.append(new)
        save(path, targets)
    return True


def remove_target(path, targets, choices):
    choice = choose("Deploy / Remove a saved server", {**choices, "b": "Back"}, default="b")
    if choice in (None, "b"):
        return
    target = targets[int(choice) - 1]
    alias = target["ssh"]
    choice = choose(
        f"Deploy / Remove {alias} from the local server list?",
        {"r": "Remove", "b": "Cancel"},
        default="b",
    )
    if choice == "r":
        if update_saved_targets(path, old=target):
            print(f"  Removed {alias} from the local server list.")
        else:
            print(f"  The saved entry for {alias} changed. Select it again to remove it.")


def select_target():
    path = STATE / "servers.json"
    while True:
        targets = load(path, [])
        choices = {str(index): target["ssh"] for index, target in enumerate(targets, 1)}
        options = {**choices, "0": menu_label("\uf067", "Add a server")}
        if targets:
            options["r"] = "Remove a saved server"
        options["b"] = "Back"
        choice = choose("Deploy / Select a server", options, default="1" if targets else "0")
        if choice in (None, "b"):
            return None
        if choice == "r":
            remove_target(path, targets, choices)
            continue
        if choice == "0":
            target = add_target(targets)
            if target is not None:
                update_saved_targets(path, new=target)
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


def resolve_target_secret(target, key):
    value = target[key]
    if value.startswith("op://"):
        value = secret(value)
    validate_target_secret(key, value)
    return value


def bundle(record, target, port):
    psk = resolve_target_secret(target, "psk")
    password = resolve_target_secret(target, "password")
    files = {
        "compose.yaml": (ROOT / "deploy" / "compose.yaml").read_bytes(),
        ".env": f"SNELL_IMAGE={record['image']}\nPUBLIC_PORT={port}\n".encode(),
        "snell.conf": f"[snell-server]\nlisten = 0.0.0.0:28261\npsk = {psk}\n".encode(),
        "shadow-tls.json": json.dumps(shadow_config(target["tls"], password)).encode(),
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


def login_to_docker_hub(target, script, default_username):
    print(f"  Docker Hub denied access to the Snell image on {target['ssh']}.")
    print("  Sign in on the server with a Docker Hub token that can pull from this repository.")
    print("  Docker will save the login for the SSH user. Leave the token blank to cancel.")
    while True:
        username = input(f"  Docker Hub username [{default_username}]: ").strip() or default_username
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", username):
            print("  Enter a valid Docker Hub username.")
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                credential = getpass.getpass("  Docker Hub token or op:// reference: ")
            except getpass.GetPassWarning as exc:
                raise ValueError("Use a terminal that supports hidden token entry.") from exc
        if not credential.strip():
            return False
        token = unquote(credential)
        if token.startswith("op://"):
            try:
                token = secret(token).strip()
            except ValueError as exc:
                print(f"  {exc}")
                continue
        if not re.fullmatch(r"[!-~]+", token):
            print("  Enter a token without spaces or line breaks.")
            continue
        try:
            remote(target, script, "login", username, stdin=(token + "\n").encode())
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 1:
                raise
            print("  Login failed. Try again or leave the token blank to cancel.")
            continue
        print("  Docker Hub login succeeded. Retrying image pull.")
        return True


def replacement_target(target):
    choice = choose(
        "Deploy / Replace the managed deployment",
        {"1": "Use saved credentials", "2": "Change PSK or password", "b": "Back"},
        default="1",
    )
    if choice in (None, "b"):
        return None
    if choice == "1":
        return target
    print("  Enter a password or an op:// reference. Press Enter to keep the saved value.")
    return {
        **target,
        "psk": prompt_target_secret("psk", "  Snell PSK: ", target["psk"]),
        "password": prompt_target_secret("password", "  ShadowTLS password: ", target["password"]),
    }


def deploy():
    version = select_release("Deploy")
    if version is None:
        return
    target = select_target()
    if target is None:
        return
    port = checked_target(target)
    script = (ROOT / "deploy" / "remote.sh").read_text()
    state = remote(target, script, "preflight")
    replacing = state != "new"
    if replacing and not re.fullmatch(r"\d+:\d+:\d+", state):
        raise ValueError("The server did not return a valid deployment state.")
    saved_target = target
    if replacing:
        target = replacement_target(target)
        if target is None:
            return
    print(f"  Server       {target['ssh']}")
    record = build(version, reuse=True)
    print(f"  New release  {version}\n  Public port  {port}/tcp\n  Image        {record['image']}")
    if input("  Prepare the deployment with the configured credentials? Type stage: ").strip() != "stage":
        print("  Deployment canceled.")
        return
    archive = bundle(record, target, port)
    while True:
        try:
            stage = remote(target, script, "stage", stdin=archive)
            break
        except subprocess.CalledProcessError as exc:
            if exc.returncode != PULL_AUTH_REQUIRED:
                raise
            if not login_to_docker_hub(target, script, record["image"].split("/", 2)[1]):
                print("  Deployment canceled.")
                return
    if not re.fullmatch(r"stage\.[A-Za-z0-9]+", stage):
        raise ValueError("The server did not return a valid staging directory. Check the remote state.")
    try:
        print(f"  Deployment prepared on {target['ssh']}.")
        confirmation = "replace" if replacing else "start"
        prompt = (
            "  Replace the managed services? Existing connections will be interrupted. Type replace: "
            if replacing else "  Verify the port and start the new services? Type start: "
        )
        if input(prompt).strip() != confirmation:
            print("  Deployment canceled.")
            return
        print(remote(target, script, "start", stage, state))
        if target != saved_target:
            try:
                saved = update_saved_targets(STATE / "servers.json", old=saved_target, new=target)
            except (OSError, ValueError) as exc:
                print(f"  The new credentials were deployed. Could not update the local server list: {exc}")
            else:
                if not saved:
                    print("  The local server entry changed. The new credentials were deployed but not saved locally.")
    finally:
        try:
            remote(target, script, "discard", stage)
        except (OSError, subprocess.CalledProcessError):
            print(f"  Could not discard {stage} on {target['ssh']}. Check the remote deployment directory.")
    outcome = "Managed deployment replaced." if replacing else "Containers started."
    print(f"  {outcome} Test the proxy connection from your client.")


def main():
    actions = {
        "1": menu_label("\uf02d", "Check official releases"),
        "2": menu_label("\uf1b2", "Build an image"),
        "3": menu_label("\uf233", "Prepare and deploy"),
        "0": menu_label("\uf011", "Quit"),
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
