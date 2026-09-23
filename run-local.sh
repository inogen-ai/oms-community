#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

requested_dir=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --publish-dir)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        echo "Usage: ./run-local.sh [--publish-dir PATH]" >&2
        exit 1
      fi
      requested_dir=$2
      shift 2
      ;;
    --help|-h)
      echo 'Usage: ./run-local.sh [--publish-dir PATH]'
      echo 'Choose a permanent folder for published skills, for example ~/.oms_skills.'
      echo 'The folder and Docker project are remembered in .env.local for later starts.'
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker with Compose is required. Install Docker, then run this command again." >&2
  exit 1
fi
if [ -L .env.local ] || { [ -e .env.local ] && [ ! -f .env.local ]; }; then
  echo "Refusing an invalid or linked .env.local; use a regular local configuration file." >&2
  exit 1
fi
if [ ! -f .env.local ]; then
  umask 077
  if command -v openssl >/dev/null 2>&1; then
    password=$(openssl rand -hex 24)
  elif command -v python3 >/dev/null 2>&1; then
    password=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
  else
    echo "OpenSSL or Python 3 is required to generate the local database password." >&2
    exit 1
  fi
  (set -C; printf 'OMS_NEO4J_PASSWORD=%s\n' "$password" > .env.local) || {
    echo "Another launcher created .env.local; using that file." >&2
  }
fi
if [ ! -f .env.local ] || [ -L .env.local ]; then
  echo "Refusing an invalid or linked .env.local; use a regular local configuration file." >&2
  exit 1
fi
chmod 600 .env.local
# Keep agent-consumable files on the host rather than inside a named Docker
# volume. Ask Compose to read its own dotenv syntax; never source .env.local
# as shell code. Capture the configuration without printing secrets.
configuration=$(docker compose --env-file .env.local config --environment)
saved_dir=$(printf '%s\n' "$configuration" | sed -n 's/^OMS_PUBLISH_HOST_ROOT=//p')
COMPOSE_PROJECT_NAME=$(printf '%s\n' "$configuration" | sed -n 's/^COMPOSE_PROJECT_NAME=//p')
if [ -z "$COMPOSE_PROJECT_NAME" ]; then
  echo "Docker Compose did not identify the workspace project." >&2
  exit 1
fi
publication_dir=${requested_dir:-${saved_dir:-"$PWD/published"}}
case "$publication_dir" in
  '~') publication_dir=$HOME ;;
  '~/'*) publication_dir="$HOME/${publication_dir#\~/}" ;;
  '~'*) echo "Use ~/ or an absolute folder path, not another user's ~name." >&2; exit 1 ;;
esac
if [ "$(printf '%s' "$publication_dir" | LC_ALL=C tr -d '[:cntrl:]')" != "$publication_dir" ]; then
  echo "The publication folder cannot contain control characters." >&2
  exit 1
fi
mkdir -p -- "$publication_dir"
OMS_PUBLISH_HOST_ROOT=$(CDPATH= cd -- "$publication_dir" && pwd -P)
OMS_PUBLISH_HOST_HOME=$(CDPATH= cd -- "$HOME" && pwd -P)
if [ "$OMS_PUBLISH_HOST_ROOT" = / ] || [ "$OMS_PUBLISH_HOST_ROOT" = "$OMS_PUBLISH_HOST_HOME" ] || [ "$OMS_PUBLISH_HOST_ROOT" = "$PWD" ]; then
  echo "Choose a dedicated publication folder, such as ~/.oms_skills, rather than your home or the application directory." >&2
  exit 1
fi
OMS_LOCAL_LAUNCHER_DIR=$PWD
export OMS_PUBLISH_HOST_ROOT OMS_PUBLISH_HOST_HOME OMS_LOCAL_LAUNCHER_DIR COMPOSE_PROJECT_NAME
# Double-quoted Compose values escape backslashes, quotes and interpolation.
# Preserve the password and all unrelated settings, replacing only our keys.
dotenv_quote() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\$/$$/g'
}
umask 077
temporary=$(mktemp .env.local.XXXXXX)
trap 'rm -f "$temporary"' EXIT HUP INT TERM
awk '!/^[[:space:]]*(export[[:space:]]+)?(OMS_PUBLISH_HOST_ROOT|COMPOSE_PROJECT_NAME)[[:space:]]*=/' .env.local > "$temporary"
printf '\nOMS_PUBLISH_HOST_ROOT="%s"\nCOMPOSE_PROJECT_NAME="%s"\n' "$(dotenv_quote "$OMS_PUBLISH_HOST_ROOT")" "$(dotenv_quote "$COMPOSE_PROJECT_NAME")" >> "$temporary"
mv "$temporary" .env.local
OMS_PUBLISH_SET_ACL=0
if [ "$(uname -s)" = Linux ]; then OMS_PUBLISH_SET_ACL=1; fi
export OMS_PUBLISH_SET_ACL
docker compose --env-file .env.local up --build --detach --wait
printf '\nOMS Community is ready at http://localhost:4318\nAPI: http://localhost:4317\n'
printf 'Published bundles: %s\n' "$OMS_PUBLISH_HOST_ROOT"
if [ -n "$requested_dir" ]; then
  printf 'Publish from the browser, then run the install command shown there to connect your agents to the new folder.\n'
fi
