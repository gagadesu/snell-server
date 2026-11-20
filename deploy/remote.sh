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
        fail 'The managed project already exists. Check its deployment and rollback state first.'
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
        printf 'The old containers still exist. Use compose start with the original project to roll back.\n'
        return
    fi

    [ -r "$legacy_file" ] || fail 'The old containers were removed and the old Compose file is missing.'
    images="$(docker compose -p "$legacy" -f "$legacy_file" config --images 2>/dev/null)" ||
        fail 'The old containers were removed and the old Compose image list could not be read.'
    [ -n "$images" ] || fail 'The old Compose file contains no images to restore.'
    printf '%s\n' "$images" | while IFS= read -r image; do
        docker image inspect --format '{{.Id}}' "$image" >/dev/null 2>&1 ||
            fail "An image needed for rollback is missing locally: $image"
    done
    printf 'The old containers were removed. Rollback can recreate them from local images with compose up --detach --pull never --no-build.\n'
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
        legacy_restore "$2"
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
        compose "$stage" pull >&2 || fail 'Image pull failed. Check the private registry credentials.'
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
        running="$(docker ps -q --filter "label=com.docker.compose.project=$4")"
        [ -z "$running" ] || fail 'The old project still has running containers. Stop them before starting the new project.'
        legacy_restore "$4" >&2
        listeners="$(ss -H -lnt "sport = :$port")" || fail 'Could not check the public port.'
        [ -z "$listeners" ] || fail 'The public port is in use. Check that the old ShadowTLS is stopped.'
        mv -- "$stage" "$active"
        compose "$active" up --detach --pull never --wait --wait-timeout 30 >&2 ||
            fail 'Startup failed. Deployment files remain in active. See the README for rollback steps.'
        compose "$active" ps
        ;;
    *) fail 'Unknown remote operation.' ;;
esac
