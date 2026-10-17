# Harness delegate for this PC, plus the docker CLI so pipeline steps can start the
# sentinel/deployer container. The delegate connects OUT to Harness; nothing is exposed.
FROM docker:27.5.1-cli AS docker-cli

FROM harness/delegate:26.09.90106
USER root
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
