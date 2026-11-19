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

Config:  ~/.config/hrdlog-upload.conf
State:   ~/.local/state/hrdlog-upload/  (seen.txt, offsets.json, queue.jsonl, log)
"""

import argparse
import configparser
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

URL = os.environ.get("HRDLOG_URL", "https://robot.hrdlog.net/NewEntry.aspx")  # override only for tests
APP = "wsjtx-tools"
CONF = os.path.expanduser("~/.config/hrdlog-upload.conf")
STATE_DIR = os.path.expanduser("~/.local/state/hrdlog-upload")
SEEN = os.path.join(STATE_DIR, "seen.txt")
OFFSETS = os.path.join(STATE_DIR, "offsets.json")
QUEUE = os.path.join(STATE_DIR, "queue.jsonl")
FAILED = os.path.join(STATE_DIR, "failed.adi")
LOG = os.path.join(STATE_DIR, "hrdlog-upload.log")
MAX_TRIES = 5          # HRDLog refused the record this many times -> failed.adi
RETRY_WAIT = 300       # seconds between retries after a network failure

CONF_TEMPLATE = """[hrdlog]
# Your callsign as registered on HRDLog.net
callsign =
# Upload code: HRDLog.net -> your profile (Options) -> "Upload code" (also in the e-mail
# they sent after registration). This is NOT your password.
code =
# ADIF files to watch, comma separated
files = ~/.local/share/WSJT-X/wsjtx_log.adi
# Seconds between checks
poll = 10
"""

TAG = re.compile(rb"<([A-Za-z0-9_]+)(?::(\d+))?(?::[A-Za-z])?>")


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
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
    cp = configparser.ConfigParser()
    if not os.path.exists(CONF):
        os.makedirs(os.path.dirname(CONF), exist_ok=True)
        with open(CONF, "w") as f:
            f.write(CONF_TEMPLATE)
        os.chmod(CONF, 0o600)
    cp.read(CONF)
    s = cp["hrdlog"] if cp.has_section("hrdlog") else {}
    files = [os.path.expanduser(p.strip()) for p in s.get("files", "").split(",") if p.strip()]
    return (s.get("callsign", "").strip().upper(), s.get("code", "").strip(), files,
            max(2, int(s.get("poll", "10") or 10)))


def main():
    ap = argparse.ArgumentParser(description="Send new ADIF contacts to HRDLog.net")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be sent, send nothing, change no state")
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--baseline", action="store_true",
                    help="mark everything in the files as seen (send nothing) and exit")
    ap.add_argument("--status", action="store_true", help="show queue / state and exit")
    args = ap.parse_args()

    os.makedirs(STATE_DIR, exist_ok=True)
    callsign, code, files, poll = read_conf()

    if args.status:
        q = load_queue()
        print("config:", CONF)
        print("callsign:", callsign or "(not set)", " code:", "set" if code else "(not set)")
        print("files:", ", ".join(files))
        print("seen contacts:", len(load_seen()))
        print("waiting to send:", len(q))
        for item in q[:20]:
            print("  ", item["key"], "tries", item.get("tries", 0), item.get("last", ""))
        return 0

    if args.dry_run:
        seen = load_seen()
        offsets = load_json(OFFSETS, {})
        for p in files:
            for fl, raw in scan_file(p, dict(offsets), set(seen), False, False):
                print("WOULD SEND", qso_key(fl), raw.decode("latin-1"))
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

    if not callsign or not code:
        log("callsign or upload code missing in %s; watching only (nothing is sent)" % CONF)

    queue = load_queue()
    next_retry = 0
    auth_bad = False
    log("started: %s, files %s, %d waiting" % (callsign or "?", ", ".join(files), len(queue)))
    while True:
        # 1. new records -> queue (marked seen at once, so they are queued only once)
        for p in files:
            new = scan_file(p, offsets, seen, False)
            for fl, raw in new:
                k = qso_key(fl)
                queue.append({"key": k, "raw": raw.decode("latin-1"), "tries": 0})
                log("new contact %s" % k)
            if new:
                add_seen(seen, [qso_key(fl) for fl, _ in new])
                save_queue(queue)
        save_json(OFFSETS, offsets)

        # 2. send the queue
        if queue and callsign and code and not auth_bad and time.time() >= next_retry:
            keep = []
            for i, item in enumerate(queue):
                r = send(callsign, code, item["raw"])
                if r == "ok":
                    log("sent %s" % item["key"])
                elif r == "dup":
                    log("already on HRDLog %s" % item["key"])
                elif r.startswith("net:"):
                    log("network problem (%s); %d waiting, retry in %d s"
                        % (r[4:], len(queue) - i, RETRY_WAIT))
                    keep.extend(queue[i:])
                    next_retry = time.time() + RETRY_WAIT
                    break
                elif r.startswith("auth:"):
                    log("HRDLog refused the login: %s. Check callsign / upload code in %s, "
                        "then restart the service." % (r[5:], CONF))
                    keep.extend(queue[i:])
                    auth_bad = True
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
                        next_retry = time.time() + RETRY_WAIT
                time.sleep(1)       # be gentle with the server
            queue = keep
            save_queue(queue)

        if args.once:
            return 0
        time.sleep(poll)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
