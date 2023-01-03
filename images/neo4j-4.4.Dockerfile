## Neo4j typically has users add plugins via environment variables unfortunately
## once the Neo4j version is too far out of date, that mechanism breaks.
## So I create a neo4j image with plugins pre-installed so we don't break due to upstream.
FROM neo4j:4.4
# Download neosemantics plugin jar
RUN wget -P $PWD/plugins https://github.com/neo4j-labs/neosemantics/releases/download/4.4.0.3/neosemantics-4.4.0.3.jar
# Download apoc plugin jar
RUN wget -P $PWD/plugins https://github.com/neo4j-contrib/neo4j-apoc-procedures/releases/download/4.4.0.12/apoc-4.4.0.12-all.jar