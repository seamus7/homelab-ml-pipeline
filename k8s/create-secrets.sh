#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Error: .env file not found at $ENV_FILE" >&2
  exit 1
fi

# Load only the variables we need
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)
RUNNER_TOKEN=$(grep -E '^RUNNER_TOKEN=' "$ENV_FILE" | cut -d= -f2-)

if [[ -z "$POSTGRES_PASSWORD" ]]; then
  echo "Error: POSTGRES_PASSWORD not found in .env" >&2
  exit 1
fi

if [[ -z "$RUNNER_TOKEN" ]]; then
  echo "Error: RUNNER_TOKEN not found in .env" >&2
  exit 1
fi

kubectl create secret generic postgres-secret \
  --from-literal=POSTGRES_USER=admin \
  --from-literal=POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
  --from-literal=POSTGRES_DB=postgres \
  --dry-run=client -o yaml | kubectl apply -f -

echo "postgres-secret applied."

kubectl create secret generic runner-secret \
  --from-literal=RUNNER_TOKEN="$RUNNER_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "runner-secret applied."
