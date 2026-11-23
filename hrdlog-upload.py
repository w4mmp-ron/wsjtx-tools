#!/usr/bin/env python3
"""Watch ADIF log files and send each new contact to HRDLog.net.

HRDLog.net upload API (the one Wavelog, QLog, CQRLOG and Log4OM use):
  POST https://robot.hrdlog.net/NewEntry.aspx  (form-urlencoded)
  Callsign = your callsign, Code = the upload code from your HRDLog profile,
  App = program name, ADIFData = one ADIF record ending in <eor>.
  Reply (XML): <insert>1 = stored, <insert>0 = already there (duplicate),
  <error>...</error> = refused ("Unknown user", "Invalid token", ...).

On the first start the contacts already in the files are only marked as seen,
not sent. After that every new record is sent once. Records that could not be
sent (no network, HRDLog down) stay in a queue and are retried.

WSJT-X logs no name. If decode-split is used too, its US callbook (FCC) and its
HamQTH cache usually know the station: NAME, QTH and STATE are then added to the
record before it is sent (only fields the record doesn't already have).

Linux:   a user service (systemctl --user ... hrdlog-upload), no window.
  Config  ~/.config/hrdlog-upload.conf
  State   ~/.local/state/hrdlog-upload/  (seen.txt, offsets.json, queue.jsonl, log)
Windows: a small window (hrdlog-upload.exe) with callsign / upload code and the
  log; it uploads while it is open. "Start with Windows" starts it minimised.
  Config and state in %APPDATA%\\hrdlog-upload\\, watches %LOCALAPPDATA%\\WSJT-X\\wsjtx_log.adi
"""

import argparse
import configparser
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata

URL = os.environ.get("HRDLOG_URL", "https://robot.hrdlog.net/NewEntry.aspx")  # override only for tests
APP = "wsjtx-tools"
WINDOWS = os.name == "nt"
if WINDOWS:
    STATE_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "hrdlog-upload")
    CONF = os.path.join(STATE_DIR, "hrdlog-upload.conf")
    DEFAULT_FILES = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                                 "WSJT-X", "wsjtx_log.adi")
    DECODE_SPLIT_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                                    "decode-split")
else:
    CONF = os.path.expanduser("~/.config/hrdlog-upload.conf")
    STATE_DIR = os.path.expanduser("~/.local/state/hrdlog-upload")
    DEFAULT_FILES = "~/.local/share/WSJT-X/wsjtx_log.adi"
    DECODE_SPLIT_DIR = os.path.expanduser("~/.local/share/decode-split")
# decode-split's name files (read only; nothing is added when they are not there)
US_CALLBOOK = os.path.join(DECODE_SPLIT_DIR, "us_callbook.db")
LOOKUP_CACHE = os.path.join(DECODE_SPLIT_DIR, "lookup_cache.db")
SEEN = os.path.join(STATE_DIR, "seen.txt")
OFFSETS = os.path.join(STATE_DIR, "offsets.json")
QUEUE = os.path.join(STATE_DIR, "queue.jsonl")
FAILED = os.path.join(STATE_DIR, "failed.adi")
LOG = os.path.join(STATE_DIR, "hrdlog-upload.log")
MAX_TRIES = 5          # HRDLog refused the record this many times -> failed.adi
RETRY_WAIT = 300       # seconds between retries after a network failure

CONF_TEMPLATE = """[hrdlog]
# Your callsign as registered on HRDLog.net
callsign = {callsign}
# Upload code: HRDLog.net -> your profile (Options) -> "Upload code" (also in the e-mail
# they sent after registration). This is NOT your password.
code = {code}
# ADIF files to watch, comma separated
files = {files}
# Seconds between checks
poll = {poll}
"""

LISTENERS = []         # the Windows window adds itself here to see the log lines

TAG = re.compile(rb"<([A-Za-z0-9_]+)(?::(\d+))?(?::[A-Za-z])?>")


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    for f in LISTENERS:
        f(line)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def parse_records(data):
    """Return (records, end) for the complete records in data (bytes).

    Each record is (fields dict, raw bytes of the record). end = offset just
    after the last complete <eor>; an unfinished record after it is left alone.
    The ADIF length counts are used, so a '<' inside a value does no harm.
    """
    records = []
    pos = 0
    end = 0
    start = None
    fields = {}
    lower = data.lower()
    eoh = lower.find(b"<eoh>")
    if eoh >= 0:
        pos = end = eoh + 5
    while True:
        m = TAG.search(data, pos)
        if not m:
            break
        name = m.group(1).lower()
        if start is None:
            start = m.start()
        if name == b"eor":
            raw = data[start:m.end()].strip()
            if fields:
                records.append((fields, raw))
            fields = {}
            start = None
            pos = end = m.end()
            continue
        if name == b"eoh":
            fields = {}
            start = None
            pos = end = m.end()
            continue
        n = int(m.group(2) or 0)
        value = data[m.end():m.end() + n]
        if len(value) < n:          # record not fully written yet
            break
        fields[name.decode("ascii", "replace")] = value.decode("latin-1").strip()
        pos = m.end() + n
    return records, end


def qso_key(fields):
    t = fields.get("time_on", "")[:4]
    return "|".join([fields.get("call", "").upper(), fields.get("qso_date", ""), t,
                     fields.get("band", "").lower(), fields.get("mode", "").upper()])


def base_call(call):
    """W1ABC/P, KH6/W1ABC -> W1ABC (the longest part is the station's own call)."""
    parts = [x for x in call.upper().strip("<>").split("/") if x]
    return max(parts, key=len) if parts else ""


def plain(text):
    """ASCII only (ADIF counts characters; accents are dropped, not garbled)."""
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().strip()


def query(path, sql, args):
    if not os.path.exists(path):
        return None
    try:
        db = sqlite3.connect("file:%s?mode=ro" % urllib.request.pathname2url(path), uri=True)
        try:
            return db.execute(sql, args).fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return None


def station_info(call):
    """{'name', 'qth', 'state'} from decode-split's US callbook, else its HamQTH cache."""
    b = base_call(call)
    if not b:
        return {}
    r = query(US_CALLBOOK, "SELECT name, city, state FROM us_callbook WHERE callsign = ?", (b,))
    if r:
        return {"name": r[0], "qth": r[1], "state": r[2]}
    try:
        r = query(LOOKUP_CACHE, "SELECT name, qth, state FROM lookup_cache "
                                "WHERE callsign = ? AND found = 1", (b,))
    except sqlite3.Error:
        r = None
    if r is None:       # a cache from before decode-split kept the city
        r = query(LOOKUP_CACHE, "SELECT name, '', state FROM lookup_cache "
                                "WHERE callsign = ? AND found = 1", (b,))
    if r:
        return {"name": r[0], "qth": r[1] or "", "state": r[2]}
    return {}


def add_station_info(fields, raw):
    """raw (str) with NAME / QTH / STATE put in before <eor>, if known and not there yet.
    Returns (raw, the name added or "")."""
    info = station_info(fields.get("call", ""))
    extra = ""
    for key in ("name", "qth", "state"):
        v = plain(info.get(key, ""))
        if v and key not in fields:
            extra += "<%s:%d>%s " % (key.upper(), len(v), v)
    if not extra:
        return raw, ""
    i = raw.lower().rfind("<eor>")
    if i < 0:
        return raw, ""
    added = plain(info.get("name", "")) if "name" not in fields else ""
    return raw[:i] + extra + raw[i:], added


def load_seen():
    try:
        with open(SEEN) as f:
            return set(line.strip() for line in f if line.strip())
    except OSError:
        return set()


def add_seen(seen, keys, persist=True):
    new = [k for k in keys if k not in seen]
    if new and not persist:
        seen.update(new)
    elif new:
        with open(SEEN, "a") as f:
            for k in new:
                f.write(k + "\n")
        seen.update(new)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def load_queue():
    out = []
    try:
        with open(QUEUE) as f:
            for line in f:
                if line.strip():
                    out.append(json.loads(line))
    except (OSError, ValueError):
        pass
    return out


def save_queue(q):
    tmp = QUEUE + ".tmp"
    with open(tmp, "w") as f:
        for item in q:
            f.write(json.dumps(item) + "\n")
    os.replace(tmp, QUEUE)


def send(callsign, code, raw):
    """Send one record. Returns 'ok', 'dup', 'auth', 'refused:<text>' or 'net:<text>'."""
    body = urllib.parse.urlencode({"Callsign": callsign, "Code": code, "App": APP,
                                   "ADIFData": raw}).encode("ascii")
    req = urllib.request.Request(URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": APP})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        return "net:" + str(e)
    low = text.lower()
    if "<insert>1" in low:
        return "ok"
    if "<insert>0" in low:
        return "dup"
    m = re.search(r"<error>(.*?)</error>", text, re.S | re.I)
    err = m.group(1).strip() if m else text.strip()[:200]
    if "unknown user" in err.lower() or "invalid token" in err.lower():
        return "auth:" + err
    return "refused:" + err


def scan_file(path, offsets, seen, baseline, persist=True):
    """Return the new records in path (fields, raw) and update offsets."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    info = offsets.get(path)
    full = (info is None or info.get("ino") != st.st_ino or st.st_size < info.get("off", 0))
    start = 0 if full else info["off"]
    if not full and st.st_size == start:
        return []
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    records, end = parse_records(data)
    offsets[path] = {"ino": st.st_ino, "off": start + end}
    if baseline or info is None:
        add_seen(seen, [qso_key(fl) for fl, _ in records], persist)
        if records and persist:
            log("%s: %d existing contacts marked as seen (not sent)" % (path, len(records)))
        return []
    if full:
        log("%s: file was rewritten, checking all %d records against the seen list"
            % (path, len(records)))
    return [(fl, raw) for fl, raw in records if qso_key(fl) not in seen]


def read_conf():
    cp = configparser.ConfigParser(interpolation=None)
    if not os.path.exists(CONF):
        write_conf("", "", DEFAULT_FILES, 10)
    cp.read(CONF)
    s = cp["hrdlog"] if cp.has_section("hrdlog") else {}
    files = [os.path.expanduser(p.strip()) for p in s.get("files", "").split(",") if p.strip()]
    return (s.get("callsign", "").strip().upper(), s.get("code", "").strip(), files,
            max(2, int(s.get("poll", "10") or 10)))


def write_conf(callsign, code, files, poll):
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    with open(CONF, "w") as f:
        f.write(CONF_TEMPLATE.format(callsign=callsign, code=code, files=files, poll=poll))
    try:
        os.chmod(CONF, 0o600)
    except OSError:
        pass


class Uploader:
    """The watch / send loop, used by the Linux service and the Windows window."""

    def __init__(self):
        self.stop = threading.Event()
        self.wake = threading.Event()      # set = check now (settings saved, closing)
        self.reload = False
        self.load_conf()
        self.seen = load_seen()
        self.offsets = load_json(OFFSETS, {})
        self.queue = load_queue()

    def load_conf(self):
        self.callsign, self.code, self.files, self.poll = read_conf()
        self.auth_bad = False
        self.next_retry = 0

    def run(self, once=False):
        if not self.callsign or not self.code:
            log("callsign or upload code missing in %s; watching only (nothing is sent)" % CONF)
        log("started: %s, files %s, %d waiting"
            % (self.callsign or "?", ", ".join(self.files), len(self.queue)))
        while not self.stop.is_set():
            if self.reload:
                self.reload = False
                self.load_conf()
                log("settings saved: %s, upload code %s"
                    % (self.callsign or "?", "set" if self.code else "not set"))
            self.check()
            if once:
                return
            self.wake.wait(self.poll)
            self.wake.clear()

    def check(self):
        queue = self.queue
        # 1. new records -> queue (saved first, then marked seen: none is lost or queued twice)
        for p in self.files:
            new = scan_file(p, self.offsets, self.seen, False)
            for fl, raw in new:
                k = qso_key(fl)
                text, name = add_station_info(fl, raw.decode("latin-1"))
                queue.append({"key": k, "raw": text, "tries": 0})
                log("new contact %s%s" % (k, " (%s)" % name if name else ""))
            if new:
                save_queue(queue)
                add_seen(self.seen, [qso_key(fl) for fl, _ in new])
        save_json(OFFSETS, self.offsets)

        # 2. send the queue
        if queue and self.callsign and self.code and not self.auth_bad \
                and time.time() >= self.next_retry:
            keep = []
            for i, item in enumerate(queue):
                if self.stop.is_set():
                    keep.extend(queue[i:])
                    break
                r = send(self.callsign, self.code, item["raw"])
                if r == "ok":
                    log("sent %s" % item["key"])
                elif r == "dup":
                    log("already on HRDLog %s" % item["key"])
                elif r.startswith("net:"):
                    log("network problem (%s); %d waiting, retry in %d s"
                        % (r[4:], len(queue) - i, RETRY_WAIT))
                    keep.extend(queue[i:])
                    self.next_retry = time.time() + RETRY_WAIT
                    break
                elif r.startswith("auth:"):
                    log("HRDLog refused the login: %s. Check the callsign / upload code%s."
                        % (r[5:], ", then click Save" if WINDOWS
                           else " in %s, then restart the service" % CONF))
                    keep.extend(queue[i:])
                    self.auth_bad = True
                    break
                else:
                    item["tries"] = item.get("tries", 0) + 1
                    item["last"] = r[8:]
                    if item["tries"] >= MAX_TRIES:
                        log("gave up on %s: %s (saved in %s)" % (item["key"], r[8:], FAILED))
                        with open(FAILED, "a") as f:
                            f.write(item["raw"] + "\n")
                    else:
                        log("HRDLog refused %s: %s (try %d)" % (item["key"], r[8:], item["tries"]))
                        keep.append(item)
                        self.next_retry = time.time() + RETRY_WAIT
                time.sleep(1)       # be gentle with the server
            self.queue = keep
            save_queue(keep)


ERROR_WORDS = ("refused", "network problem", "gave up", "missing")


def service_state():
    """'running since ...' / 'not running' for the Linux user service, '' on Windows."""
    if WINDOWS:
        return ""
    try:
        out = subprocess.run(["systemctl", "--user", "show", "hrdlog-upload",
                              "-p", "ActiveState", "-p", "ActiveEnterTimestamp"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return "unknown (systemctl not found)"
    v = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    if v.get("ActiveState") == "active":
        return "running since %s" % (v.get("ActiveEnterTimestamp") or "?")
    return "NOT running (%s): systemctl --user start hrdlog-upload" % (v.get("ActiveState") or "?")


def log_tail(nbytes=200000):
    try:
        with open(LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - nbytes))
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def recent_contacts(lines, n=5):
    """Last n contacts from the log: (time, key, name, outcome)."""
    found = {}
    order = []
    for line in lines:
        t, msg = line[:19], line[20:]
        if msg.startswith("new contact "):
            rest = msg[12:]
            key, _, name = rest.partition(" (")
            found[key] = [t, key, name.rstrip(")"), "waiting"]
            if key in order:
                order.remove(key)
            order.append(key)
        else:
            for word, outcome in (("sent ", "sent"), ("already on HRDLog ", "already on HRDLog"),
                                  ("gave up on ", "gave up"), ("HRDLog refused ", "refused")):
                if msg.startswith(word):
                    key = msg[len(word):].split(":")[0].split(" ")[0]
                    if key in found:
                        found[key][3] = outcome
    return [found[k] for k in order[-n:]]


def count_rows(path, sql):
    r = query(path, sql, ())
    return r[0] if r else None


def print_status(callsign, code, files):
    q = load_queue()
    lines = log_tail()
    print("config:", CONF)
    print("callsign:", callsign or "(not set)", " code:", "set" if code else "(not set)")
    if not WINDOWS:
        print("service:", service_state())
    print("files:", ", ".join(files))
    print("seen contacts:", len(load_seen()))
    print("waiting to send:", len(q))
    for item in q[:20]:
        print("  ", item["key"], "tries", item.get("tries", 0), item.get("last", ""))
    us = count_rows(US_CALLBOOK, "SELECT COUNT(*) FROM us_callbook")
    dx = count_rows(LOOKUP_CACHE, "SELECT COUNT(*) FROM lookup_cache WHERE found=1")
    print("names: US callbook %s, HamQTH cache %s" % (
        "{:,} calls".format(us) if us is not None else "not found (run decode-split)",
        "%d names" % dx if dx is not None else "not found"))
    last = recent_contacts(lines)
    print("last contacts:" if last else "last contacts: none in the log yet")
    for t, key, name, outcome in last:
        print("   %s  %-32s %-20s %s" % (t, key, name[:20], outcome))
    # only problems after the last good upload: older ones were fixed since
    errors = []
    for l in lines:
        msg = l[20:]
        if msg.startswith("sent ") or msg.startswith("already on HRDLog "):
            errors = []
        elif any(w in msg for w in ERROR_WORDS):
            errors.append(l)
    print("problems since the last upload:", "none" if not errors else "")
    for l in errors[-3:]:
        print("  ", l)
    print("full log:", LOG)


def main():
    ap = argparse.ArgumentParser(description="Send new ADIF contacts to HRDLog.net")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be sent, send nothing, change no state")
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--baseline", action="store_true",
                    help="mark everything in the files as seen (send nothing) and exit")
    ap.add_argument("--status", action="store_true", help="show queue / state and exit")
    ap.add_argument("--minimized", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    os.makedirs(STATE_DIR, exist_ok=True)
    callsign, code, files, poll = read_conf()

    if args.status:
        print_status(callsign, code, files)
        return 0

    if args.dry_run:
        seen = load_seen()
        offsets = load_json(OFFSETS, {})
        for p in files:
            for fl, raw in scan_file(p, dict(offsets), set(seen), False, False):
                print("WOULD SEND", qso_key(fl), add_station_info(fl, raw.decode("latin-1"))[0])
        q = load_queue()
        print("queue:", len(q), "waiting")
        return 0

    seen = load_seen()
    offsets = load_json(OFFSETS, {})
    if args.baseline:
        for p in files:
            scan_file(p, offsets, seen, True)
        save_json(OFFSETS, offsets)
        return 0

    if WINDOWS and not args.once:
        return window(args.minimized)
    Uploader().run(args.once)
    return 0


# ----------------------------------------------------------------------------- Windows window

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "hrdlog-upload"


def start_command():
    if getattr(sys, "frozen", False):            # the .exe
        return '"%s" --minimized' % sys.executable
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return '"%s" "%s" --minimized' % (pyw, os.path.abspath(__file__))


def autostart(value=None):
    """Read (value None) or set the 'Start with Windows' entry."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
        if value is None:
            try:
                return bool(winreg.QueryValueEx(k, RUN_NAME)[0])
            except OSError:
                return False
        if value:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, start_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except OSError:
                pass
        return value


_MUTEX = None


def already_running():
    import ctypes
    global _MUTEX
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _MUTEX = k32.CreateMutexW(None, False, "Local\\hrdlog-upload-wsjtx-tools")
    return ctypes.get_last_error() == 183          # ERROR_ALREADY_EXISTS


def window(minimized=False):
    import queue as q
    import tkinter as tk
    from tkinter import messagebox, ttk

    if already_running():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("hrdlog-upload", "hrdlog-upload is already running.")
        return 0

    up = Uploader()
    lines = q.Queue()

    root = tk.Tk()
    root.title("hrdlog-upload")
    root.geometry("720x360")

    top = ttk.Frame(root, padding=6)
    top.pack(fill="x")
    ttk.Label(top, text="Callsign").pack(side="left")
    call_v = tk.StringVar(value=up.callsign)
    ttk.Entry(top, textvariable=call_v, width=10).pack(side="left", padx=(4, 12))
    ttk.Label(top, text="Upload code").pack(side="left")
    code_v = tk.StringVar(value=up.code)
    ttk.Entry(top, textvariable=code_v, width=24, show="*").pack(side="left", padx=4)

    def save():
        write_conf(call_v.get().strip().upper(), code_v.get().strip(),
                   ", ".join(up.files), up.poll)
        up.reload = True
        up.wake.set()
    ttk.Button(top, text="Save", command=save).pack(side="left", padx=8)
    ttk.Label(root, text="Upload code = the one on your HRDLog.net profile page, not your password.",
              foreground="#606060", padding=(6, 0)).pack(fill="x")

    status = ttk.Label(root, padding=(6, 0))
    status.pack(fill="x")
    ttk.Label(root, text="Watching: " + ", ".join(up.files), padding=(6, 2)).pack(fill="x")

    mid = ttk.Frame(root, padding=(6, 2))
    mid.pack(fill="both", expand=True)
    text = tk.Text(mid, font=("Consolas", 10), height=10, wrap="word", state="disabled")
    sb = ttk.Scrollbar(mid, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=sb.set)
    text.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    bottom = ttk.Frame(root, padding=6)
    bottom.pack(fill="x")
    auto_v = tk.BooleanVar(value=autostart())
    ttk.Checkbutton(bottom, text="Start with Windows (minimised)", variable=auto_v,
                    command=lambda: autostart(auto_v.get())).pack(side="left")
    ttk.Button(bottom, text="Open folder",
               command=lambda: os.startfile(STATE_DIR)).pack(side="right")
    ttk.Label(bottom, text="Uploads only while this window is open (it can be minimised).",
              foreground="#606060").pack(side="right", padx=8)

    # the last lines of the log file, so the window does not start empty
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            for line in f.readlines()[-30:]:
                lines.put(line.rstrip("\n"))
    except OSError:
        pass
    LISTENERS.append(lines.put)

    def show(line):
        text.configure(state="normal")
        text.insert("end", line + "\n")
        n = int(text.index("end-1c").split(".")[0])
        if n > 500:
            text.delete("1.0", "%d.0" % (n - 500))
        text.configure(state="disabled")
        text.see("end")

    def refresh():
        while True:
            try:
                show(lines.get_nowait())
            except q.Empty:
                break
        if not up.stop.is_set():
            status.configure(text="%s    upload code %s    waiting to send: %d    contacts seen: %d"
                             % (up.callsign or "(no callsign)", "set" if up.code else "NOT SET",
                                len(up.queue), len(up.seen)))
        root.after(500, refresh)

    worker = threading.Thread(target=up.run, daemon=True)

    def close():
        up.stop.set()
        up.wake.set()
        status.configure(text="stopping ...")

        def wait():
            if worker.is_alive():
                root.after(200, wait)
            else:
                log("stopped")
                root.destroy()
        wait()

    root.protocol("WM_DELETE_WINDOW", close)
    worker.start()
    refresh()
    if minimized:
        root.iconify()
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
