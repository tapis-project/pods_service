# Core image for kg
# Image: kg/core

# inherit from the flaskbase iamge:
FROM tapis/flaskbase-plugins:latest
#FROM python:3.10
#RUN useradd tapis -u 4872
RUN python -m pip install -U setuptools

# set the name of the api, for use by some of the common modules.
ENV TAPIS_API kg
ENV PYTHONPATH .:*:kg:kg/*

## PACKAGE INITIALIZATION
COPY requirements.txt /home/tapis/

RUN apt-get update && apt-get install -y
RUN apt-get install libffi-dev
RUN pip3 install --upgrade pip
RUN pip3 install -r /home/tapis/requirements.txt

## DEV TOOLS
RUN pip3 install jupyterlab

## FILE INITIALIZATION
# touch config.json
RUN touch /home/tapis/config.json
# Copy files
COPY service /home/tapis/service
COPY configschema.json entry.sh /home/tapis/
RUN chmod +x /home/tapis/entry.sh

# Permission finalization
RUN chown -R tapis:tapis /home/tapis

USER tapis
WORKDIR /home/tapis/

CMD ["/home/tapis/entry.sh"]
