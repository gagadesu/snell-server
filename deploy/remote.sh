#!/bin/sh
set -eu
umask 077
unset SNELL_IMAGE PUBLIC_PORT SNELL_NETWORK_MODE SNELL_BACKEND_PORT COMPOSE_PROFILES

root="$HOME/.local/share/snell-server"
active="$root/active"
rollback="$root/rollback"
project=snell-managed

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

deployment_state() {
    [ ! -e "$rollback" ] && [ ! -L "$rollback" ] ||
        fail 'An interrupted replacement left a rollback directory. See the README before deploying again.'
    containers="$(docker ps -a --filter "label=com.docker.compose.project=$project" \
        --format '{{.ID}} {{.Label "com.docker.compose.project.config_files"}}')" ||
        fail 'Could not inspect the managed project.'
    if [ ! -e "$active" ] && [ ! -L "$active" ]; then
        [ -z "$containers" ] || fail 'The Compose project name is in use without a managed deployment.'
        printf '%s\n' new
        return
    fi
    [ -d "$active" ] && [ ! -L "$active" ] ||
        fail 'Invalid managed deployment directory.'
    for file in .env compose.yaml snell.conf shadow-tls.json build-record.json; do
        [ -f "$active/$file" ] && [ ! -L "$active/$file" ] ||
            fail "The managed deployment is missing a regular file: $file"
    done
    while IFS= read -r container; do
        [ -z "$container" ] || [ "${container#* }" = "$active/compose.yaml" ] ||
            fail 'The Compose project name is in use by containers outside the managed deployment.'
    done <<EOF
$containers
EOF
    stat -c '%d:%i:%Y' -- "$active"
}

compose() {
    compose_directory="$1"
    shift
    docker compose --project-name "$project" --env-file "$compose_directory/.env" -f "$compose_directory/compose.yaml" "$@"
}

deployment_port() {
    value="$(sed -n "s/^$2=//p" "$1/.env")" || fail "Could not read $2."
    case "$value" in ''|*[!0-9]*) fail "Invalid $2 in deployment files." ;; esac
    [ "$value" -ge 1 ] && [ "$value" -le 65535 ] || fail "Invalid $2 in deployment files."
    printf '%s\n' "$value"
}

deployment_network() {
    mode="$(sed -n 's/^SNELL_NETWORK_MODE=//p' "$1/.env")" || fail 'Could not read the container network mode.'
    case "${mode:-bridge}" in
        bridge|host) printf '%s\n' "${mode:-bridge}" ;;
        *) fail 'Invalid container network mode in deployment files.' ;;
    esac
}

check_port_available() {
    [ "$1" != "$2" ] && [ "$1" != "$3" ] || return 0
    listeners="$(ss -H -lnt "sport = :$1")" || fail "Could not check TCP port $1."
    [ -z "$listeners" ] || fail "TCP port $1 is in use. Free it before starting the new services."
}

host_listeners_ready() {
    public="$(deployment_port "$1" PUBLIC_PORT)" || return 1
    backend="$(deployment_port "$1" SNELL_BACKEND_PORT)" || return 1
    for attempt in 1 2 3 4 5 6; do
        listeners="$(ss -H -lnt)" || return 1
        if printf '%s\n' "$listeners" | awk -v public="0.0.0.0:$public" -v backend="127.0.0.1:$backend" '
            $4 == public { front = 1 }
            $4 == backend { back = 1 }
            END { exit !(front && back) }
        '; then
            return 0
        fi
        [ "$attempt" -eq 6 ] || sleep 1
    done
    printf '%s\n' 'Host network listeners did not become ready on the public and loopback addresses.' >&2
    return 1
}

start_services() {
    # Credential changes are bind-mounted file changes and may leave the Compose model identical.
    compose "$active" up --detach --force-recreate --pull never --wait --wait-timeout 30 >&2 || return 1
    active_mode="$(deployment_network "$active")" || return 1
    [ "$active_mode" != host ] || host_listeners_ready "$active"
}

operation="$1"
case "$operation" in
    preflight)
        [ "$(uname -m)" = x86_64 ] || fail 'The target must be amd64.'
        [ "$(uname -s)" = Linux ] || fail 'The target must be Linux.'
        command -v flock >/dev/null
        command -v ss >/dev/null
        docker compose version >/dev/null
        deployment_state
        ;;
    login)
        docker login --username "$2" --password-stdin || fail 'Docker Hub login failed.'
        ;;
    stage)
        mkdir -p "$root"
        stage="$(mktemp -d "$root/stage.XXXXXXXX")"
        trap 'rm -rf -- "$stage"' EXIT
        tar -xf - -C "$stage" || fail 'Could not upload the deployment files.'
        # Containers without CAP_DAC_OVERRIDE must be able to read the bind mounts.
        # The 0700 staging directory continues to protect the files on the host.
        for secret in snell.conf shadow-tls.json; do
            [ -f "$stage/$secret" ] && [ ! -L "$stage/$secret" ] ||
                fail "Missing or invalid deployment credential file: $secret"
            chmod 0444 "$stage/$secret" || fail "Could not set permissions on $secret."
        done
        compose "$stage" config --quiet || fail 'Compose configuration validation failed.'
        if pull_output="$(compose "$stage" pull snell 2>&1)"; then
            printf '%s\n' "$pull_output" >&2
        else
            if printf '%s\n' "$pull_output" | grep -Eiq \
                'pull access denied|requested access to the resource is denied|unauthorized(:|$)|authentication required|insufficient_scope'; then
                exit 20
            fi
            printf '%s\n' "$pull_output" >&2
            fail 'Could not pull the Snell image.'
        fi
        compose "$stage" pull shadow-tls >&2 || fail 'Could not pull the public ShadowTLS image.'
        trap - EXIT
        basename "$stage"
        ;;
    start|discard)
        name="$2"
        case "$name" in
            stage.*) ;;
            *) fail 'Invalid staging directory.' ;;
        esac
        case "$name" in *[!A-Za-z0-9.]*|stage.|stage.*.*) fail 'Invalid staging directory.' ;; esac
        stage="$root/$name"
        [ -d "$root" ] || fail 'The deployment directory does not exist.'
        exec 9>"$root/deploy.lock"
        if [ "$operation" = discard ]; then
            flock 9
            rm -rf -- "$stage"
            exit
        fi
        flock -n 9 || fail 'Another deployment is starting.'
        state="$(deployment_state)" || exit 1
        [ "$state" = "$3" ] || fail 'The managed deployment changed while you were preparing. Start a new deployment.'
        [ -d "$stage" ] && [ ! -L "$stage" ] || fail 'The staged deployment does not exist.'
        port="$(deployment_port "$stage" PUBLIC_PORT)" || exit 1
        mode="$(deployment_network "$stage")" || exit 1
        previous_port=''
        previous_backend=''
        if [ "$state" != new ]; then
            previous_port="$(deployment_port "$active" PUBLIC_PORT)" || exit 1
            previous_mode="$(deployment_network "$active")" || exit 1
            if [ "$previous_mode" = host ]; then
                previous_backend="$(deployment_port "$active" SNELL_BACKEND_PORT)" || exit 1
            fi
            compose "$active" config --quiet || fail 'The current Compose configuration cannot be restored.'
        fi
        check_port_available "$port" "$previous_port" "$previous_backend"
        if [ "$mode" = host ]; then
            backend="$(deployment_port "$stage" SNELL_BACKEND_PORT)" || exit 1
            [ "$backend" != "$port" ] || fail 'The public and loopback TCP ports must differ.'
            check_port_available "$backend" "$previous_port" "$previous_backend"
        fi
        if [ "$state" != new ]; then
            mv -T -- "$active" "$rollback"
        fi
        if ! mv -T -- "$stage" "$active"; then
            if [ "$state" != new ]; then
                mv -T -- "$rollback" "$active" || fail 'Could not restore the deployment files. See the README.'
            fi
            fail 'Could not activate the staged deployment files.'
        fi
        if ! start_services; then
            [ "$state" != new ] ||
                fail 'Startup failed. Deployment files remain in active for inspection or retry.'
            printf '%s\n' 'Replacement failed. Restoring the previous deployment.' >&2
            mv -T -- "$active" "$stage" && mv -T -- "$rollback" "$active" ||
                fail 'Could not restore the deployment files. See the README.'
            start_services ||
                fail 'Could not restart the previous deployment. Its files are in active. See the README.'
            fail 'Replacement failed. The previous deployment was restored.'
        fi
        if [ "$state" != new ]; then
            rm -rf -- "$rollback" || printf '%s\n' 'Replacement started, but old files remain in rollback. See the README.' >&2
        fi
        compose "$active" ps || printf '%s\n' 'Services started, but Compose status could not be displayed.' >&2
        ;;
    *) fail 'Unknown remote operation.' ;;
esac
