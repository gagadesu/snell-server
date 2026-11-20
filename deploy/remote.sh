#!/bin/sh
set -eu
umask 077
unset SNELL_IMAGE PUBLIC_PORT COMPOSE_PROFILES

root="$HOME/.local/share/snell-server"
active="$root/active"
project=snell-managed
legacy_file="$HOME/.config/containers/snell/docker-compose.yml"

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

require_inactive() {
    containers="$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
    [ -z "$containers" ] && [ ! -e "$active" ] ||
        fail '新项目已存在，请先检查安装状态及回滚记录。'
}

compose() {
    directory="$1"
    shift
    docker compose --project-name "$project" --env-file "$directory/.env" -f "$directory/compose.yaml" "$@"
}

legacy_restore() {
    legacy="$1"
    previous="$(docker ps -a --filter "label=com.docker.compose.project=$legacy" \
        --format '{{.Names}}  {{.Image}}  {{.Status}}')"
    if [ -n "$previous" ]; then
        printf '%s\n' "$previous"
        printf '旧容器仍在。回滚可使用旧 Compose 文件执行 start。\n'
        return
    fi

    [ -r "$legacy_file" ] || fail '旧容器已移除，且找不到旧 Compose 文件。'
    images="$(docker compose -p "$legacy" -f "$legacy_file" config --images 2>/dev/null)" ||
        fail '旧容器已移除，且无法解析旧 Compose 的镜像列表。'
    [ -n "$images" ] || fail '旧 Compose 没有可用于重建的镜像。'
    printf '%s\n' "$images" | while IFS= read -r image; do
        docker image inspect --format '{{.Id}}' "$image" >/dev/null 2>&1 ||
            fail "旧 Compose 所需镜像不在本机：$image"
    done
    printf '旧容器已移除。旧 Compose 和本机镜像仍在，可用 up --detach --pull never --no-build 重建。\n'
}

operation="$1"
case "$operation" in
    preflight)
        [ "$(uname -m)" = x86_64 ] || fail '目标需要 Linux amd64。'
        [ "$(uname -s)" = Linux ] || fail '目标需要 Linux。'
        command -v flock >/dev/null
        command -v ss >/dev/null
        docker compose version >/dev/null
        require_inactive
        legacy_restore "$2"
        ;;
    stage)
        require_inactive
        mkdir -p "$root"
        stage="$(mktemp -d "$root/stage.XXXXXXXX")"
        trap 'rm -rf -- "$stage"' EXIT
        tar -xf - -C "$stage" || fail '配置上传失败。'
        compose "$stage" config --quiet || fail 'Compose 配置验证失败。'
        compose "$stage" pull >&2 || fail '拉取镜像失败，请核对私有仓库的读取权限。'
        trap - EXIT
        basename "$stage"
        ;;
    start|discard)
        name="$2"
        case "$name" in
            stage.*) ;;
            *) fail '暂存目录无效。' ;;
        esac
        case "$name" in *[!A-Za-z0-9.]*|stage.|stage.*.*) fail '暂存目录无效。' ;; esac
        stage="$root/$name"
        if [ "$operation" = discard ]; then
            rm -rf -- "$stage"
            exit
        fi
        exec 9>"$root/deploy.lock"
        flock -n 9 || fail '另一个部署正在启动。'
        require_inactive
        port="$3"
        running="$(docker ps -q --filter "label=com.docker.compose.project=$4")"
        [ -z "$running" ] || fail '旧项目仍有运行中的容器，请手动停止后再切换。'
        legacy_restore "$4" >&2
        listeners="$(ss -H -lnt "sport = :$port")" || fail '无法检查入口监听。'
        [ -z "$listeners" ] || fail '公网入口仍被占用，请确认旧 ShadowTLS 已停止。'
        mv -- "$stage" "$active"
        compose "$active" up --detach --pull never --wait --wait-timeout 30 >&2 ||
            fail '新实例启动失败。部署文件保留在 active，请按 README 检查并恢复旧服务。'
        compose "$active" ps
        ;;
    *) fail '未知远端操作。' ;;
esac
