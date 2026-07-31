# Restart the dashboard server

Use these steps whenever the dashboard is running old code or is not responding.

## 1. Stop the current server

Press `Ctrl+C` in the terminal running the server.

If that terminal is unavailable, run:

```bash
lsof -ti tcp:8765 | xargs kill
```

## 2. Start the server again

From the project directory:

```bash
cd ~/Projects/card-dues
.venv/bin/python -m carddues serve
```

The dashboard will be available at:

<http://127.0.0.1:8765>

Keep this terminal open while using the dashboard. To run it in the background:

```bash
.venv/bin/python -m carddues serve > /tmp/carddues-server.log 2>&1 &
```

## 3. Verify the restart

Open <http://127.0.0.1:8765> and reload the page.

You can also check the server from another terminal:

```bash
curl -s -o /dev/null -w "dashboard %{http_code}\n" \
  http://127.0.0.1:8765/
```

An HTTP `200` means the dashboard is responding.

## Development mode

To automatically reload Python and template changes while developing:

```bash
.venv/bin/python -m carddues serve --debug
```

Do not use debug mode when exposing the dashboard beyond your local machine.

## After updating the application

The first request after a restart applies database migrations automatically.
After restarting, reload the dashboard before fetching Gmail statements.

The current application should show **Connect Gmail** when Gmail is not connected.
If it still shows **Fetch from Gmail**, an older server process is still serving
the page. Stop the process on port `8765` and start it again.

## Troubleshooting

Check whether another process owns the port:

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

Start on another port if necessary:

```bash
.venv/bin/python -m carddues serve --port 8766
```

Then open <http://127.0.0.1:8766>.
