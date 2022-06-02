# Core image for pods
# Image: tapis/pods-api

# Create base image
FROM python:3.10
RUN useradd tapis -u 4872
WORKDIR /home/tapis/

# set the name of the api, for use by some of the common modules.
ENV TAPIS_API pods
ENV PYTHONPATH .:*:pods:pods/*

## PACKAGE INITIALIZATION
COPY requirements.txt /home/tapis/

RUN apt-get update && apt-get install -y
RUN apt-get install libffi-dev vim curl -y
RUN pip3 install --upgrade pip
RUN pip3 install -r /home/tapis/requirements.txt

# rabbitmqadmin download for rabbit init
RUN wget https://raw.githubusercontent.com/rabbitmq/rabbitmq-management/v3.8.9/bin/rabbitmqadmin
RUN chmod +x rabbitmqadmin

## FILE INITIALIZATION
# touch config.json
RUN touch /home/tapis/config.json
# Copy files
COPY service /home/tapis/service
COPY alembic /home/tapis/alembic
COPY configschema.json entry.sh alembic.ini /home/tapis/
RUN chmod +x /home/tapis/entry.sh
# We overwrite sqlmodel package because it's buggy, but we still want the features.
COPY SQLMODEL/main.py /usr/local/lib/python3.10/site-packages/sqlmodel/main.py

# Permission finalization
#RUN chown -R tapis:tapis /home/tapis


CMD ["/home/tapis/entry.sh"]
