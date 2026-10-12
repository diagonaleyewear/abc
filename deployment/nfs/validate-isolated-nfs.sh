#!/bin/sh
# Dedicated mount/network namespace; never mount, stop or alter the host's NFS.
set -eu
IMAGE="${1:?Supply isolated probe image}"
NAME="cloud-nfs-isolated-$(date +%s)-$$"
SOURCE="${2:-192.168.1.32:/volume1/cloud-imagenes}"
GATEWAY=
restore_network() {
    docker exec "$NAME" ip link set eth0 up
    if test -n "$GATEWAY"; then
        docker exec "$NAME" ip route replace default via "$GATEWAY" dev eth0
    fi
}
cleanup() {
    restore_network >/dev/null 2>&1 || true
    if docker exec "$NAME" test -f /tmp/nfs-probe-state/directory; then
        if docker exec "$NAME" test -f /tmp/nfs-probe-state/writer-started; then
            i=0
            while ! docker exec "$NAME" test -f /tmp/nfs-probe-state/writer-finished; do
                i=$((i + 1))
                if test "$i" -ge 30; then
                    echo "Writer still active; do not delete its files. Retained container: $NAME"
                    return
                fi
                sleep 2
            done
        fi
        docker exec "$NAME" python /probe-code/nfs_isolated_probe.py cleanup || {
            echo "Probe cleanup failed; container retained for recovery: $NAME"
            return
        }
    fi
    docker exec "$NAME" umount /isolated-nfs >/dev/null 2>&1 || true
    docker rm -f "$NAME" >/dev/null
}
trap cleanup EXIT
docker run -d --name "$NAME" --memory=512m --cpus=1 --cap-add=SYS_ADMIN --cap-add=NET_ADMIN \
    --security-opt apparmor=unconfined --security-opt seccomp=unconfined \
    -e NFS_PROBE_SOURCE="$SOURCE" "$IMAGE" >/dev/null
# No host network, host PID namespace, host mounts or docker socket.
docker exec "$NAME" mkdir -m 000 /isolated-nfs
if docker exec "$NAME" python /probe-code/nfs_repository_guard.py --root /isolated-nfs --source "$SOURCE"; then
    echo "Missing-mount guard failed"; exit 1
fi
docker exec "$NAME" mount -t nfs -o vers=4.1,hard,nosharecache,timeo=10,retrans=2,sec=sys \
    "$SOURCE" /isolated-nfs
docker exec "$NAME" python /probe-code/nfs_repository_guard.py --root /isolated-nfs --source "$SOURCE"
docker exec "$NAME" python /probe-code/nfs_isolated_probe.py setup
GATEWAY="$(docker exec "$NAME" sh -c "ip route show default | cut -d ' ' -f 3")"
test -n "$GATEWAY"
docker exec "$NAME" ip link set eth0 down
docker exec "$NAME" chown 1037:100 /tmp/nfs-probe-state
docker exec "$NAME" chmod 0700 /tmp/nfs-probe-state
docker exec -d --user 1037:100 "$NAME" sh -c 'umask 077; python /probe-code/nfs_isolated_probe.py write > /tmp/nfs-write.log 2>&1'
sleep 5
docker exec "$NAME" test -f /tmp/nfs-probe-state/writer-started
if docker exec "$NAME" test -f /tmp/nfs-probe-state/writer-finished; then
    echo "Write unexpectedly finished with isolated network down"; exit 1
fi
echo "Real hard-NFS write pending during isolated network outage"
restore_network
i=0
while ! docker exec "$NAME" test -f /tmp/nfs-probe-state/writer-finished; do
    i=$((i + 1))
    test "$i" -lt 30 || { echo "Reconnection exceeded 60s; inspect retained probe"; exit 1; }
    sleep 2
done
docker exec "$NAME" cat /tmp/nfs-write.log
docker exec "$NAME" python /probe-code/nfs_isolated_probe.py locks
docker exec "$NAME" python /probe-code/nfs_isolated_probe.py cleanup
docker exec "$NAME" rm /tmp/nfs-probe-state/directory
docker exec "$NAME" umount /isolated-nfs
if docker exec "$NAME" python /probe-code/nfs_repository_guard.py --root /isolated-nfs --source "$SOURCE"; then
    echo "Post-unmount guard failed"; exit 1
fi
docker restart "$NAME" >/dev/null
if docker exec "$NAME" python /probe-code/nfs_repository_guard.py --root /isolated-nfs --source "$SOURCE"; then
    echo "Restart accepted missing mount"; exit 1
fi
echo "Missing mount rejected inside container before mount, after unmount and after restart"
