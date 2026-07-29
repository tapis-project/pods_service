FROM python:3.12-slim

COPY pods_agent.py /usr/local/bin/pods-agent
RUN chmod +x /usr/local/bin/pods-agent

ENV PODS_AGENT_STATE=/var/lib/pods-agent
VOLUME /var/lib/pods-agent

ENTRYPOINT ["pods-agent"]
CMD ["run"]
