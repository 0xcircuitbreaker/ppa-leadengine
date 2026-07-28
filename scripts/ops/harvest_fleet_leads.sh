#!/usr/bin/env bash
# Harvest leads from all fleet nodes: dedup per node remotely, pull one CSV per node.
PASS='HU7=+@Xuxon'
OUT=/Users/a2.0/ppa-leadengine/exports/fleet_harvest
mkdir -p "$OUT"

FLEET="147.182.176.5 157.230.210.205 147.182.219.185 159.223.138.235 137.184.202.10 143.198.160.35 134.209.75.52"

DEDUP_SCRIPT='
import csv, re, glob, sys
seen = set()
rows = []
for fn in glob.glob("/root/leadgen/exports/fresh_1m/*.csv") + glob.glob("/root/grid_bundle/exports/*.csv"):
    try:
        with open(fn) as f:
            for r in csv.DictReader(f):
                p = re.sub(r"\D", "", str(r.get("phone", "")))
                if len(p) < 10 or p[-10:] in seen: continue
                seen.add(p[-10:])
                rows.append(r)
    except: pass
w = csv.DictWriter(open("/root/leadgen/exports/node_leads.csv", "w", newline=""), fieldnames=["business_name","phone","phone_type","category","city","state","source","discovery_method","website","is_sole_proprietor","found_at"], extrasaction="ignore")
w.writeheader()
for r in rows: w.writerow(r)
print(len(rows))
'

for ip in $FLEET; do
  n=$(sshpass -p "$PASS" ssh -n -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o UserKnownHostsFile=/dev/null root@"$ip" \
    "python3 -c $(printf '%q' "$DEDUP_SCRIPT")" < /dev/null 2>/dev/null | tail -1 | tr -d ' \r\n')
  if [ -n "${n}" ] && [ "${n}" != "0" ] 2>/dev/null; then
    sshpass -p "$PASS" scp -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o UserKnownHostsFile=/dev/null \
      root@"$ip":/root/leadgen/exports/node_leads.csv "$OUT/node_${ip}.csv" 2>/dev/null
    echo "$ip: $n leads pulled"
  else
    echo "$ip: 0 or unreachable"
  fi
done

# Local machines (LAN): Quasar + Dell — pull their fresh leads too (crash-safety)
QCOUNT=$(ssh -o ConnectTimeout=8 quasar 'python3 /Users/quasar/ppa-leadengine/scripts/ops/harvest_node.py /Users/quasar/ppa-leadengine 2>/dev/null' | tail -1 | tr -d ' \r\n')
if [ -n "${QCOUNT}" ] && [ "${QCOUNT}" != "0" ] 2>/dev/null; then
  scp -o ConnectTimeout=10 quasar:/Users/quasar/ppa-leadengine/exports/node_leads.csv "$OUT/node_quasar.csv" 2>/dev/null
  echo "quasar: $QCOUNT leads pulled"
else
  echo "quasar: 0 or unreachable"
fi
echo "HARVEST_DONE: $(ls "$OUT" | wc -l) node files"
# Local machine's own scanner output -> node_local.csv (the fresh_1m gap fix)
python3 - <<'PYEOF'
import csv, glob, re
OUT = "/Users/a2.0/ppa-leadengine/exports/fleet_harvest"
FIELDS = ["business_name","phone","phone_type","category","city","state","source","discovery_method","website","is_sole_proprietor","found_at"]
seen, rows = set(), []
for fn in sorted(glob.glob("/Users/a2.0/ppa-leadengine/exports/fresh_1m/*.csv")):
    try:
        with open(fn, errors="replace") as f:
            for r in csv.DictReader(f):
                p = re.sub(r"\D", "", r.get("phone", ""))
                if len(p) >= 10 and p not in seen:
                    seen.add(p); rows.append(r)
    except Exception:
        pass
with open(f"{OUT}/node_local.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
    w.writeheader(); w.writerows(rows)
print(f"local: {len(rows)} leads consolidated")
PYEOF
