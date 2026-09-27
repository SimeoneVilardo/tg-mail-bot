// Email Worker: un tubo. Passa la mail grezza all'homelab e basta.
export default {
  async email(msg, env) {
    const r = await fetch(env.HOMELAB_URL, {
      method: "POST",
      headers: { "x-secret": env.SECRET, "x-from": msg.from, "x-to": msg.to },
      body: await new Response(msg.raw).arrayBuffer(),
    }).catch(() => null);
    if (r?.status === 403) return msg.setReject(await r.text()); // bloccato: rifiuto SMTP
    if (!r?.ok) msg.setReject("Mailbox temporarily unavailable, please try again later"); // homelab giù: rimbalzo
  },
};
