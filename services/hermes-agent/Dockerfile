FROM python:3.11-slim
RUN useradd -m hermes && apt-get update && apt-get install -y --no-install-recommends bash curl git ca-certificates && rm -rf /var/lib/apt/lists/*
USER hermes
WORKDIR /home/hermes
ENV HERMES_HOME=/home/hermes/.hermes DEPARTMENT=UNSET HERMES_PROFILE=UNSET
COPY bootstrap-agent.sh /usr/local/bin/bootstrap-agent.sh
CMD ["bash","/usr/local/bin/bootstrap-agent.sh"]
