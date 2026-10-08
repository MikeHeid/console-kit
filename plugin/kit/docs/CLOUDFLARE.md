# Cloudflare onboarding

The console is never exposed directly. It listens on 127.0.0.1 only, and it is
reached through two locks that must **both** open:

1. **Cloudflare Access** at the edge: only the people your policy names can
   reach the hostname at all;
2. **the token check at the origin:** the server verifies the
   `Cf-Access-Jwt-Assertion` that Access attaches, against your team's public
   keys and your application's AUD tag. It refuses every request without one,
   loopback included.

A **Cloudflare Tunnel** carries traffic from the edge to 127.0.0.1, so no port
is opened on your machine or router.

You need a Cloudflare account with a **zone** (a domain on Cloudflare), and a
**Zero Trust** organisation (the free plan is enough). Cloudflare moves its
dashboard around; the names below are what they were when this was written,
and the thing to look for is always the same.

## 1. Team domain

In the Zero Trust dashboard, your team domain is `<team>.cloudflareaccess.com`.
It is shown in the organisation's settings (Settings, then Custom Pages or
General, depending on the dashboard version).

This is `--team-domain` for onboarding. It is **not a secret**: it is where
anyone can fetch your public signing keys, at
`https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`.

## 2. Access application

Zero Trust, then **Access**, then **Applications**, then **Add an application**,
then **Self-hosted**:

- **Application domain:** the console's hostname, e.g.
  `acme-console.example.com`. It must be in a zone on this account.
- **Policy:** Allow, with an Include rule naming who may log in: your email
  address, or an email domain or group. Keep it narrow. This is who can answer
  your agents' questions.
- **Identity providers:** the one-time PIN (email code) works with no setup.

Save, then open the application's **Overview**. The **Application Audience
(AUD) Tag** is a 64-character hex string: this is `--aud` for onboarding. It is
**not a secret** either. It names the application a token must be minted for,
and a token cannot be made from it.

## 3. Tunnel

On the machine that runs the console, with `cloudflared` installed:

```bash
cloudflared tunnel login                # once per machine: pick the zone; writes ~/.cloudflared/cert.pem
cloudflared tunnel create acme-console  # prints the tunnel's id; writes ~/.cloudflared/<id>.json
```

**Both files are secrets.** `cert.pem` can create tunnels on your account, and
`<id>.json` can run this one. Never commit them, paste them or share them. The
kit never opens either: `onboard.py tunnel` checks only that `<id>.json`
exists, and writes its path into the tunnel's own config.

```bash
python3 onboard.py tunnel --project /path/to/project --id <the id it printed>
```

That writes `~/.cloudflared/config-acme-console.yml`: this tunnel's own config,
separate from any default `config.yml` another service may own.

## 4. DNS: always pass `--config`

```bash
cloudflared tunnel --config ~/.cloudflared/config-acme-console.yml \
  route dns --overwrite-dns <tunnel id> acme-console.example.com
```

**The trap, measured on 2026-09-28:** `cloudflared tunnel route dns <name>
<host>` **without** `--config` wrote the CNAME to the tunnel in the default
`~/.cloudflared/config.yml`, not the tunnel named on the command line. The
hostname then pointed at another service. Always pass `--config`, use the
tunnel's id rather than its name, and read back the tunnel id the command
prints.

## 5. Start the tunnel and check

```bash
systemctl --user enable --now acme-console-tunnel
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4793/api/view   # 403: no token, refused at the origin
```

Then open `https://acme-console.example.com`. Access asks you to log in, then
the page loads, with the Inbox button on it.

| What you see | Likely cause |
|---|---|
| Access says you are not allowed | the policy does not include your email |
| 403 after logging in | the AUD in `.overture/console.env` is not this application's |
| 502 or 1033 from Cloudflare | the tunnel is not running (`systemctl --user status acme-console-tunnel`) |
| The hostname shows a different site | DNS was routed without `--config` (section 4) |
