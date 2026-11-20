#!/bin/sh
set -eu
umask 077
unset SNELL_IMAGE PUBLIC_PORT COMPOSE_PROFILES

root="$HOME/.local/share/snell-server"
active="$root/active"
project=snell-managed

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

require_inactive() {
    containers="$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
    [ -z "$containers" ] && [ ! -e "$active" ] ||
        fail 'The managed project already exists. Check its deployment and rollback state first.'
}

compose() {
    directory="$1"
    shift
    docker compose --project-name "$project" --env-file "$directory/.env" -f "$directory/compose.yaml" "$@"
}

operation="$1"
case "$operation" in
    preflight)
        [ "$(uname -m)" = x86_64 ] || fail 'The target must be amd64.'
        [ "$(uname -s)" = Linux ] || fail 'The target must be Linux.'
        command -v flock >/dev/null
        command -v ss >/dev/null
        docker compose version >/dev/null
        require_inactive
        ;;
    login)
        docker login --username "$2" --password-stdin || fail 'Docker Hub login failed.'
        ;;
    stage)
        require_inactive
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
        if [ "$operation" = discard ]; then
            rm -rf -- "$stage"
            exit
        fi
        exec 9>"$root/deploy.lock"
        flock -n 9 || fail 'Another deployment is starting.'
        require_inactive
        port="$3"
        listeners="$(ss -H -lnt "sport = :$port")" || fail 'Could not check the public port.'
        [ -z "$listeners" ] || fail 'The public port is in use. Free it before starting the new services.'
        mv -- "$stage" "$active"
        compose "$active" up --detach --pull never --wait --wait-timeout 30 >&2 ||
            fail 'Startup failed. Deployment files remain in active. See the README to stop the deployment.'
        compose "$active" ps
        ;;
    *) fail 'Unknown remote operation.' ;;
esac
