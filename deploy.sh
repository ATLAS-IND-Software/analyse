#!/usr/bin/env sh
set -eu
umask 077

CONTAINER_NAME="${CONTAINER_NAME:-histo-maker}"
IMAGE_NAME="${IMAGE_NAME:-histo-maker:latest}"
PORT="${PORT:-8000}"
MAX_UPLOAD_MB="${MAX_UPLOAD_MB:-50}"
INSPECT_RATE_LIMIT="${INSPECT_RATE_LIMIT:-30}"
ANALYZE_RATE_LIMIT="${ANALYZE_RATE_LIMIT:-10}"
ESTIMATE_RATE_LIMIT="${ESTIMATE_RATE_LIMIT:-30}"
MAX_CONCURRENT_INSPECTIONS_PER_WORKER="${MAX_CONCURRENT_INSPECTIONS_PER_WORKER:-1}"
MAX_CONCURRENT_ANALYSES_PER_WORKER="${MAX_CONCURRENT_ANALYSES_PER_WORKER:-2}"
UPLOAD_CACHE_TTL_SECONDS="${UPLOAD_CACHE_TTL_SECONDS:-600}"
UPLOAD_CACHE_MAX_MB="${UPLOAD_CACHE_MAX_MB:-256}"
UPLOAD_CACHE_MAX_ITEMS="${UPLOAD_CACHE_MAX_ITEMS:-100}"
KDE_MAX_SAMPLE_SIZE="${KDE_MAX_SAMPLE_SIZE:-20000}"
RUG_MAX_POINTS="${RUG_MAX_POINTS:-300}"
MEMORY_LIMIT="${MEMORY_LIMIT:-1g}"
CPU_LIMIT="${CPU_LIMIT:-2.0}"
SHARE_PUBLIC_KEYRING="${SHARE_PUBLIC_KEYRING:-}"
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VERSION=$(tr -d '\r\n ' < "$PROJECT_DIR/VERSION")
SIGNING_KEY_FILE="${SHARE_SIGNING_KEY_FILE:-$PROJECT_DIR/.share-signing-key}"
PUBLIC_KEYRING_FILE="${SHARE_PUBLIC_KEYRING_FILE:-$PROJECT_DIR/.share-public-keyring.json}"

if [ -z "$SHARE_PUBLIC_KEYRING" ] && [ -f "$PUBLIC_KEYRING_FILE" ]; then
  SHARE_PUBLIC_KEYRING=$(tr -d '\r\n' < "$PUBLIC_KEYRING_FILE")
fi

if [ -n "${SHARE_SIGNING_PRIVATE_KEY:-}" ]; then
  SIGNING_KEY="$SHARE_SIGNING_PRIVATE_KEY"
elif [ -f "$SIGNING_KEY_FILE" ]; then
  chmod 600 "$SIGNING_KEY_FILE"
  SIGNING_KEY=$(tr -d '\r\n ' < "$SIGNING_KEY_FILE")
else
  if ! command -v openssl >/dev/null 2>&1; then
    echo "OpenSSL wird zum Erzeugen des persistenten Signierschlüssels benötigt." >&2
    exit 1
  fi
  umask 077
  SIGNING_KEY=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\r\n')
  printf '%s' "$SIGNING_KEY" > "$SIGNING_KEY_FILE"
  echo "Neuen persistenten Signierschlüssel in '$SIGNING_KEY_FILE' erstellt."
fi

if [ "${#SIGNING_KEY}" -ne 43 ] || ! printf '%s' "$SIGNING_KEY" | grep -Eq '^[A-Za-z0-9_-]{43}$'; then
  echo "Die Signierschlüsseldatei enthält keinen gültigen 32-Byte-Base64url-Seed." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker wurde nicht gefunden." >&2
  exit 1
fi

echo "Baue Image '$IMAGE_NAME' (Version $VERSION) ..."
docker build --pull --build-arg "APP_VERSION=$VERSION" --tag "$IMAGE_NAME" "$PROJECT_DIR"

CANDIDATE_NAME="${CONTAINER_NAME}-candidate"
ROLLBACK_NAME="${CONTAINER_NAME}-rollback"
OLD_EXISTS=0
OLD_WAS_RUNNING=0
OLD_RENAMED=0
CANDIDATE_STARTED=0
CANDIDATE_PROMOTED=0
SECRET_ENV_FILE=""

if docker ps -a --filter "name=^/${CONTAINER_NAME}$" --format '{{.ID}}' | grep -q .; then
  OLD_EXISTS=1
  [ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME")" != "true" ] || OLD_WAS_RUNNING=1
fi
if docker ps -a --filter "name=^/${ROLLBACK_NAME}$" --format '{{.ID}}' | grep -q .; then
  if [ "$OLD_EXISTS" -eq 0 ]; then
    echo "Unterbrochenes Deployment erkannt. Stelle Rollback-Container wieder her." >&2
    if docker ps -a --filter "name=^/${CANDIDATE_NAME}$" --format '{{.ID}}' | grep -q .; then
      docker rm -f "$CANDIDATE_NAME" >/dev/null
    fi
    docker rename "$ROLLBACK_NAME" "$CONTAINER_NAME"
    docker start "$CONTAINER_NAME" >/dev/null
    echo "Vorheriger Container wurde wiederhergestellt. Bitte Deployment erneut starten." >&2
    exit 1
  fi
  echo "Rollback-Container '$ROLLBACK_NAME' existiert neben dem aktiven Container. Bitte Zustand manuell prüfen." >&2
  exit 1
fi
if docker ps -a --filter "name=^/${CANDIDATE_NAME}$" --format '{{.ID}}' | grep -q .; then
  echo "Entferne unvollständigen Kandidaten '$CANDIDATE_NAME' ..."
  docker rm -f "$CANDIDATE_NAME" >/dev/null
fi

rollback() {
  status=$?
  [ "$status" -ne 0 ] || return 0
  trap - EXIT HUP INT TERM
  if [ -n "$SECRET_ENV_FILE" ]; then
    rm -f -- "$SECRET_ENV_FILE" >/dev/null 2>&1 || true
    SECRET_ENV_FILE=""
  fi
  echo "Deployment fehlgeschlagen. Stelle vorherigen Container wieder her." >&2
  if [ "$CANDIDATE_PROMOTED" -eq 1 ]; then
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  elif [ "$CANDIDATE_STARTED" -eq 1 ]; then
    docker logs --tail 80 "$CANDIDATE_NAME" >&2 2>/dev/null || true
    docker rm -f "$CANDIDATE_NAME" >/dev/null 2>&1 || true
  fi
  if [ "$OLD_RENAMED" -eq 1 ]; then
    docker rename "$ROLLBACK_NAME" "$CONTAINER_NAME" >/dev/null 2>&1 || true
    [ "$OLD_WAS_RUNNING" -ne 1 ] || docker start "$CONTAINER_NAME" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap rollback EXIT HUP INT TERM

if [ "$OLD_EXISTS" -eq 1 ]; then
  if [ "$OLD_WAS_RUNNING" -eq 1 ]; then
    echo "Stoppe bisherigen Container erst nach erfolgreichem Image-Build ..."
    docker stop --time 45 "$CONTAINER_NAME" >/dev/null
  fi
  docker rename "$CONTAINER_NAME" "$ROLLBACK_NAME"
  OLD_RENAMED=1
fi

echo "Starte Release-Kandidaten auf 127.0.0.1:$PORT ..."
SECRET_ENV_FILE=$(mktemp "${TMPDIR:-/tmp}/histo-maker-docker-secret.XXXXXX")
printf 'SHARE_SIGNING_PRIVATE_KEY=%s\n' "$SIGNING_KEY" > "$SECRET_ENV_FILE"
chmod 600 "$SECRET_ENV_FILE"
DOCKER_RUN_STATUS=0
docker run --detach \
  --name "$CANDIDATE_NAME" \
  --restart no \
  --stop-timeout 45 \
  --memory "$MEMORY_LIMIT" \
  --cpus "$CPU_LIMIT" \
  --pids-limit 256 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --env-file "$SECRET_ENV_FILE" \
  --env "APP_VERSION=$VERSION" \
  --env "SHARE_PUBLIC_KEYRING=$SHARE_PUBLIC_KEYRING" \
  --env "TRUST_CF_CONNECTING_IP=1" \
  --env "MAX_UPLOAD_MB=$MAX_UPLOAD_MB" \
  --env "RATE_LIMIT_INSPECT_PER_WINDOW=$INSPECT_RATE_LIMIT" \
  --env "RATE_LIMIT_ANALYZE_PER_WINDOW=$ANALYZE_RATE_LIMIT" \
  --env "RATE_LIMIT_ESTIMATE_PER_WINDOW=$ESTIMATE_RATE_LIMIT" \
  --env "MAX_CONCURRENT_INSPECTIONS_PER_WORKER=$MAX_CONCURRENT_INSPECTIONS_PER_WORKER" \
  --env "MAX_CONCURRENT_ANALYSES_PER_WORKER=$MAX_CONCURRENT_ANALYSES_PER_WORKER" \
  --env "UPLOAD_CACHE_TTL_SECONDS=$UPLOAD_CACHE_TTL_SECONDS" \
  --env "UPLOAD_CACHE_MAX_MB=$UPLOAD_CACHE_MAX_MB" \
  --env "UPLOAD_CACHE_MAX_ITEMS=$UPLOAD_CACHE_MAX_ITEMS" \
  --env "KDE_MAX_SAMPLE_SIZE=$KDE_MAX_SAMPLE_SIZE" \
  --env "RUG_MAX_POINTS=$RUG_MAX_POINTS" \
  --publish "127.0.0.1:$PORT:8000" \
  "$IMAGE_NAME" >/dev/null || DOCKER_RUN_STATUS=$?
rm -f -- "$SECRET_ENV_FILE"
SECRET_ENV_FILE=""
SIGNING_KEY=""
[ "$DOCKER_RUN_STATUS" -eq 0 ] || exit "$DOCKER_RUN_STATUS"
CANDIDATE_STARTED=1

deadline=$(( $(date +%s) + 90 ))
health="starting"
while [ "$(date +%s)" -lt "$deadline" ]; do
  sleep 2
  health=$(docker inspect --format '{{.State.Health.Status}}' "$CANDIDATE_NAME" 2>/dev/null || echo "missing")
  [ "$health" != "healthy" ] || break
  [ "$health" != "unhealthy" ] || { echo "Release-Kandidat ist unhealthy." >&2; exit 1; }
done
[ "$health" = "healthy" ] || { echo "Release-Kandidat wurde nicht innerhalb von 90 Sekunden healthy." >&2; exit 1; }

docker rename "$CANDIDATE_NAME" "$CONTAINER_NAME"
CANDIDATE_STARTED=0
CANDIDATE_PROMOTED=1
docker update --restart unless-stopped "$CONTAINER_NAME" >/dev/null
if [ "$OLD_RENAMED" -eq 1 ]; then
  docker rm "$ROLLBACK_NAME" >/dev/null
  OLD_RENAMED=0
fi
trap - EXIT HUP INT TERM

echo "Histo Maker läuft lokal unter http://127.0.0.1:$PORT"
docker ps --filter "name=^/${CONTAINER_NAME}$"
