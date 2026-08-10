#!/usr/bin/env bash
# Deploy the stack, reading the league key from git-ignored .env.deploy.
#
#   ./scripts/deploy.sh diff     review the change
#   ./scripts/deploy.sh deploy   apply it
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env.deploy ]]; then
  echo "Missing .env.deploy — copy .env.deploy.example and fill it in." >&2
  exit 1
fi
set -a; source .env.deploy; set +a

if [[ -z "${LEAGUE_KEY:-}" || "$LEAGUE_KEY" == *000000* ]]; then
  echo "LEAGUE_KEY is unset or still the placeholder." >&2
  exit 1
fi

# The dependency layer holds platform-specific wheels and is git-ignored, so
# build it every time rather than trusting whatever is on disk.
./scripts/build-layer.sh

ACTION="${1:-diff}"
echo "→ cdk $ACTION   league=$LEAGUE_KEY   profile=$AWS_PROFILE   region=$AWS_REGION"
exec ./node_modules/.bin/cdk "$ACTION" -c "league_key=$LEAGUE_KEY" "${@:2}"
