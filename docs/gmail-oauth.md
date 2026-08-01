# Gmail OAuth: expiry and renewal

Card Dues uses Google OAuth with scope `gmail.readonly` for statement PDFs and
(separately) expense-alert emails. Files live under `~/.mydues/` (or
`$MYDUES_HOME`):

| File | Role |
|------|------|
| `credentials.json` | OAuth client from Google Cloud Console (or path from `MYDUES_CREDENTIALS`) |
| `token.json` | Access + refresh tokens after **Connect Gmail** |

Full first-time setup (Cloud Console, Desktop vs Web client, redirect URI) is in
[README.md](../README.md#connect-gmail).

## What expires when

| Thing | Typical lifetime |
|--------|------------------|
| Access token | ~1 hour (auto-refreshed by the app) |
| Refresh token (consent screen **Testing**) | ~7 days |
| Refresh token (consent screen **In production**) | Until revoked / unused long-term / account security change |
| `credentials.json` (client ID/secret) | No fixed day count; only if you delete/rotate the client |

You only need to reconnect when the **refresh token** dies or is revoked.

## Renew (refresh token expired)

1. Start the dashboard: `.venv/bin/python -m mydues serve`
2. Click **Disconnect Gmail** (clears `token.json`)
3. Optional: revoke the app at https://myaccount.google.com/permissions
4. Click **Connect Gmail** and finish consent
5. Confirm **Fetch from Gmail** (Credit cards) and/or **Fetch expense alerts**
   (Expenses) work

Or from a terminal (desktop OAuth client only):

```bash
.venv/bin/python -m mydues auth
```

Web clients must use the dashboard Connect flow; `mydues auth` will tell you so.

If `credentials.json` is missing, recreate the OAuth client in Google Cloud
Console, enable the Gmail API, download JSON to `~/.mydues/credentials.json`
(or your `MYDUES_CREDENTIALS` path), then Connect again.

## Move OAuth consent from Testing to Production

Avoids renewing every ~7 days for a personal app.

1. [Google Cloud Console](https://console.cloud.google.com/) → same project as `credentials.json`
2. **APIs & Services → OAuth consent screen**
3. **Publish app** (move publishing status from Testing to In production)
4. Confirm; unverified personal use is fine — expect the “Google hasn’t verified this app” warning
5. Disconnect Gmail (and optionally revoke under Google Account permissions)
6. Connect Gmail again so a new production refresh token is stored

No Card Dues code changes are required. Keep Gmail API enabled and the same
`credentials.json`.

## Redirect URI checklist

Default dashboard URL: `http://127.0.0.1:8765`

- **Web application** client: authorised redirect URI must be
  `http://127.0.0.1:<port>/oauth/callback` for the port you actually serve on
- **Desktop app** client: loopback redirect is handled by Google; no URI list to edit

If Fetch or Connect fails after changing `--port`, update the redirect URI and
reload the dashboard (see [RESTART.md](../RESTART.md)).
