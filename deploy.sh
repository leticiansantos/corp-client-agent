#!/usr/bin/env bash
# deploy.sh — Build frontend and deploy corp-client-agent to Databricks Apps
#
# Usage:
#   ./deploy.sh
#
# Required env vars (or set below):
#   DATABRICKS_HOST         — workspace URL, e.g. https://adb-xxx.azuredatabricks.net/
#   DATABRICKS_APP_NAME     — name of the Databricks App
#   DATABRICKS_APP_PATH     — workspace path where app files live
#                             e.g. /Users/leticia.santos@databricks.com/apps/corp-client-agent
#
# Optional:
#   DATABRICKS_PROFILE      — CLI profile to use (default: uses env vars / DEFAULT profile)
#   SKIP_FRONTEND_BUILD     — set to "1" to skip npm build (use existing dist/)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Config ──────────────────────────────────────────────────────────────────
DATABRICKS_APP_NAME="${DATABRICKS_APP_NAME:-corp-client-agent-sqlite}"
# Path used by `databricks sync` (no /Workspace prefix)
DATABRICKS_APP_PATH="${DATABRICKS_APP_PATH:-/Users/leticia.santos@databricks.com/corp-client-agent-sqlite}"
# Path used by `apps deploy --source-code-path` (requires /Workspace prefix)
DATABRICKS_APP_DEPLOY_PATH="/Workspace${DATABRICKS_APP_PATH}"
SKIP_FRONTEND_BUILD="${SKIP_FRONTEND_BUILD:-0}"

PROFILE_ARG=""
if [[ -n "${DATABRICKS_PROFILE:-}" ]]; then
  PROFILE_ARG="--profile $DATABRICKS_PROFILE"
fi

# ── 1. Build frontend ────────────────────────────────────────────────────────
if [[ "$SKIP_FRONTEND_BUILD" != "1" ]]; then
  echo "▶ Building frontend..."
  cd "$SCRIPT_DIR/frontend"
  npm install --silent
  npm run build
  cd "$SCRIPT_DIR"
  echo "  ✓ Frontend built → frontend/dist/"
else
  echo "⏭  Skipping frontend build (SKIP_FRONTEND_BUILD=1)"
fi

# ── 2. Copy dist → backend/static ───────────────────────────────────────────
echo "▶ Copying frontend/dist → backend/static..."
rm -rf "$SCRIPT_DIR/backend/static"
cp -r "$SCRIPT_DIR/frontend/dist" "$SCRIPT_DIR/backend/static"
echo "  ✓ backend/static updated"

# ── 3. Generate root requirements.txt (triggers Databricks Apps build phase) ─
WHL_FILE=$(ls "$SCRIPT_DIR/backend/wheels/"*.whl 2>/dev/null | sort -V | tail -1)
if [[ -z "$WHL_FILE" ]]; then
  echo "ERROR: No .whl found in backend/wheels/" >&2
  exit 1
fi
WHL_NAME="$(basename "$WHL_FILE")"
cat > "$SCRIPT_DIR/requirements.txt" <<EOF
-r backend/requirements.txt
./backend/wheels/$WHL_NAME
EOF
echo "  ✓ requirements.txt gerado (wheel: $WHL_NAME)"

# ── 4. Sync to Databricks workspace ──────────────────────────────────────────
echo "▶ Syncing to workspace: $DATABRICKS_APP_PATH"

databricks $PROFILE_ARG sync \
  "$SCRIPT_DIR" \
  "$DATABRICKS_APP_PATH" \
  --exclude 'frontend/node_modules' \
  --exclude 'frontend/dist' \
  --exclude 'backend/.venv' \
  --exclude 'backend/local.db*' \
  --exclude 'tasks' \
  --full

echo "  ✓ Files synced"

# ── 5. Restart app ───────────────────────────────────────────────────────────
echo "▶ Restarting app '$DATABRICKS_APP_NAME'..."
databricks $PROFILE_ARG apps deploy "$DATABRICKS_APP_NAME" \
  --source-code-path "$DATABRICKS_APP_DEPLOY_PATH"

echo ""
echo "✅ Deploy concluído: $DATABRICKS_APP_NAME"
