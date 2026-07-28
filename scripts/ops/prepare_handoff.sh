#!/bin/bash
# HANDOFF SANITIZATION — removes operator-only assets from THIS machine
# before the partner takes it. DRY-RUN by default; execute with --execute.
# Review the list carefully: deletions are irreversible.
set -u
MODE="${1:---dry-run}"

remove_list=(
  # operator's expansive system (stays with operator; repo is on GitHub)
  "/Users/a2.0/HermesLeadEngine"
  "/Users/a2.0/HermesLeadEngineDeploy-AZ"
  "/Users/a2.0/HermesLeadEngine-storage"
  "/Users/a2.0/HermesLeadEngineBackups"
  "/Users/a2.0/HermesLeadEngineCSLB"
  "/Users/a2.0/HermesLeadEngineQuarantine"
  "/Users/a2.0/HermesLeadEngineResponseQuality"
  # operator's telegram archive (contains tokens)
  "/Users/a2.0/ppa-leadengine/archives"
)

launchd_list=(
  com.hermesleadengine.local-scanner com.hermesleadengine.workers com.hermesleadengine.exports
  com.hermesleadengine.rif-scheduler com.hermesleadengine.postgres-backup-full
  com.hermesleadengine.daily-report com.hermesleadengine.directory-discovery
  com.hermesleadengine.watchdog com.hermesleadengine.api com.hermesleadengine.queue-supervisor
  com.hermesleadengine.wave-scheduler com.hermesleadengine.dashboard com.hermesleadengine.backup
  com.hermesleadengine.prune com.hermesleadengine.postgres-backup
  com.hermesleadengine.state-scan-coordinator com.hermesleadengine.directory-lane
  com.hermesleadengine.partner-bot com.hermesleadengine.daily-delivery
)

echo "=== launchd jobs to bootout + remove plists ==="
for j in "${launchd_list[@]}"; do echo "  $j"; done
echo "=== paths to delete ==="
for p in "${remove_list[@]}"; do echo "  $p"; done
echo "=== also (manual review) ==="
echo "  ~/.git-credentials / ~/.ssh (GitHub push keys for operator repos)"
echo "  operator Webshare creds anywhere under ppa-leadengine (already default-off)"

if [ "$MODE" = "--execute" ]; then
  for j in "${launchd_list[@]}"; do
    launchctl bootout "gui/$(id -u)/$j" 2>/dev/null
    rm -f "$HOME/Library/LaunchAgents/$j.plist"
  done
  for p in "${remove_list[@]}"; do
    [ -e "$p" ] && rm -rf "$p" && echo "deleted $p"
  done
  echo "SANITIZATION DONE. Delivery system remaining: ~/ppa-leadengine + ~/.hermes + com.ppa.* + ai.hermes.gateway"
else
  echo
  echo "DRY-RUN only. Run with --execute when the operator confirms."
fi
