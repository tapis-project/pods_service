#!/bin/bash


if [ $api = "reg" ]; then
    # dev
    cd /home/tapis/service; uvicorn crud:app --reload
    # prod - https://www.uvicorn.org/deployment/
    # gunicorn uvicorn.worker stuff
    fi

while true; do sleep 86400; done
