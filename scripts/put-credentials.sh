#!/usr/bin/env bash
# Store secrets in Parameter Store, with the account pinned.
#
#   ./scripts/put-credentials.sh x        # the four X OAuth 1.0a values
#
# Why this exists: a bare `aws ssm put-parameter` uses whatever profile the
# shell happens to have, and an export that doesn't carry between commands
# silently writes live credentials to the wrong AWS account. This script reads
# the profile from .env.deploy and verifies the account before writing anything.
set -euo pipefail
cd "$(dirname "$0")/.."

EXPECTED_ACCOUNT=159198628641

[[ -f .env.deploy ]] || { echo "Missing .env.deploy" >&2; exit 1; }
set -a; source .env.deploy; set +a
export AWS_PROFILE AWS_REGION

actual=$(aws sts get-caller-identity --query Account --output text)
if [[ "$actual" != "$EXPECTED_ACCOUNT" ]]; then
  echo "Refusing to write: profile '$AWS_PROFILE' is account $actual, expected $EXPECTED_ACCOUNT." >&2
  exit 1
fi

case "${1:-}" in
  x)     prefix=/gotffl/x
         keys=(consumer_key consumer_secret access_token access_token_secret) ;;
  yahoo) prefix=/gotffl/yahoo
         keys=(client_id client_secret refresh_token) ;;
  *)     echo "usage: $0 {x|yahoo}" >&2; exit 1 ;;
esac

echo "Writing to $prefix in account $actual ($AWS_REGION). Values are not echoed."
for k in "${keys[@]}"; do
  read -rsp "  $k: " v && echo
  [[ -n "$v" ]] || { echo "    skipped (empty)"; continue; }
  aws ssm put-parameter --name "$prefix/$k" --value "$v" --type SecureString --overwrite >/dev/null
  echo "    stored (${#v} chars)"
  unset v
done

echo
aws ssm get-parameters-by-path --path "$prefix" --query 'Parameters[].Name' --output text \
  | tr '\t' '\n' | sed 's/^/  /'
