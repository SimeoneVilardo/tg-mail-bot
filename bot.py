#!/usr/bin/env python3
"""Mail di *@DOMAIN su Telegram, solo libreria standard.

Ricezione: Cloudflare Email Routing -> Email Worker -> POST /inbound (qui).
Invio: bozza su Telegram -> 📤 Invia -> Resend.
"""
import base64
import hmac
import html
import json
import os
import re
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from email import policy
from email.parser import BytesParser
from email.utils import formataddr, getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ["TELEGRAM_TOKEN"]
RESEND_KEY = os.environ["RESEND_API_KEY"]
SECRET = os.environ["INBOUND_SECRET"]
DOMAIN = os.environ.get("MAIL_DOMAIN", "simeonevilardo.com").lower()
FROM_NAME = os.environ.get("FROM_NAME", "Simeone Vilardo")
DEFAULT_ALIAS = os.environ.get("DEFAULT_ALIAS", "simeone")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "")
TG = f"https://api.telegram.org/bot{TOKEN}/"
SEP = "──────────────"
MAX_TG_FILE = 20 * 1024 * 1024  # limite di download della Bot API

HELP = f"""📬 <b>Mail @{DOMAIN}</b>
Qualsiasi indirizzo <code>*@{DOMAIN}</code> arriva qui. L'hashtag è l'alias che ha ricevuto la mail: toccalo per vedere tutte le sue mail.

↩️ <b>Rispondere</b>: fai swipe-reply sulla mail e scrivi. Puoi mandare anche file, foto o album. Arriva una bozza, che parte solo quando tocchi 📤 Invia.
📎 <b>Allegare</b>: rispondi alla bozza con file o altro testo.
✉️ <b>Nuova mail</b>:
<code>/new mario@esempio.it da:lavoro
Oggetto
Testo…</code>
<code>da:</code> è facoltativo, di default è {DEFAULT_ALIAS}@{DOMAIN}. Per più destinatari separali con la virgola.

🚫 <b>Blocchi</b>: 🚫 Mittente, 🚫 Dominio e 🔇 Alias sotto ogni mail rifiutano le mail successive. Ritocca per sbloccare, /blocked per l'elenco.
👻 = il tuo indirizzo non era in A/Cc (BCC o mailing list) · ⚠️ = DMARC fallito. In entrambi i casi la notifica arriva senza suono."""

# ---------------------------------------------------------------- database

db = sqlite3.connect(os.environ.get("DB_PATH", "data/mail.db"), check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
lock = threading.Lock()
db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS mails (
  id INTEGER PRIMARY KEY, received_at TEXT DEFAULT CURRENT_TIMESTAMP,
  alias TEXT, env_from TEXT, sender TEXT, subject TEXT, raw BLOB,
  tg_msg_id INTEGER  -- NULL finché la notifica non è partita
);
CREATE TABLE IF NOT EXISTS blocks (id INTEGER PRIMARY KEY, kind TEXT, value TEXT, UNIQUE(kind, value));
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY, mail_id INTEGER, sender TEXT, to_addrs TEXT, cc_all TEXT DEFAULT '[]',
  reply_all INTEGER DEFAULT 0, subject TEXT, body TEXT DEFAULT '', quote TEXT DEFAULT '',
  in_reply_to TEXT, refs TEXT, files TEXT DEFAULT '[]', status TEXT DEFAULT 'draft', tg_msg_id INTEGER
);
CREATE TABLE IF NOT EXISTS tg_map (msg_id INTEGER PRIMARY KEY, mail_id INTEGER, draft_id INTEGER);
""")


def q(sql, *args):
    with lock:
        return db.execute(sql, args).fetchall()


def q1(sql, *args):
    rows = q(sql, *args)
    return rows[0] if rows else None


def ex(sql, *args):
    with lock:
        return db.execute(sql, args).lastrowid


def owner():
    if OWNER_CHAT_ID:
        return int(OWNER_CHAT_ID)
    r = q1("SELECT value FROM settings WHERE key = 'owner'")
    return int(r["value"]) if r else None


# ---------------------------------------------------------------- telegram

def multipart(fields, files):
    b = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        v = v if isinstance(v, str) else json.dumps(v)
        out.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (name, data) in files.items():
        name = re.sub(r'["\r\n]', "_", name)
        out += [f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n".encode(), data, b"\r\n"]
    out.append(f"--{b}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={b}"


def tg(method, files=None, **params):
    params = {k: v for k, v in params.items() if v is not None}
    body, ctype = multipart(params, files) if files else (json.dumps(params).encode(), "application/json")
    for _ in range(3):
        try:
            req = urllib.request.Request(TG + method, body, {"Content-Type": ctype})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)["result"]
        except urllib.error.HTTPError as e:
            err = json.load(e)
            if e.code != 429:
                raise RuntimeError(f"{method}: {err.get('description')}") from None
            time.sleep(err.get("parameters", {}).get("retry_after", 5))
    raise RuntimeError(f"{method}: troppe richieste")


def reply_to(msg_id):
    return {"message_id": msg_id, "allow_sending_without_reply": True} if msg_id else None


def send(text, kb=None, reply=None, silent=False, chat=None):
    return tg("sendMessage", chat_id=chat or owner(), text=text, parse_mode="HTML", reply_markup=kb,
              reply_parameters=reply_to(reply), disable_notification=silent or None,
              link_preview_options={"is_disabled": True})


def edit(msg_id, text, kb=None):
    try:
        tg("editMessageText", chat_id=owner(), message_id=msg_id, text=text, parse_mode="HTML",
           reply_markup=kb, link_preview_options={"is_disabled": True})
    except RuntimeError as e:
        if "not modified" not in str(e):
            raise


def send_doc(name, data, reply, mail_id=None):
    m = tg("sendDocument", files={"document": (name, data)}, chat_id=owner(),
           reply_parameters=reply_to(reply), disable_notification=True)
    ex("INSERT OR REPLACE INTO tg_map (msg_id, mail_id) VALUES (?, ?)", m["message_id"], mail_id)


def download(file_id):
    path = tg("getFile", file_id=file_id)["file_path"]
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=120) as r:
        return r.read()


def btn(text, data):
    return {"text": text, "callback_data": data}


def react(msg):
    try:
        tg("setMessageReaction", chat_id=owner(), message_id=msg["message_id"],
           reaction=[{"type": "emoji", "emoji": "👍"}])
    except Exception:
        pass


# ---------------------------------------------------------------- lettura mail

def parse(raw, headersonly=False):
    return BytesParser(policy=policy.default).parsebytes(raw, headersonly=headersonly)


def hdr(m, name):
    try:
        return str(m.get(name) or "").strip()
    except Exception:  # header malformato
        return ""


def addrs(m, name):
    try:
        h = m.get(name)
        if h is None:
            return []
        return [(a.display_name, a.addr_spec.lower()) for a in h.addresses if "@" in a.addr_spec]
    except Exception:
        return [(n, a.lower()) for n, a in getaddresses([hdr(m, name)]) if "@" in a]


def content(part):
    try:
        return part.get_content()
    except Exception:  # charset sconosciuto o simili
        return (part.get_payload(decode=True) or b"").decode("utf-8", "replace")


# I link delle mail HTML viaggiano nel testo come \x01url\x02testo\x03: rich() li rende cliccabili, plain() li toglie.
LINK = re.compile(r"\x01([^\x02]*)\x02(.*?)\x03", re.S)


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote", "hr"}
    SKIP = {"style", "script", "title"}

    def __init__(self):
        super().__init__()
        self.out, self.skip, self.link = [], 0, False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "a" and not self.link and (a.get("href") or "").startswith(("http://", "https://", "mailto:")):
            self.out.append(f"\x01{a['href'].strip()}\x02")
            self.link = True
        elif tag == "img" and self.link:
            self.out.append(a.get("alt") or "")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "a" and self.link:
            self.out.append("\x03")
            self.link = False
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html2text(s):
    p = _Text()
    p.feed(s)
    p.close()
    if p.link:
        p.out.append("\x03")
    t = re.sub(r"[ \t\r\f\v ­͏​-‍⁠﻿]+", " ", "".join(p.out))
    t = re.sub(r" *\n *", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def body_part(m, kind):
    try:
        return m.get_body(preferencelist=(kind,))
    except Exception:
        return None


def body_text(m):
    for kind in ("plain", "html"):
        part = body_part(m, kind)
        if part is not None:
            t = content(part)
            t = (html2text(t) if kind == "html" else t).strip()
            if t:
                return t
    return ""


def plain(t):
    return re.sub(r"\x01[^\x02]*\x02|\x03", "", t)


def rich(t, links=True):
    """Testo -> HTML Telegram, con i link come <a>. Il testo di un link vuoto (es. un bottone-immagine) diventa 🔗."""
    def a(mt):
        inner = mt.group(2)
        core = inner.strip() or "🔗"
        lead, trail = inner[:len(inner) - len(inner.lstrip())], inner[len(inner.rstrip()):]
        return f'{lead}<a href="{mt.group(1).replace(chr(34), "&quot;")}">{core}</a>{trail}'
    return LINK.sub(a, esc(t)) if links else esc(plain(t))


def cut(t, n):
    """Tronca a n caratteri visibili (gli url dei link non contano per Telegram)."""
    out = []
    for tok in re.split(r"(\x01[^\x02]*\x02)", t):
        if tok.startswith("\x01"):
            out.append(tok)
        elif len(tok) > n:
            out.append(tok[:n])
            s = "".join(out)
            return s + ("\x03" if s.rfind("\x01") > s.rfind("\x03") else "") + "…"
        else:
            out.append(tok)
            n -= len(tok)
    return t


def attachments(m):
    out = []
    for p in m.walk():
        if p.is_multipart():
            continue
        name, disp = p.get_filename(), p.get_content_disposition()
        if disp == "attachment" or (name and disp != "inline"):
            out.append((name or f"allegato.{p.get_content_subtype()}", p.get_payload(decode=True) or b""))
    return out


def auth(m):
    """Esiti SPF/DKIM/DMARC scritti da Cloudflare (gli header di altri server vengono ignorati)."""
    res = {}
    for name in ("Authentication-Results", "ARC-Authentication-Results"):
        for v in m.get_all(name) or []:
            v = str(v)
            if "cloudflare" not in re.split(r"\b(?:spf|dkim|dmarc|arc)=", v)[0].lower():
                continue
            for k in ("spf", "dkim", "dmarc"):
                found = [x.lower() for x in re.findall(rf"(?<![\w.]){k}=(\w+)", v)]  # es. SPF di HELO e di MAIL FROM
                if found and k not in res:
                    res[k] = "pass" if "pass" in found else next(
                        (x for x in found if x in ("fail", "softfail", "permerror")), found[0])
    return res


def icon(result):
    if result == "pass":
        return "✅"
    if result in ("fail", "softfail", "permerror"):
        return "❌"
    return "➖" if result else "❔"


def esc(s):
    return html.escape(s or "", quote=False)


def show(lst, cut=300):
    s = ", ".join(f"{n} <{a}>" if n else a for n, a in lst)
    return esc(s[:cut] + "…" if len(s) > cut else s)


def safe_name(s, ext):
    return (re.sub(r"[^\w\- ]+", "", s).strip()[:60] or "mail") + ext


# ---------------------------------------------------------------- notifiche

def mail_kb(r):
    blocked = lambda kind, value: q1("SELECT 1 FROM blocks WHERE kind = ? AND value = ?", kind, value)
    i = r["id"]
    return {"inline_keyboard": [
        [btn("✅ Sblocca mittente" if blocked("sender", r["sender"]) else "🚫 Mittente", f"b:sender:{i}"),
         btn("✅ Sblocca dominio" if blocked("domain", r["sender"].rsplit("@", 1)[-1]) else "🚫 Dominio", f"b:domain:{i}"),
         btn("🔊 Riattiva alias" if blocked("alias", r["alias"]) else "🔇 Alias", f"b:alias:{i}")],
        [btn("📄 Originale", f"o:{i}")],
    ]}


def notify(r, links=True):
    m = parse(r["raw"])
    alias = r["alias"]
    frm, to, cc, rt = addrs(m, "From"), addrs(m, "To"), addrs(m, "Cc"), addrs(m, "Reply-To")
    a = auth(m)
    ghost = alias not in {x for _, x in to + cc}
    spoof = a.get("dmarc") == "fail"
    atts = attachments(m)

    lines = [f"📩 #{re.sub(r'[^0-9A-Za-z_]', '_', alias.split('@')[0])} · {esc(alias)}"]
    if ghost:
        lines.append("👻 <b>BCC/lista</b>: il tuo indirizzo non è in A/Cc")
    if spoof:
        lines.append("⚠️ <b>Possibile spoofing</b>: DMARC fallito")
    lines.append(f"<b>Da:</b> {show(frm) or esc(r['env_from']) or '—'}")
    lines.append(f"<b>A:</b> {show(to) or '—'}")
    if cc:
        lines.append(f"<b>Cc:</b> {show(cc)}")
    if rt and rt != frm:
        lines.append(f"<b>Rispondi a:</b> {show(rt)}")
    lines.append(f"<b>Oggetto:</b> {esc(hdr(m, 'Subject')) or '<i>(nessuno)</i>'}")
    lines.append("🔐 " + " ".join(f"{k.upper()} {icon(a.get(k))}" for k in ("spf", "dkim", "dmarc"))
                 + (f"   📎 {len(atts)}" if atts else ""))
    lines.append(SEP)
    head = "\n".join(lines)

    body = body_text(m)
    short = cut(body, 3900 - len(head))
    if short != body:
        short += "\n(continua in 📄 Originale)"
    text = head + (f"\n<blockquote expandable>{rich(short, links)}</blockquote>" if body else "\n<i>(nessun testo)</i>")

    card = send(text, mail_kb(r), silent=ghost or spoof)
    ex("UPDATE mails SET tg_msg_id = ? WHERE id = ?", card["message_id"], r["id"])
    ex("INSERT OR REPLACE INTO tg_map (msg_id, mail_id) VALUES (?, ?)", card["message_id"], r["id"])
    for name, data in atts:
        try:
            send_doc(name, data, card["message_id"], r["id"])
        except Exception as e:
            send(f"⚠️ Allegato <code>{esc(name)}</code> non inoltrato: {esc(str(e))}", reply=card["message_id"], silent=True)


wake = threading.Event()


def notifier():
    while True:
        wake.wait(60)  # il timeout fa da retry se Telegram era giù
        wake.clear()
        if not owner():
            continue
        for r in q("SELECT * FROM mails WHERE tg_msg_id IS NULL ORDER BY id"):
            try:
                try:
                    notify(r)
                except OSError:
                    raise  # rete: si riprova al prossimo giro
                except Exception:
                    traceback.print_exc()
                    notify(r, links=False)  # Telegram ha rifiutato l'HTML: riprova senza link
            except Exception:
                traceback.print_exc()
                break


# ---------------------------------------------------------------- ricezione (dal Worker)

def block_reason(alias, senders):
    if q1("SELECT 1 FROM blocks WHERE kind = 'alias' AND value = ?", alias):
        return "Address does not exist"
    for s in senders:
        dom = s.rsplit("@", 1)[-1]
        doms = [".".join(dom.split(".")[i:]) for i in range(dom.count(".") + 1)]  # anche i sottodomini
        if q1("SELECT 1 FROM blocks WHERE kind = 'sender' AND value = ?", s) or q1(
                f"SELECT 1 FROM blocks WHERE kind = 'domain' AND value IN ({','.join('?' * len(doms))})", *doms):
            return "Sender blocked"
    return None


class Inbound(BaseHTTPRequestHandler):
    def answer(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.answer(200, "ok")

    def do_POST(self):
        if self.path != "/inbound" or not hmac.compare_digest(self.headers.get("x-secret", ""), SECRET):
            return self.answer(401, "unauthorized")
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        alias = self.headers.get("x-to", "").strip().lower()
        env_from = self.headers.get("x-from", "").strip().lower()
        if not alias.endswith("@" + DOMAIN):
            return self.answer(403, "Relay access denied")
        m = parse(raw, headersonly=True)
        sender = (addrs(m, "From") or [("", env_from)])[0][1]
        why = block_reason(alias, {s for s in (sender, env_from) if s})
        print(f"inbound {env_from} -> {alias}: {why or 'ok'}", flush=True)
        if why:
            return self.answer(403, why)
        ex("INSERT INTO mails (alias, env_from, sender, subject, raw) VALUES (?, ?, ?, ?, ?)",
           alias, env_from, sender, hdr(m, "Subject"), raw)
        self.answer(200, "ok")
        wake.set()


# ---------------------------------------------------------------- bozze e invio

def draft_view(d):
    files = json.loads(d["files"])
    cc = json.loads(d["cc_all"]) if d["reply_all"] else []
    body = d["body"][:3000] + ("…" if len(d["body"]) > 3000 else "")
    lines = [f"✉️ <b>Bozza</b>  {esc(d['sender'])} → {esc(', '.join(json.loads(d['to_addrs'])))}"]
    if cc:
        lines.append(f"<b>Cc:</b> {esc(', '.join(cc))}")
    lines.append(f"<b>{esc(d['subject']) or '<i>(nessun oggetto)</i>'}</b>" + (f" · 📎 {len(files)}" if files else ""))
    lines.append(SEP)
    lines.append(esc(body) or "<i>(nessun testo)</i>")
    if files:
        lines.append("\n📎 " + ", ".join(esc(f["name"]) for f in files))
    if d["quote"]:
        lines.append("<i>+ testo originale citato</i>")
    row = [btn("📤 Invia", f"s:{d['id']}")]
    if json.loads(d["cc_all"]):
        row.append(btn("👤 Solo mittente" if d["reply_all"] else "👥 Rispondi a tutti", f"a:{d['id']}"))
    row.append(btn("🗑", f"d:{d['id']}"))
    return "\n".join(lines), {"inline_keyboard": [row]}


def render_draft(did, reply=None):
    d = q1("SELECT * FROM drafts WHERE id = ?", did)
    text, kb = draft_view(d)
    if d["tg_msg_id"]:
        return edit(d["tg_msg_id"], text, kb)
    m = send(text, kb, reply=reply)
    ex("UPDATE drafts SET tg_msg_id = ? WHERE id = ?", m["message_id"], did)
    ex("INSERT OR REPLACE INTO tg_map (msg_id, draft_id) VALUES (?, ?)", m["message_id"], did)


def tg_files(msg):
    n = msg["message_id"]
    if "photo" in msg:
        f, name = msg["photo"][-1], f"foto_{n}.jpg"
    else:
        for kind, ext in (("document", "bin"), ("video", "mp4"), ("audio", "mp3"), ("voice", "ogg"), ("video_note", "mp4")):
            if kind in msg:
                f, name = msg[kind], msg[kind].get("file_name") or f"{kind}_{n}.{ext}"
                break
        else:
            return []
    if f.get("file_size", 0) > MAX_TG_FILE:
        raise RuntimeError(f"{name} supera 20 MB, il limite di download della Bot API")
    return [{"id": f["file_id"], "name": name}]


def add_to_draft(did, text, files, msg):
    d = q1("SELECT * FROM drafts WHERE id = ?", did)
    if d["status"] != "draft":
        return send("Questa bozza è già stata inviata o scartata.", reply=msg["message_id"])
    body = "\n\n".join(x for x in (d["body"], text.strip()) if x)
    ex("UPDATE drafts SET body = ?, files = ? WHERE id = ?", body, json.dumps(json.loads(d["files"]) + files), did)
    render_draft(did)
    react(msg)


def reply_draft(mail_id, text, files, msg):
    r = q1("SELECT * FROM mails WHERE id = ?", mail_id)
    m = parse(r["raw"])
    to = [a for _, a in addrs(m, "Reply-To") or addrs(m, "From")] or [r["env_from"]]
    cc_all = []
    for _, a in addrs(m, "To") + addrs(m, "Cc"):
        if not a.endswith("@" + DOMAIN) and a not in to and a not in cc_all:
            cc_all.append(a)
    subject = hdr(m, "Subject")
    if not re.match(r"(?i)\s*re\s*:", subject):
        subject = "Re: " + subject
    mid = hdr(m, "Message-ID")
    try:
        date = parsedate_to_datetime(hdr(m, "Date")).strftime("%d/%m/%Y %H:%M")
    except Exception:
        date = hdr(m, "Date")
    quoted = "\n".join("> " + line for line in plain(body_text(m))[:20000].splitlines())
    quote = f"\n\nIl {date}, {hdr(m, 'From') or r['env_from']} ha scritto:\n{quoted}"
    did = ex("INSERT INTO drafts (mail_id, sender, to_addrs, cc_all, subject, body, quote, in_reply_to, refs, files) "
             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", mail_id, r["alias"], json.dumps(to), json.dumps(cc_all),
             subject, text.strip(), quote, mid, f"{hdr(m, 'References')} {mid}".strip(), json.dumps(files))
    render_draft(did, reply=msg["message_id"])
    return did


def new_draft(text, files, msg):
    lines = text.split("\n")
    sender, to = DEFAULT_ALIAS, []
    for arg in lines[0].split()[1:]:
        if arg.lower().startswith(("da:", "from:")):
            sender = arg.split(":", 1)[1]
        else:
            to += [x for x in arg.split(",") if x]
    sender = sender.lower() if "@" in sender else f"{sender.lower()}@{DOMAIN}"
    if not sender.endswith("@" + DOMAIN):
        send(f"⚠️ Si può inviare solo da indirizzi @{DOMAIN}.")
        return None
    if not to or any(not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", x) for x in to):
        send("Uso:\n<code>/new mario@esempio.it da:lavoro\nOggetto\nTesto…</code>")
        return None
    did = ex("INSERT INTO drafts (sender, to_addrs, subject, body, files) VALUES (?, ?, ?, ?, ?)", sender,
             json.dumps(to), lines[1].strip() if len(lines) > 1 else "", "\n".join(lines[2:]).strip(), json.dumps(files))
    render_draft(did, reply=msg["message_id"])
    return did


def resend(payload):
    req = urllib.request.Request("https://api.resend.com/emails", json.dumps(payload).encode(), {
        "Authorization": f"Bearer {RESEND_KEY}", "Content-Type": "application/json", "User-Agent": "tg-mail-bot"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)["id"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Resend {e.code}: {e.read().decode(errors='replace')[:300]}") from None


def send_draft(did):
    d = q1("SELECT * FROM drafts WHERE id = ?", did)
    if d["status"] != "draft":
        return "Già inviata o scartata"
    to, files = json.loads(d["to_addrs"]), json.loads(d["files"])
    cc = json.loads(d["cc_all"]) if d["reply_all"] else []
    payload = {"from": formataddr((FROM_NAME, d["sender"])), "to": to, "subject": d["subject"],
               "text": d["body"] + d["quote"]}
    if cc:
        payload["cc"] = cc
    if d["in_reply_to"]:
        payload["headers"] = {"In-Reply-To": d["in_reply_to"], "References": d["refs"]}
    if files:
        payload["attachments"] = [{"filename": f["name"], "content": base64.b64encode(download(f["id"])).decode()}
                                  for f in files]
    resend(payload)
    ex("UPDATE drafts SET status = 'sent' WHERE id = ?", did)
    rcpt = ", ".join(to + cc)
    edit(d["tg_msg_id"], f"✅ <b>Inviata</b>  {esc(d['sender'])} → {esc(rcpt)}\n<b>{esc(d['subject'])}</b>"
                         + (f" · 📎 {len(files)}" if files else "") + f"\n{SEP}\n{esc(d['body'][:3000])}")
    return "📤 Inviata"


# ---------------------------------------------------------------- comandi e bottoni

KIND = {"sender": "mittente", "domain": "dominio", "alias": "alias"}


def blocks_view():
    rows = q("SELECT * FROM blocks ORDER BY kind, value")
    if not rows:
        return "Nessun blocco attivo.", None
    return "🚫 <b>Blocchi attivi</b>: tocca per sbloccare", {"inline_keyboard": [
        [btn(f"✅ {KIND[r['kind']]}: {r['value']}", f"u:{r['id']}")] for r in rows]}


def toggle_block(kind, mail_id, card_id):
    r = q1("SELECT id, alias, sender FROM mails WHERE id = ?", mail_id)
    value = {"sender": r["sender"], "domain": r["sender"].rsplit("@", 1)[-1], "alias": r["alias"]}[kind]
    if q1("SELECT 1 FROM blocks WHERE kind = ? AND value = ?", kind, value):
        ex("DELETE FROM blocks WHERE kind = ? AND value = ?", kind, value)
        note = f"✅ Sbloccato {KIND[kind]} {value}"
    else:
        ex("INSERT INTO blocks (kind, value) VALUES (?, ?)", kind, value)
        note = f"🚫 Bloccato {KIND[kind]} {value}"
    tg("editMessageReplyMarkup", chat_id=owner(), message_id=card_id, reply_markup=mail_kb(r))
    return note


def original(mail_id, card_id):
    r = q1("SELECT * FROM mails WHERE id = ?", mail_id)
    m = parse(r["raw"])
    send_doc(safe_name(r["subject"], ".eml"), r["raw"], card_id, mail_id)
    part = body_part(m, "html")
    if part is not None:
        send_doc(safe_name(r["subject"], ".html"), ('<meta charset="utf-8">' + content(part)).encode(), card_id, mail_id)
    else:
        send_doc(safe_name(r["subject"], ".txt"), plain(body_text(m)).encode(), card_id, mail_id)


def on_callback(cq):
    msg = cq.get("message") or {}
    if msg.get("chat", {}).get("id") != owner():
        return tg("answerCallbackQuery", callback_query_id=cq["id"], text="⛔")
    action, *args = cq["data"].split(":")
    note, alert = None, False
    try:
        if action == "b":
            note = toggle_block(args[0], int(args[1]), msg["message_id"])
        elif action == "o":
            original(int(args[0]), msg["message_id"])
        elif action == "s":
            note = send_draft(int(args[0]))
        elif action == "a":
            ex("UPDATE drafts SET reply_all = 1 - reply_all WHERE id = ? AND status = 'draft'", int(args[0]))
            render_draft(int(args[0]))
        elif action == "d":
            ex("UPDATE drafts SET status = 'discarded' WHERE id = ? AND status = 'draft'", int(args[0]))
            tg("deleteMessage", chat_id=owner(), message_id=msg["message_id"])
            note = "🗑 Bozza scartata"
        elif action == "u":
            ex("DELETE FROM blocks WHERE id = ?", int(args[0]))
            text, kb = blocks_view()
            edit(msg["message_id"], text, kb)
            note = "✅ Sbloccato"
    except Exception as e:
        traceback.print_exc()
        note, alert = f"⚠️ {e}"[:190], True
    tg("answerCallbackQuery", callback_query_id=cq["id"], text=note, show_alert=alert or None)


albums = {}  # media_group_id -> bozza, per gli album (solo il primo messaggio ha la didascalia)


def on_message(msg):
    text = msg.get("text") or msg.get("caption") or ""
    cmd = text.split(maxsplit=1)[0].split("@")[0].lower() if text.startswith("/") else ""
    files = tg_files(msg)
    group = msg.get("media_group_id")
    rep = msg.get("reply_to_message")
    target = rep and q1("SELECT * FROM tg_map WHERE msg_id = ?", rep["message_id"])
    if group in albums:
        return add_to_draft(albums[group], text, files, msg)
    if cmd in ("/start", "/help"):
        return send(HELP)
    if cmd == "/blocked":
        return send(*blocks_view())
    if cmd == "/new":
        did = new_draft(text, files, msg)
    elif target and target["draft_id"]:
        did = target["draft_id"]
        add_to_draft(did, text, files, msg)
    elif target and target["mail_id"]:
        if not text.strip() and not files:
            return send("Scrivi del testo o manda un file per rispondere.")
        did = reply_draft(target["mail_id"], text, files, msg)
    else:
        return send("Per rispondere a una mail fai swipe-reply sul suo messaggio. /help per il resto.")
    if group and did:
        albums[group] = did


def handle(u):
    if "callback_query" in u:
        return on_callback(u["callback_query"])
    msg = u.get("message")
    if not msg:
        return
    chat = msg["chat"]["id"]
    if owner() is None and msg["chat"]["type"] == "private" and (msg.get("text") or "").startswith("/start"):
        ex("INSERT INTO settings (key, value) VALUES ('owner', ?)", str(chat))
        print(f"owner: chat {chat} @{msg.get('from', {}).get('username')}", flush=True)
        send(HELP)
        wake.set()
        return
    if chat != owner():
        print(f"chat {chat} non autorizzata", flush=True)
        return tg("sendMessage", chat_id=chat, text="⛔ Bot privato.")
    try:
        on_message(msg)
    except Exception as e:
        traceback.print_exc()
        send(f"⚠️ {esc(str(e))}", reply=msg["message_id"])


def poll():
    offset = 0
    while True:
        try:
            updates = tg("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "callback_query"])
        except Exception as e:
            print(f"getUpdates: {e}", flush=True)
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                handle(u)
            except Exception:
                traceback.print_exc()


def main():
    threading.Thread(target=ThreadingHTTPServer(("", 8080), Inbound).serve_forever, daemon=True).start()
    threading.Thread(target=notifier, daemon=True).start()
    try:
        tg("deleteWebhook")
        tg("setMyCommands", commands=[{"command": "new", "description": "Nuova mail"},
                                      {"command": "blocked", "description": "Blocchi attivi"},
                                      {"command": "help", "description": "Come funziona"}])
    except Exception as e:
        print(f"setup telegram: {e}", flush=True)
    print(f"tg-mail-bot pronto, owner={owner()}", flush=True)
    wake.set()
    poll()


if __name__ == "__main__":
    main()
