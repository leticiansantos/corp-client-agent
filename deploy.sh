#!/bin/bash
set -e

APP_NAME="corp-client-agent"
WORKSPACE_PATH="/Workspace/Users/leticia.santos@databricks.com/$APP_NAME"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATABRICKS_PROFILE="fevm-leticia-santos-stable"

# echo "=== [1/4] Building frontend ==="
# cd "$SCRIPT_DIR/frontend"
# npm install
# npm run build

# echo ""
# echo "=== [2/4] Building corp-agent-framework wheel ==="
# cd "$SCRIPT_DIR/../corp-agent-framework"
# python3 -m pip install build -q
# python3 -m build --wheel --outdir "$SCRIPT_DIR/wheels/" -q

echo ""
echo "=== [3/4] Copying frontend build to backend/static ==="
rm -rf "$SCRIPT_DIR/backend/static"
mkdir -p "$SCRIPT_DIR/backend/static"
cp -r "$SCRIPT_DIR/frontend/dist/." "$SCRIPT_DIR/backend/static/"

echo ""
echo "=== [4/4] Syncing files to Databricks Workspace ==="
cd "$SCRIPT_DIR"

# Copy corp-agent-framework source so it can be installed as a local package
rm -rf "$SCRIPT_DIR/corp-agent-framework"
cp -r "$SCRIPT_DIR/../corp-agent-framework" "$SCRIPT_DIR/corp-agent-framework"

databricks sync "$SCRIPT_DIR" "$WORKSPACE_PATH" --full --profile "$DATABRICKS_PROFILE"

# Clean up local copy after sync
rm -rf "$SCRIPT_DIR/corp-agent-framework"

echo ""
echo "=== [5/5] Deploying app ==="
databricks apps deploy "$APP_NAME" \
  --source-code-path "$WORKSPACE_PATH" \
  --profile "$DATABRICKS_PROFILE"

echo ""
echo "Done! App URL: https://corp-client-agent-7474658353922350.aws.databricksapps.com"
