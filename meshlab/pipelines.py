"""MeshLab pipeline definitions.

Every transport declares a SCHEMA of adjustable parameters (type, range,
step, default, help). The dashboard renders controls directly from this
schema, and the builders below turn a parameter dict into a gst-launch-1.0
argv list. Adding a knob = adding one schema entry + using it in the builder.

No third-party dependencies. Pipelines run via `gst-launch-1.0` subprocess,
so argv tokens are passed without any shell quoting pain.
"""

# ---------------------------------------------------------------- schemas

def P(type_, default, label, help_="", **kw):
    d = {"type": type_, "default": default, "label": label, "help": help_}
    d.update(kw)
    return d


VIDEO_PARAMS = {
    "source": P("enum", "auto", "Video source",
                "auto = real webcam (avfvideosrc on Mac / mfvideosrc on Windows). "
                "testpattern = built-in generator, no camera needed.",
                options=["auto", "testpattern"]),
    "device_index": P("int", 0, "Camera index",
                      "Which camera if several are attached.", min=0, max=5, step=1),
    "resolution": P("enum", "640x360", "Resolution",
                    "Sacrifice resolution before framerate.",
                    options=["1920x1080", "1280x720", "854x480", "640x360",
                             "480x270", "320x240"]),
    "fps": P("int", 30, "Framerate (fps)",
             "Keep >= 24 for a responsive feel.", min=5, max=60, step=1),
    "bitrate_kbps": P("int", 1500, "Video bitrate (kbps)",
                      "Must fit the radio's CURRENT capacity with ~50% headroom. "
                      "MCS0 @ 2.5 MHz floor is ~800-1000 kbps total.",
                      min=50, max=12000, step=50),
    "keyint": P("int", 30, "Keyframe interval (frames)",
                "With intra-refresh ON this is the refresh cycle length.",
                min=1, max=300, step=1),
    "intra_refresh": P("bool", True, "Intra-refresh (no IDR keyframes)",
                       "Corruption heals as a moving stripe instead of a GOP-long freeze."),
    "slices": P("int", 4, "Slices per frame",
                "One lost packet costs a stripe, not the whole frame.",
                min=1, max=8, step=1),
    "speed_preset": P("enum", "ultrafast", "x264 speed preset",
                      "Slower = better quality per bit, more CPU + latency.",
                      options=["ultrafast", "superfast", "veryfast", "faster",
                               "fast", "medium"]),
    "time_overlay": P("bool", False, "Burn timestamp overlay",
                      "Needs pango plugin; useful for latency measurements."),
}

TRANSPORTS = {
    "udp_ts": {
        "label": "Raw UDP / MPEG-TS",
        "desc": "Simplest + lowest latency. No recovery: every lost packet hits the picture.",
        "sender": {
            "port": P("int", 5000, "UDP port", min=1024, max=65535, step=1),
            "ts_alignment": P("int", 7, "TS packets per UDP datagram",
                              "7 x 188B = 1316B, safely under MTU.",
                              min=1, max=7, step=1),
        },
        "receiver": {
            "port": P("int", 5000, "UDP port", min=1024, max=65535, step=1),
            "buffer_kb": P("int", 2048, "Socket buffer (kB)",
                           "Bigger absorbs bursts; smaller drops sooner.",
                           min=64, max=8192, step=64),
        },
    },
    "rtp": {
        "label": "RTP (no FEC)",
        "desc": "Adds sequencing + jitter buffer. Loss visible but reordering handled.",
        "sender": {
            "port": P("int", 5000, "UDP port", min=1024, max=65535, step=1),
            "mtu": P("int", 1200, "RTP MTU (bytes)",
                     "Payload size per packet. Keep <= 1400 to avoid IP fragmentation.",
                     min=500, max=1400, step=10),
        },
        "receiver": {
            "port": P("int", 5000, "UDP port", min=1024, max=65535, step=1),
            "jb_latency_ms": P("int", 200, "Jitter buffer (ms)",
                               "Higher rides out delay spikes, adds fixed latency.",
                               min=0, max=2000, step=10),
        },
    },
    "rtp_ulpfec": {
        "label": "RTP + ULP FEC",
        "desc": "Forward error correction: fixed latency that never degrades. "
                "The survival transport.",
        "sender": {
            "port": P("int", 5000, "UDP port", min=1024, max=65535, step=1),
            "mtu": P("int", 1200, "RTP MTU (bytes)", min=500, max=1400, step=10),
            "fec_percentage": P("int", 50, "FEC overhead (%)",
                                "100% ~ doubles the stream but survives heavy loss. "
                                "Budget = bitrate x (1 + this/100).",
                                min=0, max=100, step=5),
            "fec_multipacket": P("bool", True, "Multi-packet FEC",
                                 "Protect across packets (recommended for video)."),
        },
        "receiver": {
            "port": P("int", 5000, "UDP port", min=1024, max=65535, step=1),
            "latency_ms": P("int", 200, "rtpbin latency (ms)",
                            "Must cover FEC recovery time.", min=0, max=2000, step=10),
        },
    },
    "srt": {
        "label": "SRT (ARQ retransmission)",
        "desc": "Clean picture under mild loss; freezes if loss exceeds the latency budget.",
        "sender": {
            "port": P("int", 6000, "SRT port", min=1024, max=65535, step=1),
            "latency_ms": P("int", 200, "SRT latency budget (ms)",
                            "Time window for retransmissions. Bigger = more recovery, more delay.",
                            min=20, max=2000, step=10),
        },
        "receiver": {
            "port": P("int", 6000, "SRT port", min=1024, max=65535, step=1),
            "latency_ms": P("int", 200, "SRT latency budget (ms)",
                            min=20, max=2000, step=10),
        },
    },
}

PRESETS = {
    "survival_bare": {
        "desc": "The unbreakable floor feed: fits MCS0 @ 2.5 MHz. 200 kbps + 100% FEC.",
        "transport": "rtp_ulpfec",
        "video": {"resolution": "480x270", "fps": 30, "bitrate_kbps": 200,
                  "keyint": 30, "intra_refresh": True, "slices": 4,
                  "speed_preset": "ultrafast"},
        "sender": {"fec_percentage": 100, "fec_multipacket": True},
        "receiver": {"latency_ms": 200},
    },
    "balanced_720p": {
        "desc": "Good picture when the link is healthy. Needs ~MCS6+ capacity.",
        "transport": "srt",
        "video": {"resolution": "1280x720", "fps": 30, "bitrate_kbps": 3000,
                  "keyint": 60, "intra_refresh": True, "slices": 4,
                  "speed_preset": "veryfast"},
        "sender": {"latency_ms": 150},
        "receiver": {"latency_ms": 150},
    },
    "lowest_latency": {
        "desc": "Raw UDP, no buffers. For latency benchmarking, not robustness.",
        "transport": "udp_ts",
        "video": {"resolution": "640x360", "fps": 30, "bitrate_kbps": 1000,
                  "keyint": 30, "intra_refresh": True, "slices": 4,
                  "speed_preset": "ultrafast"},
        "sender": {},
        "receiver": {"buffer_kb": 256},
    },
}


def schema():
    return {"video": VIDEO_PARAMS, "transports": {
        k: {"label": v["label"], "desc": v["desc"],
            "sender": v["sender"], "receiver": v["receiver"]}
        for k, v in TRANSPORTS.items()}, "presets": PRESETS}


# ---------------------------------------------------------------- helpers

def _defaults(spec):
    return {k: v["default"] for k, v in spec.items()}


def merge_params(spec, given):
    """Fill defaults, clamp ranges, drop unknown keys."""
    out = _defaults(spec)
    for k, v in (given or {}).items():
        if k not in spec:
            continue
        s = spec[k]
        if s["type"] == "int":
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            v = max(s["min"], min(s["max"], v))
        elif s["type"] == "bool":
            v = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "on")
        elif s["type"] == "enum":
            if v not in s["options"]:
                continue
        out[k] = v
    return out


def _source_tokens(v, platform):
    if v["source"] == "testpattern":
        return ["videotestsrc", "is-live=true", "pattern=smpte"]
    if platform == "mac":
        return ["avfvideosrc", f"device-index={v['device_index']}"]
    return ["mfvideosrc", f"device-index={v['device_index']}"]


def _encode_tokens(v):
    w, h = v["resolution"].split("x")
    b = v["bitrate_kbps"]
    toks = ["!", "videoconvert", "!", "videoscale", "!",
            f"video/x-raw,width={w},height={h},framerate={v['fps']}/1"]
    if v["time_overlay"]:
        toks += ["!", "timeoverlay", "halignment=right", "valignment=top"]
    toks += ["!", "x264enc", "tune=zerolatency",
             f"speed-preset={v['speed_preset']}", f"bitrate={b}",
             f"key-int-max={v['keyint']}",
             f"intra-refresh={'true' if v['intra_refresh'] else 'false'}",
             f"option-string=slices={v['slices']}:vbv-maxrate={b}:vbv-bufsize={max(b // 2, 50)}"]
    return toks


_DECODE = ["!", "rtph264depay", "!", "avdec_h264", "!", "videoconvert",
           "!", "fpsdisplaysink", "text-overlay=false", "sync=false"]
_DECODE_TS = ["!", "tsparse", "!", "tsdemux", "!", "h264parse", "!",
              "avdec_h264", "!", "videoconvert",
              "!", "fpsdisplaysink", "text-overlay=false", "sync=false"]

_RTP_CAPS = ("caps=application/x-rtp,media=video,clock-rate=90000,"
             "encoding-name=H264,payload=96")


# ---------------------------------------------------------------- builders

def build_sender(transport, video, params, platform, dest_ip):
    v = merge_params(VIDEO_PARAMS, video)
    p = merge_params(TRANSPORTS[transport]["sender"], params)
    src = _source_tokens(v, platform)
    enc = _encode_tokens(v)

    if transport == "udp_ts":
        toks = src + enc + ["!", "mpegtsmux", f"alignment={p['ts_alignment']}",
                            "!", "udpsink", f"host={dest_ip}", f"port={p['port']}"]
    elif transport == "rtp":
        toks = src + enc + ["!", "rtph264pay", "config-interval=1", "pt=96",
                            f"mtu={p['mtu']}",
                            "!", "udpsink", f"host={dest_ip}", f"port={p['port']}"]
    elif transport == "rtp_ulpfec":
        mp = "true" if p["fec_multipacket"] else "false"
        fec = ('fec-encoders=fec,0="rtpulpfecenc\\ percentage\\='
               f'{p["fec_percentage"]}\\ multipacket\\={mp}\\ pt\\=122";')
        toks = (["rtpbin", "name=rtp", fec] + src + enc +
                ["!", "rtph264pay", "config-interval=1", "pt=96",
                 f"mtu={p['mtu']}", "!", "rtp.send_rtp_sink_0",
                 "rtp.send_rtp_src_0", "!", "udpsink",
                 f"host={dest_ip}", f"port={p['port']}"])
    elif transport == "srt":
        toks = src + enc + ["!", "mpegtsmux", "alignment=7", "!", "srtsink",
                            f"uri=srt://{dest_ip}:{p['port']}?mode=caller",
                            f"latency={p['latency_ms']}",
                            "wait-for-connection=false"]
    else:
        raise ValueError(f"unknown transport {transport}")
    return ["gst-launch-1.0", "-v"] + toks


def build_receiver(transport, params, platform):
    p = merge_params(TRANSPORTS[transport]["receiver"], params)

    if transport == "udp_ts":
        toks = ["udpsrc", f"port={p['port']}",
                f"buffer-size={p['buffer_kb'] * 1024}"] + _DECODE_TS
    elif transport == "rtp":
        toks = ["udpsrc", f"port={p['port']}", _RTP_CAPS,
                "!", "rtpjitterbuffer", f"latency={p['jb_latency_ms']}",
                "do-lost=true"] + _DECODE
    elif transport == "rtp_ulpfec":
        fec = 'fec-decoders=fec,0="rtpulpfecdec\\ pt\\=122";'
        toks = (["rtpbin", "name=rtp", f"latency={p['latency_ms']}", fec,
                 "udpsrc", f"port={p['port']}", _RTP_CAPS,
                 "!", "rtp.recv_rtp_sink_0", "rtp."] + _DECODE)
    elif transport == "srt":
        toks = ["srtsrc", f"uri=srt://:{p['port']}?mode=listener",
                f"latency={p['latency_ms']}"] + _DECODE_TS
    else:
        raise ValueError(f"unknown transport {transport}")
    return ["gst-launch-1.0", "-v"] + toks
