#!/bin/bash
# OKX BB Deploy Script — ensures code is actually running after changes
# Usage: ./scripts/deploy_okx_bb.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_DIR="$HOME/.openclaw/workspace/trading/okx_bb"
SERVICE="okx-bb-monitor"

echo "=== OKX BB Deploy ==="

# 1. Check for uncommitted changes
cd "$REPO_DIR"
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "❌ Uncommitted changes in repo! Commit first."
    git status --short
    exit 1
fi
COMMIT=$(git rev-parse --short HEAD)
echo "✅ Repo clean at commit $COMMIT"

# 2. Run tests
echo "--- Running tests ---"
source "$HOME/.openclaw/workspace/trading/.venv/bin/activate"
export OKX_BB_CONFIG_DIR="$RUNTIME_DIR/config"
pytest "$REPO_DIR/okx_bb/tests/" -q --tb=short 2>&1
echo "✅ Tests passed"

# 3. Copy to runtime
echo "--- Deploying to runtime ---"
for f in ws_monitor.py executor.py strategy.py exchange.py config.py cleanup.py; do
    if [ -f "$REPO_DIR/okx_bb/$f" ]; then
        cp "$REPO_DIR/okx_bb/$f" "$RUNTIME_DIR/$f"
    fi
done
# Also copy core/
cp "$REPO_DIR/core/"*.py "$HOME/.openclaw/workspace/trading/core/" 2>/dev/null || true
echo "✅ Files copied to runtime"

# 4. Verify MD5 match
REPO_MD5=$(md5sum "$REPO_DIR/okx_bb/ws_monitor.py" | cut -d' ' -f1)
RUNTIME_MD5=$(md5sum "$RUNTIME_DIR/ws_monitor.py" | cut -d' ' -f1)
if [ "$REPO_MD5" != "$RUNTIME_MD5" ]; then
    echo "❌ MD5 mismatch after copy!"
    exit 1
fi
echo "✅ MD5 verified: $REPO_MD5"

# 5. Restart service
echo "--- Restarting $SERVICE ---"
OLD_PID=$(systemctl show -p MainPID $SERVICE | cut -d= -f2)
sudo systemctl restart $SERVICE
sleep 5
NEW_PID=$(systemctl show -p MainPID $SERVICE | cut -d= -f2)

if [ "$NEW_PID" = "$OLD_PID" ] || [ "$NEW_PID" = "0" ]; then
    echo "❌ Service did not restart! PID unchanged or 0"
    systemctl status $SERVICE
    exit 1
fi
echo "✅ Restarted: PID $OLD_PID → $NEW_PID"

# 6. Verify startup logs
echo "--- Startup logs ---"
journalctl -u $SERVICE --since "30s ago" --no-pager 2>&1 | grep -v "Hint\|Users in\|Pass -q" | head -15

# 7. Check for errors
ERRORS=$(journalctl -u $SERVICE --since "30s ago" --no-pager 2>&1 | grep -ci "error\|traceback\|exception" || true)
if [ "$ERRORS" -gt 0 ]; then
    echo "⚠️ Found $ERRORS error lines in startup — check above!"
else
    echo "✅ No errors in startup"
fi

echo ""
echo "=== Deploy complete: commit $COMMIT → PID $NEW_PID ==="
