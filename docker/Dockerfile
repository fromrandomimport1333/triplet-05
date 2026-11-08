# One image for every Python service of the Twin team (Twin --fake, Guardian and Brain stand-ins, timeline,
# scenarios). SUMO is not installed: in Docker the Twin runs in --fake mode. Built by docker-compose.yml.
FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY twin/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY twin/ twin/
COPY timeline/ timeline/
COPY scenarios/ scenarios/
COPY docs/schemas/ docs/schemas/
RUN mkdir -p logs
