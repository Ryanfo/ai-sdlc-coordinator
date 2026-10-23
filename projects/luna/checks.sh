#!/usr/bin/env bash
# Coordinator checks for Luna (and-digital/luna-skeleton), referenced by projects/SDLC.toml.
#
# Luna is not a workspace monorepo: every module (services/*, packages/*, clients/*, cli)
# installs, lints, builds and tests on its own, and GitLab CI only runs the modules a change
# touches. This does the same in a coordinator worktree (the current directory).
#
#   checks.sh setup          link each module's node_modules to your Luna checkout's (seconds)
#   checks.sh lint|build|test   run that script in each module changed since the base branch
#   checks.sh preview PORT   run the main front end on PORT
#
# Needs the local package registry (Verdaccio on localhost:4873) running: `luna local up`, or
# platform/scripts/local-package-registry.sh from your Luna checkout.
# Lockfiles and package.json files an install rewrites are restored afterwards, so checks leave no changes.

set -uo pipefail

BASE_BRANCH="${LUNA_BASE_BRANCH:-ada-bio-builder}"
# Your own Luna checkout, where `luna local up` has installed every module's dependencies.
CHECKOUT="${LUNA_CHECKOUT:-$HOME/Projects/luna-skeleton}"
REGISTRY="http://localhost:4873/-/ping"
MODULE_GLOBS=(cli services/* packages/* clients/*)

log() { printf '[luna-checks] %s\n' "$*"; }

all_modules() {
  local m
  for m in "${MODULE_GLOBS[@]}"; do
    [ -f "$m/package.json" ] && echo "$m"
  done
}

base_ref() {
  local r
  for r in "origin/$BASE_BRANCH" "$BASE_BRANCH"; do
    if git rev-parse --verify --quiet "$r^{commit}" >/dev/null; then echo "$r"; return; fi
  done
  log "base branch $BASE_BRANCH not found in this repository"; exit 2
}

changed_modules() {
  local mb
  mb=$(git merge-base HEAD "$(base_ref)") || { log "no merge base with $BASE_BRANCH"; exit 2; }
  { git diff --name-only "$mb"; git ls-files --others --exclude-standard; } \
    | awk -F/ '$1=="cli"{print "cli"; next} ($1=="services"||$1=="packages"||$1=="clients") && NF>2 {print $1"/"$2}' \
    | sort -u \
    | while read -r m; do [ -f "$m/package.json" ] && echo "$m"; done
}

has_script() {
  node -e 'process.exit((require(require("path").resolve(process.argv[1], "package.json")).scripts||{})[process.argv[2]] ? 0 : 1)' "$1" "$2"
}

need_registry() {
  if ! curl -fsS -o /dev/null --max-time 5 "$REGISTRY"; then
    log "the local package registry is not running on localhost:4873."
    log "Start it with \`luna local up\` (or platform/scripts/local-package-registry.sh) and rerun."
    exit 3
  fi
}

restore_lockfiles() {
  git ls-files -m -- 'yarn.lock' '*/yarn.lock' 'package.json' '*/package.json' | xargs -r git checkout --
}

# Give a module its own node_modules before anything writes to it: a copy of the checkout's
# (copy-on-write), or a fresh install when the ticket changed its dependencies.
own_modules() {
  local m=$1
  if [ -L "$m/node_modules" ]; then
    local src
    src=$(readlink "$m/node_modules")
    rm "$m/node_modules"
    if git diff --quiet "$(git merge-base HEAD "$(base_ref)")" -- "$m/package.json" "$m/yarn.lock"; then
      log "copy node_modules for $m"
      cp -cR "$src" "$m/node_modules" 2>/dev/null || cp -R "$src" "$m/node_modules"
      return
    fi
  fi
  [ -d "$m/node_modules" ] || install_module "$m"
}

install_module() {
  local m=$1
  log "install $m"
  (
    cd "$m" || exit 1
    # The local registry does not reproduce the tarballs the lockfiles were made from, so the
    # recorded checksums of Luna's own packages are dropped (the lockfile is restored afterwards).
    if [ -f yarn.lock ]; then
      perl -0pi -e 's{(^"?\@bhag-business-platform/.*?)(?=^\S|\z)}{ my $b = $1; $b =~ s/^\s+integrity .*\n//mg; $b =~ s/(resolved "[^"#]+)#[0-9a-f]+"/$1"/g; $b }gems' yarn.lock
    fi
    yarn install --non-interactive --prefer-offline --network-concurrency 4 \
      || { [ -x ../../platform/scripts/refresh-internal-packages.sh ] && ../../platform/scripts/refresh-internal-packages.sh; }
  )
}

cmd=${1:-}
case "$cmd" in
  setup)
    # Installing all 33 modules takes 10+ minutes, so each module's node_modules starts as a
    # link to the checkout's. A check that installs or builds a module copies it first
    # (own_modules), so nothing ever writes into the checkout.
    exclude="$(git rev-parse --git-common-dir)/info/exclude"
    mkdir -p "$(dirname "$exclude")"
    grep -qx 'node_modules' "$exclude" 2>/dev/null || echo 'node_modules' >> "$exclude"
    linked=0; missing=()
    while read -r m; do
      if [ -e "$m/node_modules" ]; then continue; fi
      if [ -d "$CHECKOUT/$m/node_modules" ]; then
        ln -s "$CHECKOUT/$m/node_modules" "$m/node_modules" && linked=$((linked + 1))
      else
        missing+=("$m")
      fi
    done < <(all_modules)
    log "linked node_modules for $linked modules from $CHECKOUT"
    if [ ${#missing[@]} -gt 0 ]; then
      log "not installed in $CHECKOUT (run \`luna local up\` there): ${missing[*]}"
    fi
    ;;
  lint|build|test)
    modules=$(changed_modules)
    if [ -z "$modules" ]; then log "no module changed since $BASE_BRANCH; nothing to $cmd"; exit 0; fi
    failed=()
    for m in $modules; do
      if ! has_script "$m" "$cmd"; then log "$m has no $cmd script; skipped"; continue; fi
      # Lint and test only read node_modules; build writes to it (services' build.sh installs).
      if [ "$cmd" = build ] || ! git diff --quiet "$(git merge-base HEAD "$(base_ref)")" -- "$m/package.json" "$m/yarn.lock" || [ ! -e "$m/node_modules" ]; then
        need_registry
        own_modules "$m" || { failed+=("$m"); continue; }
      fi
      log "$cmd $m"
      (cd "$m" && yarn run "$cmd") || failed+=("$m")
    done
    restore_lockfiles
    if [ ${#failed[@]} -gt 0 ]; then log "$cmd failed in: ${failed[*]}"; exit 1; fi
    log "$cmd passed in: $(echo $modules)"
    ;;
  preview)
    port=${2:?usage: luna-checks.sh preview PORT}
    cd clients/luna-front-end && exec yarn start --port "$port" --strictPort
    ;;
  *)
    echo "usage: luna-checks.sh setup|lint|build|test|preview PORT" >&2
    exit 64
    ;;
esac
