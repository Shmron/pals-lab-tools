#!/bin/bash
# Resumable runner for the Harare building-damage job on the PALs server.
# Finished building tiles are cached on disk, so every retry/restart continues where the last stopped.
cd "$(dirname "$0")"
export EE_SA_KEY=/home/USER/PRECISE/palsearth/palsearth-sa-key.json
export EE_SA_EMAIL=ee-service-account@your-project.iam.gserviceaccount.com
PY=/home/USER/PRECISE/harare_flood_flask/venv/bin/python
for site in harare harare_dense; do
  for attempt in $(seq 1 30); do
    echo "$(date '+%F %T') [$site] attempt $attempt"
    if $PY -W ignore building_flood_damage.py $site; then echo "$(date '+%F %T') [$site] DONE"; break; fi
    echo "$(date '+%F %T') [$site] failed, retrying in 60 s"; sleep 60
  done
done
echo "$(date '+%F %T') ALL DONE"
