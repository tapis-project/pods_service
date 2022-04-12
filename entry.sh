#!/bin/bash


if [ $api = "api" ]; then
    # dev
    cd /home/tapis/service; uvicorn api:api --reload
    # prod - https://www.uvicorn.org/deployment/
    # gunicorn uvicorn.worker stuff
    fi

while true; do sleep 86400; done
