FROM python:3.13-slim

WORKDIR /app

RUN useradd --create-home --uid 1000 app && mkdir data && chown app:app data

COPY bot.py ./

USER app

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["python", "bot.py"]
