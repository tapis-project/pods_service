#!/bin/bash


if [ $api = "api" ]; then
    # dev
    # Set up stores.
    python3 -u /home/tapis/service/stores.py 
    cd /home/tapis/service; uvicorn api:api --reload --host 0.0.0.0
    # prod - https://www.uvicorn.org/deployment/
    # gunicorn uvicorn.worker stuff
    fi

while true; do sleep 86400; done
