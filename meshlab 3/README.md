# MeshLab — MD01/MD02 video testbed

One codebase, both laptops. No pip installs — Python 3.9+ stdlib only.
Pipelines run through `gst-launch-1.0` (already installed and proven on
both machines). Dashboard works fully offline (no CDN assets).

## Files

| File | Purpose |
|---|---|
| `meshlab.py` | the agent: HTTP API + dashboard server, gst subprocess manager, radio poller, SQLite run logging |
| `pipelines.py` | transport definitions, per-parameter ranges, pipeline builders, presets |
| `static/index.html` | dashboard (schema-driven controls, live charts) |
| `config_mac.json` / `config_win.json` | per-laptop identity |

## Start

Copy this whole folder to **both** laptops.

```bash
# Mac
python3 meshlab.py -c config_mac.json
```

```powershell
# Windows (install Python 3 from python.org if `py` is missing)
py meshlab.py -c config_win.json
```

Open **http://localhost:8800** on either laptop (or the other agent's IP —
they cross-link). The header shows three dots: radio / peer / pipeline.
All three green = ready.

## Using it

1. Pick **direction** (who sends), **transport**, optionally a **preset**
   (`survival_bare` = the proven unbreakable config).
2. Adjust any parameter — every transport exposes its own tunable ranges
   (FEC %, SRT latency, jitter buffer, MTU, bitrate, fps, slices, …).
   The exact gst command being run is shown under Run control.
3. **Start run** — this agent starts the receiver on the peer agent and the
   sender locally (or vice-versa), and begins logging one stats row per
   second (local radio + peer radio + receiver fps) to `meshlab.db`.
4. Watch the charts: SNR both directions, delivered fps, radio throughput,
   ARQ retransmits, and **TX overflow** (if that alarms, the feed is bigger
   than the link's current capacity — lower the bitrate).
5. **Stop** ends the run on both sides. Export any run as CSV from the
   Runs table.

`source: testpattern` in Video lets you test the whole chain without a
camera (uses videotestsrc).

## Requirements recap

- GStreamer on PATH on both machines (`gst-launch-1.0 --version`)
- Firewall: TCP 8800 inbound allowed on both (the earlier MeshLab rule
  covers it), UDP 5000-5004 + 6000 for media
- Radios reachable: Mac → 192.168.10.41, Windows → 192.168.10.42

## Troubleshooting

- **peer dot red** — other agent not running, or TCP 8800 blocked, or
  wrong `peer_url` in config.
- **radio dot red** — radio IP wrong/unreachable; check `radio_url`.
- **pipeline starts then dies** — see the error line + last command in
  Run control; paste the command in a terminal to see full gst output.
- **video window doesn't appear on receiver** — firewall (UDP), or sender
  not actually running; check both agents' pipeline dots.
- **camera fails on Mac** — grant camera permission to the terminal app
  running meshlab (System Settings → Privacy & Security → Camera).
