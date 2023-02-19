#!/bin/bash

if [ $pods-component = "api" ]; then
    # Set up stores during init.
    python3 -u /home/tapis/service/stores.py
    # Start API
    cd /home/tapis/service; uvicorn api:api --reload --host 0.0.0.0 --port 8000
    # prod - https://www.uvicorn.org/deployment/
    # gunicorn uvicorn.worker stuff
elif [ $pods-component = "health" ]; then
    # Start health
    python3 -u /home/tapis/service/health.py

elif [ $pods-component = "spawner" ]; then
    # Start spawner
    python3 -u /home/tapis/service/spawner.py
else
    echo "entry.sh requires pods-component env var to be set, could not find component match."
fi

while true; do sleep 86400; done