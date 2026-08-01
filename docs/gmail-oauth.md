# Gmail OAuth: expiry and renewal

Card Dues uses Google OAuth with scope `gmail.readonly`.
Files live under `~/.carddues/`:

- `credentials.json` — OAuth client from Google Cloud Console
- `token.json` — access + refresh tokens after Connect Gmail

## What expires when

| Thing | Typical lifetime |
|--------|------------------|
| Access token | ~1 hour (auto-refreshed by the app) |
| Refresh token (consent screen **Testing**) | ~7 days |
| Refresh token (consent screen **In production**) | Until revoked / unused long-term / account security change |
| `credentials.json` (client ID/secret) | No fixed day count; only if you delete/rotate the client |

You only need to reconnect when the **refresh token** dies or is revoked.

## Renew (refresh token expired)

1. Start the dashboard: `.venv/bin/python -m carddues serve`
2. Click **Disconnect Gmail** (clears `token.json`)
3. Optional: revoke the app at https://myaccount.google.com/permissions
4. Click **Connect Gmail** and finish consent
5. Confirm Fetch works

Or from a terminal (desktop client):

```bash
.venv/bin/python -m carddues auth
```

If `credentials.json` is missing, recreate the OAuth client in Google Cloud Console, enable the Gmail API, download JSON to `~/.carddues/credentials.json`, then Connect again.

## Move OAuth consent from Testing to Production

Avoids renewing every ~7 days for a personal app.

1. [Google Cloud Console](https://console.cloud.google.com/) → same project as `credentials.json`
2. **APIs & Services → OAuth consent screen**
3. **Publish app** (move publishing status from Testing to In production)
4. Confirm; unverified personal use is fine — expect the “Google hasn’t verified this app” warning
5. Disconnect Gmail (and optionally revoke under Google Account permissions)
6. Connect Gmail again so a new production refresh token is stored

No Card Dues code changes are required. Keep Gmail API enabled and the same `credentials.json`.
