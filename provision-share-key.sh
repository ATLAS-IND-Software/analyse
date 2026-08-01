#!/usr/bin/env sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SIGNING_KEY_FILE="${SHARE_SIGNING_KEY_FILE:-$PROJECT_DIR/.share-signing-key}"
PUBLIC_KEYRING_FILE="${SHARE_PUBLIC_KEYRING_FILE:-$PROJECT_DIR/.share-public-keyring.json}"
FORCE=0
REDEPLOY=0
DRY_RUN=0
ALLOW_INVALIDATE=0

usage() {
  echo "Verwendung: $0 [--force] [--deploy] [--dry-run] [--allow-invalidate-existing-links]"
  echo "  --force    Vorhandenen Schlüssel rotieren und bisherigen Public Key erhalten."
  echo "  --deploy   Anschließend deploy.sh mit dem neuen Schlüssel ausführen."
  echo "  --dry-run  Nur Voraussetzungen und geplante Aktion prüfen."
  echo "  --allow-invalidate-existing-links  Rotation ohne Sicherung des bisherigen Public Keys zulassen."
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --force) FORCE=1 ;;
    --deploy) REDEPLOY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --allow-invalidate-existing-links) ALLOW_INVALIDATE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unbekannte Option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

KEY_DIR=$(dirname -- "$SIGNING_KEY_FILE")
if [ ! -d "$KEY_DIR" ]; then
  echo "Das Zielverzeichnis existiert nicht: $KEY_DIR" >&2
  exit 1
fi
if [ -f "$SIGNING_KEY_FILE" ] && [ "$FORCE" -ne 1 ]; then
  echo "Es existiert bereits ein Signierschlüssel. Verwende --force, um ihn bewusst zu rotieren; der bisherige Public Key wird standardmäßig erhalten." >&2
  exit 1
fi
if ! command -v openssl >/dev/null 2>&1; then
  echo "OpenSSL wird zum Erzeugen des Signierschlüssels benötigt." >&2
  exit 1
fi

if [ "$DRY_RUN" -eq 1 ]; then
  if [ -f "$SIGNING_KEY_FILE" ]; then
    echo "Dry-run: Schlüssel würde rotiert, privat gesichert und sein Public Key in $PUBLIC_KEYRING_FILE erhalten."
  else
    echo "Dry-run: Schlüssel würde erzeugt: $SIGNING_KEY_FILE"
  fi
  [ "$REDEPLOY" -eq 1 ] && echo "Dry-run: Anschließend würde deploy.sh ausgeführt."
  exit 0
fi

umask 077
BACKUP_FILE=""
if [ -f "$SIGNING_KEY_FILE" ]; then
  chmod 600 "$SIGNING_KEY_FILE"
  if [ "$ALLOW_INVALIDATE" -eq 1 ]; then
    echo "Warnung: Der bisherige Public Key wird nicht erhalten; bestehende Links können ungültig werden." >&2
  else
    if ! command -v curl >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
      echo "Für eine sichere Rotation werden curl und jq benötigt. Alternativ bewusst --allow-invalidate-existing-links angeben." >&2
      exit 1
    fi
    CURRENT_KEY_JSON=$(curl --fail --silent --show-error "http://127.0.0.1:${PORT:-8000}/api/share-key") || {
      echo "Der aktuelle Public Key konnte nicht abgerufen werden. Container starten oder --allow-invalidate-existing-links bewusst angeben." >&2
      exit 1
    }
    CURRENT_KEY_ID=$(printf '%s' "$CURRENT_KEY_JSON" | jq -er '.key_id | strings | select(test("^[0-9a-f]{16}$"))') || {
      echo "Die Share-Key-Antwort enthält keine gültige aktuelle Key-ID." >&2
      exit 1
    }
    CURRENT_PUBLIC_KEY=$(printf '%s' "$CURRENT_KEY_JSON" | jq -er '.public_key | strings | select(test("^[A-Za-z0-9_-]{43}$"))') || {
      echo "Die Share-Key-Antwort enthält keinen gültigen Ed25519-Public-Key." >&2
      exit 1
    }
    KEYRING_DIR=$(dirname -- "$PUBLIC_KEYRING_FILE")
    [ -d "$KEYRING_DIR" ] || { echo "Das Keyring-Zielverzeichnis existiert nicht: $KEYRING_DIR" >&2; exit 1; }
    TEMPORARY_KEYRING=$(mktemp "$KEYRING_DIR/.share-public-keyring.tmp.XXXXXX")
    if [ -f "$PUBLIC_KEYRING_FILE" ]; then
      jq -e 'type == "object" and all(to_entries[]; (.key | test("^[0-9a-f]{16}$")) and (.value | type == "string") and (.value | test("^[A-Za-z0-9_-]{43}$")))' "$PUBLIC_KEYRING_FILE" >/dev/null || {
        rm -f -- "$TEMPORARY_KEYRING"
        echo "Der vorhandene Public-Key-Keyring enthält ungültige Einträge." >&2
        exit 1
      }
      if ! jq -c --arg key_id "$CURRENT_KEY_ID" --arg public_key "$CURRENT_PUBLIC_KEY" '. + {($key_id): $public_key}' "$PUBLIC_KEYRING_FILE" > "$TEMPORARY_KEYRING"; then
        rm -f -- "$TEMPORARY_KEYRING"
        echo "Der Public-Key-Keyring konnte nicht aktualisiert werden." >&2
        exit 1
      fi
    else
      if ! jq -cn --arg key_id "$CURRENT_KEY_ID" --arg public_key "$CURRENT_PUBLIC_KEY" '{($key_id): $public_key}' > "$TEMPORARY_KEYRING"; then
        rm -f -- "$TEMPORARY_KEYRING"
        echo "Der Public-Key-Keyring konnte nicht erstellt werden." >&2
        exit 1
      fi
    fi
    chmod 600 "$TEMPORARY_KEYRING"
    mv -f -- "$TEMPORARY_KEYRING" "$PUBLIC_KEYRING_FILE"
    echo "Aktuellen Public Key $CURRENT_KEY_ID im historischen Keyring gesichert: $PUBLIC_KEYRING_FILE"
  fi
  TIMESTAMP=$(date -u '+%Y%m%dT%H%M%SZ')
  BACKUP_FILE="$SIGNING_KEY_FILE.backup.$TIMESTAMP"
  cp -- "$SIGNING_KEY_FILE" "$BACKUP_FILE"
  chmod 600 "$BACKUP_FILE"
fi

TEMPORARY_FILE=$(mktemp "$KEY_DIR/.share-signing-key.tmp.XXXXXX")
cleanup() {
  [ ! -f "$TEMPORARY_FILE" ] || rm -f -- "$TEMPORARY_FILE"
}
trap cleanup EXIT HUP INT TERM
openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\r\n' > "$TEMPORARY_FILE"
chmod 600 "$TEMPORARY_FILE"
mv -f -- "$TEMPORARY_FILE" "$SIGNING_KEY_FILE"
trap - EXIT HUP INT TERM

echo "Neuer Signierschlüssel wurde provisioniert: $SIGNING_KEY_FILE"
[ -z "$BACKUP_FILE" ] || echo "Vorheriger Schlüssel wurde gesichert: $BACKUP_FILE"

if [ "$REDEPLOY" -eq 1 ]; then
  echo "Deploye Container mit dem neuen Schlüssel ..."
  SHARE_SIGNING_KEY_FILE="$SIGNING_KEY_FILE" SHARE_PUBLIC_KEYRING_FILE="$PUBLIC_KEYRING_FILE" "$PROJECT_DIR/deploy.sh"
else
  echo "Der laufende Container verwendet den neuen Schlüssel erst nach einem Redeployment."
fi
