FROM python:3.11-slim
RUN useradd -m hermes && apt-get update && apt-get install -y --no-install-recommends bash curl git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hermes_agent ./hermes_agent
RUN chown -R hermes:hermes /app
USER hermes
ENV HERMES_HOME=/home/hermes/.hermes DEPARTMENT=UNSET HERMES_PROFILE=UNSET
EXPOSE 8080
CMD ["uvicorn", "hermes_agent.main:app", "--host", "0.0.0.0", "--port", "8080"]
