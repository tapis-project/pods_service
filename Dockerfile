# Core image for pods
# Image: tapis/pods-api
#
# Multi-stage, ordered for cache: base (system + runtime deps) → optional
# devbase (adds requirements-dev.txt, i.e. jupyter) → final (copies the code).
#
# The code COPY is LAST and lives in exactly one place, so:
#   - editing service/ rebuilds only the cheap code layers; the pip installs
#     (including jupyter) stay CACHED, and
#   - the dev image is not a second copy of the build instructions.
#
# Slim runtime (default):  docker build .
# With jupyter (dev):      docker build --build-arg RUNTIME_BASE=devbase .
# `make build` picks the right one from DEV_TOOLS — DEV_TOOLS=true makes the
# api pod run `jupyter lab`, which only exists in the devbase layer.

# Which layer the final image builds on: `base` (slim) or `devbase` (+jupyter).
# Must be declared before the first FROM to be usable in one.
ARG RUNTIME_BASE=base

# Create base image
FROM python:3.12 AS base
RUN useradd tapis -u 4872
WORKDIR /home/tapis/

# set the name of the api, for use by some of the common modules.
ENV TAPIS_API=pods
ENV PYTHONPATH=.:*:pods:pods/*

## PACKAGE INITIALIZATION
COPY --chown=tapis:tapis requirements.txt /home/tapis/

RUN apt-get update && apt-get install -y
RUN apt-get install libffi-dev vim curl -y
RUN pip3 install --upgrade pip
RUN pip3 install -r /home/tapis/requirements.txt

# rabbitmqadmin download for rabbit init
RUN wget https://raw.githubusercontent.com/rabbitmq/rabbitmq-management/v3.8.9/bin/rabbitmqadmin
RUN chmod +x rabbitmqadmin

# Dev-tools layer: runtime image plus requirements-dev.txt (jupyterlab, …).
# Sits BELOW the code copy on purpose — a service/ edit must not reinstall it.
#
# Installed into its OWN venv, never the service's site-packages: jupyter-server
# requires jsonschema>=4.18, and a plain `pip install` silently upgraded past the
# jsonschema==4.17.3 pin, which broke every tapipy resource load ("cannot import
# name '_legacy_validators'") and made the API look like it had bad credentials.
# --system-site-packages so notebooks can still import the service's packages;
# anything jupyter needs at a different version lands in the venv and shadows it
# ONLY for jupyter.
FROM base AS devbase
COPY --chown=tapis:tapis requirements-dev.txt /home/tapis/
RUN python3 -m venv --system-site-packages /opt/devtools \
    && /opt/devtools/bin/pip install --quiet --upgrade pip \
    && /opt/devtools/bin/pip install -r /home/tapis/requirements-dev.txt \
    && chown -R tapis:tapis /opt/devtools \
    && ln -sf /opt/devtools/bin/jupyter /usr/local/bin/jupyter-dev

# The image everything actually runs from. RUNTIME_BASE=devbase adds jupyter.
FROM ${RUNTIME_BASE} AS final

## FILE INITIALIZATION
# For jupyter
RUN mkdir -p /home/tapis/.local && chown tapis:tapis /home/tapis/.local
# Get tapisservice.log ready for logging
RUN touch /home/tapis/tapisservice.log && chown tapis:tapis /home/tapis/tapisservice.log
# Get config.json ready for mount
RUN touch /home/tapis/config.json && chown tapis:tapis /home/tapis/config.json
# We overwrite sqlmodel package because it's buggy, but we still want the features.
#COPY SQLMODEL/main.py /usr/local/lib/python3.12/site-packages/sqlmodel/main.py
# Copy files
COPY --chown=tapis:tapis alembic /home/tapis/alembic
COPY --chown=tapis:tapis tests /home/tapis/tests
COPY --chown=tapis:tapis service /home/tapis/service
COPY --chown=tapis:tapis docs /home/tapis/docs
# Agent self-update: central serves agent/pods_agent.py (version + sha256
# advertised in checkin endpoints) so edges update themselves — no image pull.
COPY --chown=tapis:tapis agent /home/tapis/agent
COPY --chown=tapis:tapis configschema.json alembic.ini /home/tapis/
COPY --chown=tapis:tapis --chmod=777 entry.sh /home/tapis/
# Add helpful navigation through filenames at root of container
RUN touch /pods-code-in---home-tapis

# # Install Tailscale
# RUN curl -fsSL https://pkgs.tailscale.com/stable/debian/bullseye.gpg | apt-key add - \
#     && curl -fsSL https://pkgs.tailscale.com/stable/debian/bullseye.list | tee /etc/apt/sources.list.d/tailscale.list \
#     && apt-get update \
#     && apt-get install -y tailscale

# For tailscale to allow subnet router via ipv4
#RUN sysctl -w net.ipv4.ip_forward=1

# Permission finalization
RUN chown -R tapis:tapis /home/tapis

# Run everything as tapis user (uid 4872)
USER tapis

CMD ["/home/tapis/entry.sh"]

