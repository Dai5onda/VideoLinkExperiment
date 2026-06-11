#!/usr/bin/env python3
"""MeshLab agent — video-over-COFDM-mesh experiment testbed.

Pure Python stdlib (no pip installs). Runs identically on macOS and
Windows; the config file decides who it is. Drives gst-launch-1.0 as a
subprocess, polls the Suntor radio's JSON API, logs runs to SQLite,
serves the dashboard, and can remote-control the peer agent so one
browser tab starts both ends of a run.

Usage:  python3 meshlab.py -c config_mac.json
        py meshlab.py -c config_win.json     (Windows)
Then open http://localhost:8800
"""
import argparse
import collections
import csv
import io
import json
import os
import platform as _platform
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pipelines

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = "mac" if _platform.system() == "Darwin" else "win"
VERSION = "0.1"

# ------------------------------------------------------------------ config

def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("name", "port", "local_ip", "peer_ip", "peer_url", "radio_url"):
        if key not in cfg:
            sys.exit(f"config missing key: {key}")
    return cfg


# ------------------------------------------------------------- http helper

def http_json(url, payload=None, timeout=3):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("error")
        except Exception:
            detail = None
        raise RuntimeError(f"peer error: {detail or e.reason}") from None


# --------------------------------------------------------- pipeline manager

FPS_RE = re.compile(r"rendered: (\d+), dropped: (\d+), current: ([\d.]+)")


class PipelineManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.meta = {}
        self.fps = {}
        self.tail = collections.deque(maxlen=40)
        self.error = None
        self.gst = shutil.which("gst-launch-1.0") or shutil.which("gst-launch-1.0.exe")

    def start(self, kind, transport, video, params, dest_ip):
        with self.lock:
            self._stop_locked()
            if not self.gst:
                raise RuntimeError("gst-launch-1.0 not found on PATH")
            if kind == "sender":
                argv = pipelines.build_sender(transport, video, params,
                                              PLATFORM, dest_ip)
            else:
                argv = pipelines.build_receiver(transport, params, PLATFORM)
            argv[0] = self.gst
            self.error = None
            self.fps = {}
            self.tail.clear()
            self.proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace")
            self.meta = {"kind": kind, "transport": transport,
                         "started": time.time(),
                         "cmd": " ".join(argv[1:])}
            threading.Thread(target=self._reader, args=(self.proc,),
                             daemon=True).start()
            return self.meta

    def _reader(self, proc):
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            m = FPS_RE.search(line)
            if m:
                self.fps = {"rendered": int(m.group(1)),
                            "dropped": int(m.group(2)),
                            "current": float(m.group(3)),
                            "ts": time.time()}
                continue
            self.tail.append(line[:300])
            if "ERROR" in line or "erroneous pipeline" in line:
                self.error = line[:300]

    def stop(self):
        with self.lock:
            self._stop_locked()

    def _stop_locked(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.meta = {}

    def state(self):
        running = self.proc is not None and self.proc.poll() is None
        exited = (self.proc is not None and self.proc.poll() is not None)
        return {"running": running,
                "exited_code": self.proc.poll() if exited else None,
                "meta": self.meta, "fps": self.fps, "error": self.error,
                "tail": list(self.tail)[-8:]}


# ------------------------------------------------------------- radio poller

class RadioPoller(threading.Thread):
    """Polls the local radio's /status + /statusadvanced once per second."""

    def __init__(self, base_url):
        super().__init__(daemon=True)
        self.base = base_url.rstrip("/")
        self.latest = {}
        self.ok = False
        self._prev = {}

    def run(self):
        while True:
            try:
                s = http_json(self.base + "/status", timeout=2)
                a = http_json(self.base + "/statusadvanced", timeout=2)
                self.latest = self._digest(s, a)
                self.ok = True
            except Exception as e:
                self.ok = False
                self.latest = {"error": str(e)[:120]}
            time.sleep(1.0)

    def _digest(self, s, a):
        out = {"ts": time.time()}
        self_id = s.get("selfId")
        ids = [n.get("id") for n in s.get("nodeInfos", [])]
        lq = s.get("linkQuality") or []
        out["self_id"] = self_id
        out["node_ids"] = ids
        out["node_count"] = s.get("nodeNumber")
        # SNR both directions between self and the first other node
        if self_id in ids and len(ids) >= 2 and lq:
            i = ids.index(self_id)
            j = next(k for k, nid in enumerate(ids) if nid != self_id)
            try:
                out["snr_out"] = lq[i][j]
                out["snr_in"] = lq[j][i]
                out["peer_node"] = ids[j]
            except (IndexError, TypeError):
                pass
        for n in s.get("nodesRssi", []):
            out["rssi_ant1"] = n.get("ant1Rssi")
            out["rssi_ant2"] = n.get("ant2Rssi")
            break
        for n in s.get("nodeInfos", []):
            if n.get("id") == self_id:
                out["resource_ratio"] = n.get("resourceRatio")
        out["temp_c"] = s.get("temp")
        # counters -> per-second deltas
        counters = {"arq": a.get("arqRetransmit"),
                    "tx_overflow": a.get("unicastTxOverflow"),
                    "rx_lost": a.get("unicastRxLost"),
                    "crc_err": a.get("aaCrcErrCount"),
                    "eth_rx_bytes": a.get("ethRxBytes"),
                    "phy_tx_bytes": a.get("phyTxBytes")}
        now = time.time()
        for k, v in counters.items():
            out[k] = v
            if v is None:
                continue
            pk, pt = self._prev.get(k, (None, None))
            if pk is not None and now > (pt or 0) and v >= pk:
                out[k + "_d"] = round((v - pk) / (now - pt), 2)
            self._prev[k] = (v, now)
        if out.get("eth_rx_bytes_d") is not None:
            out["radio_eth_kbps"] = round(out["eth_rx_bytes_d"] * 8 / 1000, 1)
        out["crc_ratio"] = a.get("aaCrcErrRatio")
        return out


# ----------------------------------------------------------------- storage

class Store:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = None
        self.path = None
        # synced/odd folders (OneDrive, network mounts) can break sqlite
        # locking -> fall back: requested path, then user home, then memory
        fallback = os.path.join(os.path.expanduser("~"), "meshlab.db")
        for candidate in (path, fallback, ":memory:"):
            try:
                db = sqlite3.connect(candidate, check_same_thread=False)
                db.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER "
                           "PRIMARY KEY AUTOINCREMENT, started TEXT, "
                           "ended TEXT, direction TEXT, transport TEXT, "
                           "params TEXT)")
                db.execute("CREATE TABLE IF NOT EXISTS samples (run_id "
                           "INTEGER, ts REAL, data TEXT)")
                db.commit()
                self.db, self.path = db, candidate
                break
            except sqlite3.OperationalError:
                continue
        if self.path != path:
            print(f"note: run database at {self.path} "
                  f"(could not write {path})")

    def start_run(self, direction, transport, params):
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO runs (started, direction, transport, params) "
                "VALUES (datetime('now'), ?, ?, ?)",
                (direction, transport, json.dumps(params)))
            self.db.commit()
            return cur.lastrowid

    def end_run(self, run_id):
        with self.lock:
            self.db.execute("UPDATE runs SET ended = datetime('now') "
                            "WHERE id = ?", (run_id,))
            self.db.commit()

    def add_sample(self, run_id, data):
        with self.lock:
            self.db.execute("INSERT INTO samples VALUES (?, ?, ?)",
                            (run_id, time.time(), json.dumps(data)))
            self.db.commit()

    def runs(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT id, started, ended, direction, transport, params "
                "FROM runs ORDER BY id DESC LIMIT 100").fetchall()
        return [{"id": r[0], "started": r[1], "ended": r[2],
                 "direction": r[3], "transport": r[4],
                 "params": json.loads(r[5] or "{}")} for r in rows]

    def export_csv(self, run_id):
        with self.lock:
            rows = self.db.execute("SELECT ts, data FROM samples WHERE "
                                   "run_id = ? ORDER BY ts", (run_id,)).fetchall()
        flat_rows, keys = [], []
        for ts, data in rows:
            d = _flatten(json.loads(data))
            d["ts"] = ts
            flat_rows.append(d)
            for k in d:
                if k not in keys:
                    keys.append(k)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(flat_rows)
        return buf.getvalue()


def _flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        elif isinstance(v, (list, tuple)):
            out[key] = json.dumps(v)
        else:
            out[key] = v
    return out


# ------------------------------------------------------------------- agent

class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.pipe = PipelineManager()
        self.radio = RadioPoller(cfg["radio_url"])
        self.radio.start()
        self.store = Store(os.path.join(HERE, "meshlab.db"))
        self.samples = collections.deque(maxlen=600)
        self.run = None
        threading.Thread(target=self._sampler, daemon=True).start()

    # one merged stats sample per second
    def _sampler(self):
        while True:
            sample = {"radio": dict(self.radio.latest),
                      "pipeline": self.pipe.state()}
            sample["pipeline"].pop("tail", None)
            if self.run:
                try:
                    peer = http_json(self.cfg["peer_url"] +
                                     "/api/stats/latest", timeout=2)
                    sample["peer"] = {"radio": peer.get("radio", {}),
                                      "pipeline": peer.get("pipeline", {})}
                except Exception as e:
                    sample["peer"] = {"error": str(e)[:80]}
                self.store.add_sample(self.run["id"], sample)
            sample["ts"] = time.time()
            self.samples.append(sample)
            time.sleep(1.0)

    # ---- local pipeline control
    def pipeline_start(self, body):
        kind = body.get("kind")
        transport = body.get("transport")
        if kind not in ("sender", "receiver"):
            raise ValueError("kind must be sender|receiver")
        if transport not in pipelines.TRANSPORTS:
            raise ValueError("unknown transport")
        dest = body.get("dest_ip") or self.cfg["peer_ip"]
        params = body.get("sender" if kind == "sender" else "receiver") or {}
        return self.pipe.start(kind, transport, body.get("video") or {},
                               params, dest)

    # ---- orchestrated run (this agent = the console)
    def run_start(self, body):
        if self.run:
            raise ValueError("a run is already active — stop it first")
        direction = body.get("direction", "self->peer")
        transport = body.get("transport")
        payload = {"transport": transport, "video": body.get("video"),
                   "sender": body.get("sender"), "receiver": body.get("receiver")}
        if direction == "self->peer":
            # receiver on peer first, then local sender
            http_json(self.cfg["peer_url"] + "/api/pipeline/start",
                      dict(payload, kind="receiver"))
            self.pipeline_start(dict(payload, kind="sender",
                                     dest_ip=self.cfg["peer_ip"]))
        else:
            self.pipeline_start(dict(payload, kind="receiver"))
            http_json(self.cfg["peer_url"] + "/api/pipeline/start",
                      dict(payload, kind="sender",
                           dest_ip=self.cfg["local_ip"]))
        run_id = self.store.start_run(direction, transport, body)
        self.run = {"id": run_id, "direction": direction,
                    "transport": transport, "started": time.time()}
        return self.run

    def run_stop(self):
        if self.run:
            self.store.end_run(self.run["id"])
            self.run = None
        self.pipe.stop()
        try:
            http_json(self.cfg["peer_url"] + "/api/pipeline/stop", {})
        except Exception:
            pass

    def info(self):
        return {"name": self.cfg["name"], "platform": PLATFORM,
                "version": VERSION, "peer_url": self.cfg["peer_url"],
                "peer_ip": self.cfg["peer_ip"], "local_ip": self.cfg["local_ip"],
                "radio_url": self.cfg["radio_url"],
                "schema": pipelines.schema()}

    def state(self):
        peer_ok = True
        peer_name = None
        try:
            peer_name = http_json(self.cfg["peer_url"] + "/api/ping",
                                  timeout=1.5).get("name")
        except Exception:
            peer_ok = False
        return {"pipeline": self.pipe.state(), "run": self.run,
                "radio_ok": self.radio.ok, "peer_ok": peer_ok,
                "peer_name": peer_name}


# ------------------------------------------------------------------- serve

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_handler(agent):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods",
                             "GET, POST, OPTIONS")
            if extra:
                for k, v in extra.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj))

        def _err(self, msg, code=400):
            self._json({"error": str(msg)}, code)

        def do_OPTIONS(self):
            self._send(204, b"")

        def do_GET(self):
            path = self.path.split("?")[0]
            q = {}
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        q[k] = v
            try:
                if path == "/" or path == "/index.html":
                    with open(os.path.join(HERE, "static", "index.html"),
                              "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
                elif path == "/api/ping":
                    self._json({"name": agent.cfg["name"], "ok": True})
                elif path == "/api/info":
                    self._json(agent.info())
                elif path == "/api/state":
                    self._json(agent.state())
                elif path == "/api/stats":
                    n = int(q.get("n", "180"))
                    self._json(list(agent.samples)[-n:])
                elif path == "/api/stats/latest":
                    self._json(agent.samples[-1] if agent.samples else {})
                elif path == "/api/runs":
                    self._json(agent.store.runs())
                elif path == "/api/export":
                    rid = int(q.get("run", "0"))
                    csv_text = agent.store.export_csv(rid)
                    self._send(200, csv_text, "text/csv", {
                        "Content-Disposition":
                            f"attachment; filename=run_{rid}.csv"})
                else:
                    self._err("not found", 404)
            except Exception as e:
                self._err(e, 500)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._err("bad json")
            try:
                if self.path == "/api/pipeline/start":
                    self._json(agent.pipeline_start(body))
                elif self.path == "/api/pipeline/stop":
                    agent.pipe.stop()
                    self._json({"ok": True})
                elif self.path == "/api/run/start":
                    self._json(agent.run_start(body))
                elif self.path == "/api/run/stop":
                    agent.run_stop()
                    self._json({"ok": True})
                else:
                    self._err("not found", 404)
            except Exception as e:
                self._err(e, 500)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.json")
    args = ap.parse_args()
    cfg = load_config(os.path.join(HERE, args.config)
                      if not os.path.isabs(args.config) else args.config)
    agent = Agent(cfg)
    srv = ThreadingHTTPServer((cfg.get("listen", "0.0.0.0"), cfg["port"]),
                              make_handler(agent))
    print(f"MeshLab agent '{cfg['name']}' ({PLATFORM}) on "
          f"http://localhost:{cfg['port']}  |  radio {cfg['radio_url']}  "
          f"|  peer {cfg['peer_url']}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        agent.run_stop()


if __name__ == "__main__":
    main()
