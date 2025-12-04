# Core image for pods
# Image: tapis/pods-api

# Create base image
FROM python:3.10
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

## FILE INITIALIZATION
# For jupyter
RUN mkdir -p /home/tapis/.local && chown tapis:tapis /home/tapis/.local
# Get tapisservice.log ready for logging
RUN touch /home/tapis/tapisservice.log && chown tapis:tapis /home/tapis/tapisservice.log
# Get config.json ready for mount
RUN touch /home/tapis/config.json && chown tapis:tapis /home/tapis/config.json
# We overwrite sqlmodel package because it's buggy, but we still want the features.
#COPY SQLMODEL/main.py /usr/local/lib/python3.10/site-packages/sqlmodel/main.py
# Copy files
COPY --chown=tapis:tapis alembic /home/tapis/alembic
COPY --chown=tapis:tapis tests /home/tapis/tests
COPY --chown=tapis:tapis service /home/tapis/service
COPY --chown=tapis:tapis docs /home/tapis/docs
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
