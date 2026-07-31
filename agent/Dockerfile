# 3.14+ gives stdlib compression.zstd — the agent auto-upgrades its log
# shipping from gzip to zstd when central advertises zstd support.
FROM python:3.14-slim

COPY pods_agent.py /usr/local/bin/pods-agent
RUN chmod +x /usr/local/bin/pods-agent

ENV PODS_AGENT_STATE=/var/lib/pods-agent
VOLUME /var/lib/pods-agent

ENTRYPOINT ["pods-agent"]
CMD ["run"]
