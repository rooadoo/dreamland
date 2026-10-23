#!/bin/sh
# Build a minimal, timestamped tarball for VPS deployment of the DREAMLAND
# backend. The package is an allowlist mirroring .dockerignore: only the files
# the Docker build context and server operation need. Local configuration,
# worlds, keys, and diagnostics never enter the package.
#
# Usage: deploy/package-for-server.sh [OUTPUT_DIR]
#   OUTPUT_DIR defaults to tmp/ under the repository root.
set -eu

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_directory/.." && pwd)

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
package_name="dreamland-backend-$timestamp"
output_directory=${1:-"$repo_root/tmp"}
mkdir -p "$output_directory"
output_directory=$(CDPATH= cd -- "$output_directory" && pwd)
output_file="$output_directory/$package_name.tar.gz"

# Allowlist: everything the Docker build context (deploy/Dockerfile) and the
# server-side operator tooling need, and nothing else.
#   pyproject.toml, README.md, LICENSE  -> wheel build inputs (see Dockerfile)
#   src/dreamland                       -> package source and package data
#   deploy                              -> Dockerfile, requirements locks,
#                                          compose files, dreamlandctl
#   contracts/backend-deployment        -> deployment contract + fixtures
included_paths="pyproject.toml README.md LICENSE src/dreamland deploy contracts/backend-deployment"

for path in $included_paths; do
    if [ ! -e "$repo_root/$path" ]; then
        printf 'Missing required path: %s\n' "$path" >&2
        exit 1
    fi
done

staging_directory=$(mktemp -d)
trap 'rm -rf "$staging_directory"' EXIT INT TERM
package_root="$staging_directory/$package_name"
mkdir -p "$package_root"

# Copy the allowlist while stripping interpreter artifacts, local secrets, and
# INI configuration (mirrors .dockerignore's deny rules). .git, .venv, tests,
# tools, docs, tmp, build, state_profiles, and lobby are excluded implicitly:
# they are never part of the allowlist above.
(
    cd "$repo_root"
    # shellcheck disable=SC2086
    tar -cf - \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='*.pyo' \
        --exclude='*.pyd' \
        --exclude='*.egg-info' \
        --exclude='.env' \
        --exclude='.env.*' \
        --exclude='*.pem' \
        --exclude='*.key' \
        --exclude='*.ini' \
        --exclude='.DS_Store' \
        $included_paths
) | tar -xf - -C "$package_root"

cat > "$package_root/SERVER-DEPLOYMENT.txt" <<EOF
DREAMLAND backend deployment package ($package_name)
Built: $timestamp (UTC)

Contents
  pyproject.toml, README.md, LICENSE   wheel build inputs
  src/dreamland/                       backend source and package data
  deploy/                              Dockerfile, pinned requirements,
                                       compose files, dreamland-deploy,
                                       dreamlandctl
  contracts/backend-deployment/        deployment contract and fixtures

Requirements on the server
  Docker Engine 28.0.0 or newer with the Compose plugin. No Python needed on
  the host; everything runs inside the container (CPython 3.12, linux/amd64).

Install
  1. Copy this tarball to the server and extract it:
       tar -xzf $package_name.tar.gz
       cd $package_name

  2. Install. This builds the image, initializes state, prompts for any
     missing credentials, validates, and starts SSH. Credentials are entered
     inside the container and never appear in arguments, files, or logs:
       deploy/dreamland-deploy install --profile beta

     SSH binds to 127.0.0.1:2222 by default. For external beta access:
       DREAMLAND_SSH_HOST=0.0.0.0 deploy/dreamland-deploy install --profile beta

     Re-running install is safe: existing worlds, configuration, and stored
     credentials are preserved, and only missing values are requested.

Lifecycle
  Back up the complete deployment volume (includes secrets; keep it private):
    deploy/dreamland-deploy backup
  Upgrade: extract the newer package, then from that new directory run:
    deploy/dreamland-deploy upgrade
  Roll back to the previous image, leaving data untouched:
    deploy/dreamland-deploy rollback --image IMAGE_ID
  Roll back to a point in time, replacing the volume:
    deploy/dreamland-deploy rollback --backup BACKUP_NAME
  Inspect deployment, image, validation, and backups:
    deploy/dreamland-deploy status

Operate the running deployment
    deploy/dreamlandctl status
    deploy/dreamlandctl logs
    deploy/dreamlandctl console
    deploy/dreamlandctl stop | restart

Notes
  - Host SSH administration stays on port 22; DREAMLAND listens on 2222.
  - The package carries the three immutable lobby seed worlds as package data
    under src/dreamland/default_state_profiles/. Initialization copies them
    into the volume. It carries no mutable world state, keys, or secrets.
  - State lives in the dreamland-data named volume.
  - Re-running step 3 validates rather than overwrites existing state.
EOF

tar -czf "$output_file" -C "$staging_directory" "$package_name"

# Verify the archive: required files present, forbidden artifacts absent.
manifest=$(tar -tzf "$output_file")
for required in \
    "$package_name/pyproject.toml" \
    "$package_name/README.md" \
    "$package_name/LICENSE" \
    "$package_name/SERVER-DEPLOYMENT.txt" \
    "$package_name/deploy/Dockerfile" \
    "$package_name/deploy/compose.yaml" \
    "$package_name/deploy/dreamlandctl" \
    "$package_name/deploy/dreamland-deploy" \
    "$package_name/deploy/requirements-build.txt" \
    "$package_name/deploy/requirements-runtime.txt" \
    "$package_name/src/dreamland/__main__.py" \
    "$package_name/src/dreamland/deployment/__main__.py"; do
    if ! printf '%s\n' "$manifest" | grep -qx "$required"; then
        printf 'Package verification failed: missing %s\n' "$required" >&2
        exit 1
    fi
done
if printf '%s\n' "$manifest" | grep -E \
    '(__pycache__|\.egg-info|\.py[cod]$|\.ini$|\.pem$|\.key$|/\.env$|/\.env\.|/\.git/|/\.venv/|/tests/|/tools/|/docs/|/tmp/|/build/|/state_profiles/|/lobby/)'; then
    printf 'Package verification failed: forbidden entries listed above.\n' >&2
    exit 1
fi

file_count=$(printf '%s\n' "$manifest" | grep -cv '/$')
printf 'Created %s (%s, %s files)\n' \
    "$output_file" "$(du -h "$output_file" | cut -f1)" "$file_count"
printf 'Server deployment instructions are inside the package at %s/SERVER-DEPLOYMENT.txt\n' "$package_name"
printf 'Quick start on the server:\n'
printf '  tar -xzf %s.tar.gz && cd %s\n' "$package_name" "$package_name"
printf '  DREAMLAND_SSH_HOST=0.0.0.0 deploy/dreamland-deploy install --profile beta\n'
