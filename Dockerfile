FROM python:3.12-slim

WORKDIR /app
COPY . /app

ENV HOST=0.0.0.0
ENV PORT=8765
ENV SQL_WRONGBOOK_DB=/app/data/sql_review.db

VOLUME ["/app/data"]
EXPOSE 8765

CMD ["python", "scripts/server.py"]
