FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY huntbot ./huntbot
RUN pip install --no-cache-dir .
ENV HUNTBOT_DB=/data/huntbot.db
VOLUME /data
EXPOSE 8787
CMD ["huntbot", "serve", "--host", "0.0.0.0"]
