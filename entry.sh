#!/bin/bash

# This script is the entrypoint for the Tapis Pods Service containers.
# api, health, and spawner. If any of these break, the script will sleep for 60 and exit 
# with a non-zero exit code. This will cause the container to restart in Kubernetes.
# Set DEBUG_SLEEP_LOOP to "true" to keep the container running for debugging.

if [ $PODS_COMPONENT = "api" ]; then
    # Write openapi.json to /home/tapis/docs/
    python3 -u /home/tapis/service/auto_openapi_writer.py
    # Set up stores during init.
    python3 -u /home/tapis/service/stores.py
    # Start API. PODS_UVICORN_RELOAD=true (dev only, #DEV-gated in api.yml) adds --reload:
    # with the dev hostPath mount of service/, code edits go live without a pod restart —
    # only migrations/config changes need a redeploy. Note: --reload forces single-process.
    cd /home/tapis/service; uvicorn api:api --workers ${PODS_UVICORN_WORKERS:-1} --host 0.0.0.0 --port 8000 $([ "$PODS_UVICORN_RELOAD" = "true" ] && echo "--reload")
    # prod - https://www.uvicorn.org/deployment/
    # gunicorn uvicorn.worker stuff
elif [ $PODS_COMPONENT = "health" ]; then
    # Start health
    python3 -u /home/tapis/service/health.py

elif [ $PODS_COMPONENT = "health-central" ]; then
    # Start health
    python3 -u /home/tapis/service/health_central.py

elif [ $PODS_COMPONENT = "remote" ]; then
    python3 -u /home/tapis/service/health_remote.py

elif [ $PODS_COMPONENT = "remotecentral" ]; then
    python3 -u /home/tapis/service/health_remote_central.py

elif [ $PODS_COMPONENT = "spawner" ]; then
    # Start spawner
    python3 -u /home/tapis/service/spawner.py
else
    echo "entry.sh requires PODS_COMPONENT env var to be set, could not find component match."
fi

if [ "$DEBUG_SLEEP_LOOP" == "true" ]
then
    while true
    do
        sleep 86400
    done
else
    sleep 60
    exit 1
fi