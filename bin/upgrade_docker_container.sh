#!/bin/bash
# Helper script to upgrade a Docker container to the latest image

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <container_name>"
    exit 1
fi

CONTAINER="$1"

# Check if container exists
if ! docker ps -a --format "{{.Names}}" | grep -q "^${CONTAINER}$"; then
    echo "Error: Container $CONTAINER does not exist"
    exit 1
fi

# Get current image
IMAGE=$(docker inspect --format='{{.Config.Image}}' "$CONTAINER" 2>/dev/null)
echo "Current image: $IMAGE"

# Pull latest image
echo "Pulling latest image..."
docker pull "$IMAGE"

# Check if image was updated
OLD_ID=$(docker inspect --format='{{.Image}}' "$CONTAINER" 2>/dev/null)
NEW_ID=$(docker images --format='{{.ID}}' "$IMAGE" | head -1)

if [[ "$OLD_ID" == "sha256:$NEW_ID" ]]; then
    echo "Container already on latest version"
    exit 0
fi

echo "New image available, recreating container..."

# Get container configuration
BINDS=$(docker inspect --format='{{range .HostConfig.Binds}}{{.}} {{end}}' "$CONTAINER")
RESTART_POLICY=$(docker inspect --format='{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER")
NETWORK=$(docker inspect --format='{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$CONTAINER")
EXTRA_HOSTS=$(docker inspect --format='{{range .HostConfig.ExtraHosts}}{{.}} {{end}}' "$CONTAINER")

# Get environment variables
ENV_VARS=$(docker inspect --format='{{range .Config.Env}}{{.}}
{{end}}' "$CONTAINER" | grep -v "^PATH=" | grep -v "^HOME=" | grep -v "^HOSTNAME=")

# Get port bindings in docker run format
PORTS=$(docker inspect --format='{{range $p, $conf := .NetworkSettings.Ports}}{{if $conf}}-p {{(index $conf 0).HostPort}}:{{$p}} {{end}}{{end}}' "$CONTAINER")

# Build volume arguments
VOLUME_ARGS=""
for bind in $BINDS; do
    VOLUME_ARGS="$VOLUME_ARGS -v $bind"
done

# Build extra hosts arguments
HOST_ARGS=""
for host in $EXTRA_HOSTS; do
    HOST_ARGS="$HOST_ARGS --add-host=$host"
done

# Always add host.docker.internal for containers that might need it
if [[ ! "$HOST_ARGS" =~ "host.docker.internal" ]]; then
    HOST_ARGS="$HOST_ARGS --add-host=host.docker.internal:host-gateway"
fi

# Build environment arguments
ENV_ARGS=""
while IFS= read -r env; do
    if [[ -n "$env" ]]; then
        ENV_ARGS="$ENV_ARGS -e \"$env\""
    fi
done <<< "$ENV_VARS"

# Stop and remove old container
echo "Stopping container..."
docker stop "$CONTAINER"
echo "Removing old container..."
docker rm "$CONTAINER"

# Recreate container
echo "Creating new container..."
eval docker run -d \
    --name "$CONTAINER" \
    --restart "$RESTART_POLICY" \
    --network "$NETWORK" \
    $VOLUME_ARGS \
    $PORTS \
    $HOST_ARGS \
    $ENV_ARGS \
    "$IMAGE"

echo "Container $CONTAINER successfully upgraded and started"
