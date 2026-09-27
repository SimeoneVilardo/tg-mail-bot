# tg-mail-bot

Tutte le mail `*@simeonevilardo.com` su Telegram (@SimeoneMailBot), con risposta e allegati.

```
mittente ─SMTP─▶ Cloudflare Email Routing (MX, catch-all)
                    ▼
              Email Worker (worker/): passa la mail grezza, niente logica
                    │ HTTPS + x-secret
                    ▼
     tg-mail-bot.simeonevilardo.com ──▶ cloudflared (homelab)
                    ▼
     container tg-mail-bot (bot.py, Python stdlib + SQLite) ◀─long polling─▶ Telegram
                    │ HTTPS
                    ▼
                 Resend ──▶ destinatario
```

- **Blocchi** (mittente, dominio, alias): l'homelab risponde 403 e il Worker rifiuta la mail durante la sessione SMTP.
- **Homelab giù**: il Worker rifiuta la mail con un errore, quindi il mittente riceve un rimbalzo.
- **Niente open relay**: non c'è nessuna porta SMTP. Si invia solo da bozze del proprietario del bot, e solo da indirizzi `@simeonevilardo.com`.
- Proprietario: il primo `/start` in privato, salvato nel DB. In alternativa si imposta `OWNER_CHAT_ID`.

## Deploy

Homelab: `/home/simeone/tg-mail-bot`, rete `cloudflare-tunnel`, nessuna porta pubblicata. Il `.env` sta lì (vedi `.env.example`).

```sh
./deploy.sh                                   # bot: copia il codice e fa docker compose up -d --build
cd worker && npx wrangler deploy              # Worker
npx wrangler secret put SECRET                # = INBOUND_SECRET del .env
```

Email Routing: regola catch-all con azione "Send to Worker" → `tg-mail-bot`.

## Limiti

- Resend free: 100 mail al giorno, 3.000 al mese.
- Allegati in uscita: al massimo 20 MB ciascuno, il limite di download della Bot API.
- Log: `ssh homelab.lan "cd /home/simeone/tg-mail-bot && docker compose logs --tail 100"`.
