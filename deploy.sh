#!/bin/sh
# Copia il codice sull'homelab e ricostruisce il container. data/ e .env restano quelli dell'homelab.
set -eu
HOST=homelab.lan
DIR=/home/simeone/tg-mail-bot
tar -c bot.py Dockerfile compose.yaml .dockerignore \
  | ssh "$HOST" "mkdir -p $DIR/data && tar -x -C $DIR && cd $DIR && docker compose up -d --build"
