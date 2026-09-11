#!/bin/sh
#
# Pull and redeploy, doing nothing if nothing that reaches the image changed.
#
# Run on the NAS from the project directory:
#
#     ./deploy.sh
#
# POSIX sh rather than bash: DSM's shell varies between versions and nothing
# here needs more.

set -eu

# --- locate compose -------------------------------------------------------
# Compose v2 is a docker subcommand; older DSM ships the v1 standalone binary.
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    echo "error: neither 'docker compose' nor 'docker-compose' is available" >&2
    exit 1
fi

# --- pull -----------------------------------------------------------------
# The commit is compared before and after rather than grepping git's output:
# "Already up to date." is human-facing text that varies with locale and
# version, whereas a revision either changed or it did not.
#
# --ff-only because this checkout is a consumer of origin/main and nothing
# else. Without it, a stray local commit turns a deploy into a merge.
BEFORE=$(git rev-parse HEAD)
git pull --ff-only origin main
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
    echo "Already at $(git rev-parse --short HEAD); nothing pulled."
    # Still make sure the stack is actually up. A previous run may have
    # pulled successfully and then failed to build, which would otherwise
    # leave the site down and every later deploy declining to fix it.
    if [ -z "$($COMPOSE ps --quiet --status running 2>/dev/null)" ]; then
        echo "Nothing is running, though. Starting it."
        $COMPOSE up -d --build
    fi
    exit 0
fi

echo "Updated $(git rev-parse --short "$BEFORE") -> $(git rev-parse --short "$AFTER")"

# --- decide whether the image has to be rebuilt ---------------------------
# Only paths that end up inside the image, or that define it, require a
# rebuild. A docs-only or test-only commit does not: the Dockerfile copies
# app/ and scripts/ and installs tcg_engine/ and ebay_client/, and nothing
# else.
#
# Note that app/static counts -- the dashboard's HTML, JS and CSS are COPYed
# into the image, so a UI change does need a rebuild even though it feels like
# a static asset. scripts/ counts for the same reason: the one-off scripts are
# run with `docker exec` inside this container, so the copy that matters is
# the one in the image, not the one in the checkout.
CHANGED=$(git diff --name-only "$BEFORE" "$AFTER")
echo "$CHANGED" | sed 's/^/  /'

NEEDS_BUILD=0
for path in $CHANGED; do
    case "$path" in
        app/*|scripts/*|tcg_engine/*|ebay_client/*|requirements.txt|Dockerfile|docker-compose.yml)
            NEEDS_BUILD=1
            break
            ;;
    esac
done

if [ "$NEEDS_BUILD" -eq 0 ]; then
    echo "Only files outside the image changed (docs, tests). No rebuild needed."
    exit 0
fi

# --- rebuild --------------------------------------------------------------
$COMPOSE up -d --build

# Report rather than assume. A build can succeed and the container still exit
# on startup -- a missing dependency did exactly that once, and the symptom
# was a 502 from the reverse proxy rather than anything docker complained
# about.
sleep 5
$COMPOSE ps
echo
echo "If the container is not running, check:  $COMPOSE logs --tail 50"
