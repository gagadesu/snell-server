#!/usr/bin/env python3
"""Local menu for building and preparing the Snell deployment."""

import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = Path.home() / ".config" / "snell-server"
RELEASES = "https://kb.nssurge.com/surge-knowledge-base/release-notes/snell.md"
VERSION = r"v\d+\.\d+\.\d+"
IMAGE = r"docker\.io/[a-z0-9_-]+/[a-z0-9._-]+@sha256:[a-f0-9]{64}"
WORKFLOW = "build.yml"


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
        raise ValueError("请输入完整版本号，例如 v5.0.1")
    return f"https://dl.nssurge.com/snell/snell-server-{version}-linux-amd64.zip"


def version_key(version):
    return tuple(map(int, version[1:].split(".")))


def show_releases():
    notes = fetch(RELEASES).decode()
    headings = list(re.finditer(rf"(?m)^### ({VERSION})\s*$", notes))
    if not headings:
        raise ValueError("无法识别官方发布说明的版本标题")
    channels = {"正式版": [], "测试版": [], "未能确认渠道": []}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(notes)
        section = notes[heading.end():end]
        version = heading[1]
        if re.search(r"\b(?:stable release|generally available|out of beta)\b|正式发布", section, re.I):
            channels["正式版"].append(version)
        elif re.search(r"\bbeta\b|\bpre-release\b|release candidate", section, re.I):
            channels["测试版"].append(version)
        elif version_key(version) <= (5, 0, 1):
            channels["正式版"].append(version)
        else:
            channels["未能确认渠道"].append(version)
    for label, versions in channels.items():
        if label != "未能确认渠道" or versions:
            print(label, max(versions, key=version_key) if versions else "未识别")
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {RELEASES}")


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
    archive_hash = hashlib.sha256(fetch(source)).hexdigest()
    settings = load(STATE / "settings.json", {})
    repo = settings.get("github_repo", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("请先在本机 settings.json 设置 github_repo（OWNER/REPO）")
    scope = settings.get("dockerhub_repository", "")
    if not re.fullmatch(r"[a-z0-9_-]+/[a-z0-9._-]+", scope):
        raise ValueError("请设置 dockerhub_repository（namespace/repository）")
    path = STATE / "builds" / f"{version}.json"
    record = load(path, {})
    if reuse and valid_build(record, version, archive_hash, scope):
        return record

    request = uuid.uuid4().hex
    run_title = f"Snell {version} ({request})"
    response = output([
        "gh", "workflow", "run", WORKFLOW, "--repo", repo,
        "-f", f"version={version}", "-f", f"archive_sha256={archive_hash}",
        "-f", f"request_id={request}",
    ]).decode()
    print(response.strip() or f"已触发构建：{run_title}", flush=True)
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
        raise ValueError(f"构建已请求，暂未找到运行编号，请在 GitHub Actions 搜索 {request}")
    subprocess.run(
        ["gh", "run", "watch", run_id, "--repo", repo, "--exit-status"],
        check=True, env=public_env(),
    )
    with tempfile.TemporaryDirectory() as folder:
        subprocess.run([
            "gh", "run", "download", run_id, "--repo", repo,
            "--name", "build-record", "--dir", folder,
        ], check=True, env=public_env())
        record = load(Path(folder) / "build-record.json", {})
    if not valid_build(record, version, archive_hash, scope):
        raise ValueError("构建记录与请求的版本、下载文件或镜像仓库不匹配")
    save(path, record)
    return record


def valid_build(record, version, archive_hash, scope):
    image = record.get("image", "")
    return (
        record.get("version") == version
        and record.get("source") == source_url(version)
        and record.get("archive_sha256") == archive_hash
        and record.get("platform") == "linux/amd64"
        and re.fullmatch(IMAGE, image) is not None
        and image.startswith(f"docker.io/{scope}@")
    )


def checked_target(target):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", target["ssh"]):
        raise ValueError("SSH 别名格式无效")
    legacy = target.get("legacy_project", "snell")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", legacy) or legacy == "snell-managed":
        raise ValueError("旧 Compose 项目名无效")
    port = int(target["port"])
    if not 1 <= port <= 65535:
        raise ValueError("TCP 端口超出范围")
    endpoint = target["tls"]
    if not re.fullmatch(r"[A-Za-z0-9.-]+(?::\d{1,5})?", endpoint):
        raise ValueError("握手目标须为主机名或主机名:端口")
    if ":" in endpoint and not 1 <= int(endpoint.rsplit(":", 1)[1]) <= 65535:
        raise ValueError("握手端口超出范围")
    for key in ("psk", "password"):
        if not target[key].startswith("op://"):
            raise ValueError("凭据字段只接受 1Password 引用")
    return port


def select_target():
    path = STATE / "servers.json"
    targets = load(path, [])
    for index, target in enumerate(targets, 1):
        print(f"{index}. {target['ssh']}")
    choice = input("选择服务器编号，或输入 0 添加：").strip()
    if choice == "0":
        target = {
            "ssh": input("SSH 别名：").strip(),
            "port": int(input("原公网 TCP 端口：")),
            "tls": input("原 ShadowTLS 握手目标（主机名:端口）：").strip(),
            "legacy_project": input("旧 Compose 项目名 [snell]：").strip() or "snell",
            "psk": input("Snell PSK 的 op:// 引用：").strip(),
            "password": input("ShadowTLS 密码的 op:// 引用：").strip(),
        }
        checked_target(target)
        if any(existing["ssh"] == target["ssh"] for existing in targets):
            raise ValueError("该 SSH 别名已存在，请编辑本机 servers.json")
        save(path, targets + [target])
        return target
    if not choice.isdigit() or not 1 <= int(choice) <= len(targets):
        raise ValueError("服务器编号无效")
    return targets[int(choice) - 1]


def secret(reference):
    value = subprocess.run(
        ["op", "read", "--no-newline", reference],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    if value.returncode or not value.stdout or b"\0" in value.stdout:
        raise ValueError("1Password 读取失败，或凭据为空或包含 NUL")
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
        raise ValueError("Snell PSK 含空白、非 ASCII 字符或 INI 注释分隔符，需核实旧配置格式")
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
    version = input("要部署的版本：").strip()
    source_url(version)
    target = select_target()
    port = checked_target(target)
    legacy = target.get("legacy_project", "snell")
    script = (ROOT / "deploy" / "remote.sh").read_text()
    previous = remote(target, script, "preflight", legacy)
    print(f"目标 {target['ssh']} 的旧 Compose 项目 {legacy}\n{previous}")
    record = build(version, reuse=True)
    print(f"新 Snell {version}，公网 TCP {port}\n{record['image']}")
    print("回滚目标是原 Compose 项目。请核对预检给出的恢复方式。")
    if input("读取两个 1Password 凭据并暂存新配置？输入 stage：").strip() != "stage":
        return
    stage = remote(target, script, "stage", stdin=bundle(record, target, port))
    if not re.fullmatch(r"stage\.[A-Za-z0-9]+", stage):
        raise ValueError("远端未返回有效暂存目录，请检查远端状态")
    try:
        print(f"暂存完成。请确认 {target['ssh']} 的旧项目 {legacy} 已停止。")
        if input("确认释放原入口并启动新服务？输入 start：").strip() != "start":
            return
        print(remote(target, script, "start", stage, str(port), legacy))
    finally:
        remote(target, script, "discard", stage)
    print("容器已启动。请在 Surge 测试此节点，代理链路验收仍待完成。")


def main():
    while True:
        print("\n1. 查询官方版本\n2. 构建指定版本\n3. 准备并部署\n0. 退出")
        try:
            choice = input("选择：").strip()
            if choice == "0":
                return
            if choice == "1":
                show_releases()
            elif choice == "2":
                record = build(input("构建版本：").strip())
                print(json.dumps(record, ensure_ascii=False, indent=2))
            elif choice == "3":
                deploy()
        except (EOFError, KeyboardInterrupt):
            return
        except subprocess.CalledProcessError as exc:
            print(f"{Path(exc.cmd[0]).name} 失败（退出码 {exc.returncode}），请查看上方阶段信息。")
        except (OSError, ValueError, KeyError) as exc:
            print(f"操作失败：{exc}")


if __name__ == "__main__":
    main()
