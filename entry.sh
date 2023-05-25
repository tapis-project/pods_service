#!/bin/bash

# This script is the entrypoint for the Tapis Pods Service containers.
# api, health, and spawner. If any of these break, the script will sleep for 60 and exit 
# with a non-zero exit code. This will cause the container to restart in Kubernetes.
# Set DEBUG_SLEEP_LOOP to "true" to keep the container running for debugging.

if [ $PODS_COMPONENT = "api" ]; then
    # Set up stores during init.
    python3 -u /home/tapis/service/stores.py
    # Start API
    cd /home/tapis/service; uvicorn api:api --reload --host 0.0.0.0 --port 8000
    # prod - https://www.uvicorn.org/deployment/
    # gunicorn uvicorn.worker stuff
elif [ $PODS_COMPONENT = "health" ]; then
    # Start health
    python3 -u /home/tapis/service/health.py

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