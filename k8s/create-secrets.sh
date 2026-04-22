#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Error: .env file not found at $ENV_FILE" >&2
  exit 1
fi

# Load variables from .env
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)
RUNNER_TOKEN=$(grep -E '^RUNNER_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
GITEA_API_TOKEN=$(grep -E '^GITEA_API_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
OPENROUTER_API_KEY=$(grep -E '^OPENROUTER_API_KEY=' "$ENV_FILE" | cut -d= -f2-)
SUPABASE_DB_PASSWORD=$(grep -E '^SUPABASE_DB_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)
JWT_SECRET=$(grep -E '^JWT_SECRET=' "$ENV_FILE" | cut -d= -f2-)
AUTHENTICATOR_PASS=$(grep -E '^AUTHENTICATOR_PASS=' "$ENV_FILE" | cut -d= -f2-)
SUPABASE_SERVICE_ROLE_KEY=$(grep -E '^SUPABASE_SERVICE_ROLE_KEY=' "$ENV_FILE" | cut -d= -f2-)
MCP_ACCESS_KEY=$(grep -E '^MCP_ACCESS_KEY=' "$ENV_FILE" | cut -d= -f2-)
DEFAULT_USER_ID=$(grep -E '^DEFAULT_USER_ID=' "$ENV_FILE" | cut -d= -f2-)

require_var() {
  local name="$1" value="$2"
  if [[ -z "$value" ]]; then
    echo "Error: $name not found in .env" >&2
    exit 1
  fi
}

require_var POSTGRES_PASSWORD     "$POSTGRES_PASSWORD"
require_var RUNNER_TOKEN          "$RUNNER_TOKEN"
require_var GITEA_API_TOKEN       "$GITEA_API_TOKEN"
require_var OPENROUTER_API_KEY    "$OPENROUTER_API_KEY"
require_var SUPABASE_DB_PASSWORD  "$SUPABASE_DB_PASSWORD"
require_var JWT_SECRET            "$JWT_SECRET"
require_var AUTHENTICATOR_PASS    "$AUTHENTICATOR_PASS"
require_var SUPABASE_SERVICE_ROLE_KEY "$SUPABASE_SERVICE_ROLE_KEY"
require_var MCP_ACCESS_KEY        "$MCP_ACCESS_KEY"
require_var DEFAULT_USER_ID       "$DEFAULT_USER_ID"

kubectl create secret generic postgres-secret \
  --from-literal=POSTGRES_USER=admin \
  --from-literal=POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
  --from-literal=POSTGRES_DB=postgres \
  --dry-run=client -o yaml | kubectl apply -f -
echo "postgres-secret applied."

kubectl create secret generic postgrest-secret \
  --from-literal=db-uri="postgres://authenticator:${AUTHENTICATOR_PASS}@supabase-db:5432/postgres" \
  --from-literal=jwt-secret="${JWT_SECRET}" \
  --dry-run=client -o yaml | kubectl apply -f -
echo "postgrest-secret applied."

kubectl create secret generic runner-secret \
  --from-literal=RUNNER_TOKEN="$RUNNER_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -
echo "runner-secret applied."

kubectl create secret generic gitea-token \
  --from-literal=token="${GITEA_API_TOKEN}" \
  --dry-run=client -o yaml | kubectl apply -f -
echo "gitea-token applied."

kubectl create secret generic openrouter-secret \
  --from-literal=api_key="${OPENROUTER_API_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f -
echo "openrouter-secret applied."

kubectl create secret generic supabase-postgres-secret \
  --from-literal=password="${SUPABASE_DB_PASSWORD}" \
  --dry-run=client -o yaml | kubectl apply -f -
echo "supabase-postgres-secret applied."

kubectl create secret generic open-brain-mcp-secret \
  --from-literal=supabase-url="http://postgrest:3000" \
  --from-literal=service-role-key="${SUPABASE_SERVICE_ROLE_KEY}" \
  --from-literal=mcp-access-key="${MCP_ACCESS_KEY}" \
  --from-literal=ollama-url="http://ollama:11434" \
  --from-literal=litellm-url="http://litellm:4000" \
  --dry-run=client -o yaml | kubectl apply -f -
echo "open-brain-mcp-secret applied."

kubectl create secret generic work-operating-model-secret \
  --from-literal=supabase-url="http://postgrest:3000" \
  --from-literal=service-role-key="${SUPABASE_SERVICE_ROLE_KEY}" \
  --from-literal=mcp-access-key="${MCP_ACCESS_KEY}" \
  --from-literal=default-user-id="${DEFAULT_USER_ID}" \
  --dry-run=client -o yaml | kubectl apply -f -
echo "work-operating-model-secret applied."
