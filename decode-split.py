#!/usr/bin/env python3
"""Decode split: WSJT-X decodes in two lists, local calls and DX calls.

Listens to the UDP messages WSJT-X sends (Settings -> Reporting -> UDP Server)
and looks each call up in WSJT-X's cty.dat. The address and port are read from
WSJT-X's own settings: 127.0.0.1 = this program alone on the port; a multicast
group such as 224.0.0.1 = shared with other programs (GridTracker, a logger ...).
Runs on Linux and on Windows (there WSJT-X's files are in %LOCALAPPDATA%\\WSJT-X).

Left list = local: your own country (from MyCall in WSJT-X and cty.dat), or with
"Local: My continent" your whole continent. Right list = everything else (DX).

Colours:  CQ = green   new country (never worked) = red
          new country on this band = orange   your call in the message = yellow
          call already worked on this band = grey text ("Hide worked" hides those)
Double-click a line = same as a double-click in WSJT-X (it calls that station).
Erasing Band Activity in WSJT-X clears both lists here too, and with "Clear each
period" ticked the lists start fresh with the first decode of each new period.
"""

import ipaddress
import io
import json
import queue
import os
import re
import socket
import sqlite3
import struct
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from tkinter import ttk

HOST = "127.0.0.1"   # used when WSJT-X's settings don't give an address
PORT = 2237
WINDOWS = os.name == "nt"
if WINDOWS:
    # WSJT-X on Windows keeps its settings, log and cty.dat together in %LOCALAPPDATA%
    _local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    WSJTX_DIR = os.path.join(_local, "WSJT-X")
    WSJTX_INI = os.path.join(WSJTX_DIR, "WSJT-X.ini")
    SETTINGS = os.path.join(os.environ.get("APPDATA") or _local, "decode-split.json")
    DATA_DIR = os.path.join(_local, "decode-split")
    FONT, HEAD_FONT = ("Consolas", 10), ("Segoe UI", 10, "bold")
else:
    WSJTX_DIR = os.path.expanduser("~/.local/share/WSJT-X")
    WSJTX_INI = os.path.expanduser("~/.config/WSJT-X.ini")
    SETTINGS = os.path.expanduser("~/.config/decode-split.json")
    DATA_DIR = os.path.expanduser("~/.local/share/decode-split")
    FONT, HEAD_FONT = ("DejaVu Sans Mono", 10), ("DejaVu Sans", 10, "bold")
CALLBOOK = os.path.join(DATA_DIR, "us_callbook.db")
FCC_URL = "https://data.fcc.gov/download/pub/uls/complete/l_amat.zip"
# The FCC server refuses requests from programs it doesn't know: say who we are.
USER_AGENT = "decode-split/1.6.0 (+https://github.com/w4mmp-ron/wsjtx-tools)"
CACHE = os.path.join(DATA_DIR, "lookup_cache.db")
HAMQTH_URL = "https://www.hamqth.com/api/v2/"
CHECK_EVERY = 24 * 3600      # "Weekly update": ask the FCC once a day whether a new file is out
CTY = os.path.join(WSJTX_DIR, "cty.dat")
ADIF = os.path.join(WSJTX_DIR, "wsjtx_log.adi")
MAGIC = 0xADBCCBDA
MAX_ROWS = 300
# First start: wide enough for both lists with all their columns and scroll bars
DEFAULT_SIZE = "1300x500"
FALLBACK_HOME = ("United States", "NA", "K")   # when MyCall is not set or not in cty.dat
CONTINENTS = {"NA": "North America", "SA": "South America", "EU": "Europe", "AF": "Africa",
              "AS": "Asia", "OC": "Oceania", "AN": "Antarctica"}
LOCAL_MODES = {"country": "My country", "continent": "My continent"}

BANDS = [(1.8, 2.0, "160m"), (3.5, 4.0, "80m"), (5.25, 5.45, "60m"), (7.0, 7.3, "40m"),
         (10.1, 10.15, "30m"), (14.0, 14.35, "20m"), (18.068, 18.168, "17m"),
         (21.0, 21.45, "15m"), (24.89, 24.99, "12m"), (28.0, 29.7, "10m"),
         (50.0, 54.0, "6m"), (144.0, 148.0, "2m"), (0.472, 0.479, "630m"),
         (0.1357, 0.1378, "2200m")]


def band_of(mhz):
    for lo, hi, name in BANDS:
        if lo <= mhz <= hi:
            return name
    return ""


# ----------------------------------------------------------------------------- cty.dat

class Cty:
    def __init__(self, path):
        self.prefix = {}
        self.exact = {}
        try:
            text = open(path, encoding="latin-1").read()
        except OSError:
            return
        for block in text.split(";"):
            block = block.strip()
            if not block:
                continue
            head, _, body = block.partition("\n")
            f = [x.strip() for x in head.split(":")]
            if len(f) < 8 or f[0].startswith("*"):      # "*" = WAE only, not DXCC
                continue
            ent = (f[0], f[3], f[7].lstrip("*"))          # name, continent, main prefix
            self.prefix[f[7].lstrip("*")] = ent
            for tok in body.replace("\n", "").split(","):
                tok = re.sub(r"\(.*?\)|\[.*?\]|<.*?>|\{.*?\}|~.*?~", "", tok.strip())
                if not tok:
                    continue
                if tok.startswith("="):
                    self.exact[tok[1:].upper()] = ent
                else:
                    self.prefix[tok.upper()] = ent

    def lookup(self, call):
        call = call.upper().strip("<>")
        if call in self.exact:
            return self.exact[call]
        parts = [p for p in call.split("/") if p and p not in
                 ("P", "M", "MM", "AM", "QRP", "A", "R", "LH") and not p.isdigit()]
        if not parts:
            return None
        if len(parts) >= 2:
            base = min(parts[:2], key=len)              # VE3/W4MMP, W4MMP/KH6 -> the prefix part
        else:
            base = parts[0]
        for n in range(len(base), 0, -1):
            e = self.prefix.get(base[:n])
            if e:
                return e
        return None


# ----------------------------------------------------------------------------- worked before

class Worked:
    def __init__(self, cty):
        self.cty = cty
        self.call_band = set()
        self.ent = set()
        self.ent_band = set()
        try:
            data = open(ADIF, encoding="latin-1").read()
        except OSError:
            return
        for rec in re.split(r"<eor>", data, flags=re.I):
            c = re.search(r"<call:(\d+)[^>]*>", rec, re.I)
            b = re.search(r"<band:(\d+)[^>]*>", rec, re.I)
            if not c:
                continue
            call = rec[c.end():c.end() + int(c.group(1))]
            band = rec[b.end():b.end() + int(b.group(1))] if b else ""
            self.add(call, band)

    def add(self, call, band):
        call = call.upper().strip()
        band = band.lower().strip()
        self.call_band.add((call, band))
        e = self.cty.lookup(call)
        if e:
            self.ent.add(e[0])
            self.ent_band.add((e[0], band))


# ----------------------------------------------------------------------------- US callbook (FCC)

def base_call(call):
    """W1ABC/P, KH6/W1ABC, W1ABC/4 -> W1ABC (the longest part is the station's own call)."""
    parts = [p for p in call.upper().strip("<>").split("/") if p]
    return max(parts, key=len) if parts else ""


def fcc_date(mmddyyyy):
    """FCC dates are MM/DD/YYYY; return YYYYMMDD so they sort."""
    d = mmddyyyy.strip()
    return d[6:10] + d[0:2] + d[3:5] if len(d) == 10 else ""


class Callbook:
    """Name and state for US calls, from the FCC's weekly amateur licence file.

    The file (l_amat.zip, about 190 MB) is downloaded and built into a small SQLite
    file in a background thread. The new file is built next to the old one and
    swapped in by the window's own thread, so lookups keep working meanwhile.
    """

    def __init__(self):
        self.db = None
        self.meta = {}
        self.cache = {}
        self.msg = ""            # status text, set by the update thread
        self.busy = False
        self.ready = False       # a new file is waiting to be swapped in
        self.open()

    def open(self):
        self.cache.clear()
        self.db, self.meta = None, {}
        if not os.path.exists(CALLBOOK):
            return
        try:
            self.db = sqlite3.connect(CALLBOOK)
            self.meta = dict(self.db.execute("SELECT key, value FROM meta"))
        except sqlite3.Error:
            self.db, self.meta = None, {}

    def get(self, call):
        """(name, state) or None."""
        b = base_call(call)
        if b in self.cache:
            return self.cache[b]
        rec = None
        if self.db:
            try:
                rec = self.db.execute("SELECT name, state FROM us_callbook WHERE callsign = ?",
                                      (b,)).fetchone()
            except sqlite3.Error:
                pass
        if len(self.cache) > 20000:
            self.cache.clear()
        self.cache[b] = rec
        return rec

    def summary(self):
        if self.msg:
            return self.msg
        if not self.db:
            return "US callbook: not built yet"
        return "US callbook: %s calls, FCC file of %s" % (
            "{:,}".format(int(self.meta.get("count", 0))), self.meta.get("data_date", "?"))

    # -- runs in the window's thread
    def start(self, force):
        if self.busy:
            return
        self.busy = True
        threading.Thread(target=self.update, args=(force,), daemon=True).start()

    def swap(self):
        """Put the newly built file in place of the old one."""
        self.ready = False
        if self.db:
            self.db.close()        # Windows can't replace a file that is open
        try:
            os.replace(CALLBOOK + ".new", CALLBOOK)
        except OSError as e:
            self.msg = "US callbook: could not replace the old file (%s)" % e
        self.open()

    # -- runs in the update thread
    def update(self, force):
        zpath = os.path.join(DATA_DIR, "l_amat.zip")
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            self.msg = "US callbook: asking the FCC for news ..."
            req = urllib.request.Request(FCC_URL, method="HEAD",
                                         headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                modified = r.headers.get("Last-Modified", "")
            if not force and self.db and modified and modified == self.meta.get("fcc_modified"):
                self.msg = ""      # nothing new: back to the normal status
                return
            self.download(zpath)
            self.build(zpath, CALLBOOK + ".new", modified)
            self.msg = ""
            self.ready = True
        except Exception as e:     # any failure: say so in the status line, never stop the window
            self.msg = "US callbook: update failed (%s)" % (str(e)[:80] or type(e).__name__)
        finally:
            for p in (zpath,):
                try:
                    os.remove(p)
                except OSError:
                    pass
            self.busy = False

    def download(self, path):
        """Download with resume: the FCC server often drops the connection part way."""
        try:
            os.remove(path)        # never resume onto a file from an earlier update
        except OSError:
            pass
        have, total, fails = 0, None, 0
        while total is None or have < total:
            req = urllib.request.Request(FCC_URL, headers={"User-Agent": USER_AGENT,
                                                           "Range": "bytes=%d-" % have})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    if r.status == 206:      # the rest of the file
                        total = int(r.headers.get("Content-Range", "").rsplit("/", 1)[-1])
                        mode = "ab"
                    else:                    # the whole file again
                        total = int(r.headers.get("Content-Length") or 0) or None
                        have, mode = 0, "wb"
                    with open(path, mode) as f:
                        while True:
                            chunk = r.read(1 << 16)
                            if not chunk:
                                break
                            f.write(chunk)
                            have += len(chunk)
                            if total:
                                self.msg = "US callbook: downloading %d%%" % (have * 100 // total)
                if total is None:
                    break                    # size unknown: take what came
            except (OSError, ValueError) as e:
                fails += 1
                if fails > 30:
                    raise OSError("download keeps failing: %s" % e)
                time.sleep(5)
        if not zipfile.is_zipfile(path):
            raise OSError("the downloaded file is not a zip file")

    def build(self, zpath, out, modified):
        for p in (out, out + "-journal"):
            if os.path.exists(p):
                os.remove(p)
        z = zipfile.ZipFile(zpath)
        data_date = "%04d-%02d-%02d" % z.getinfo("HD.dat").date_time[:3]

        def rows(name, step):
            self.msg = "US callbook: building, step %d of 4 ..." % step
            with z.open(name) as f:
                for line in io.TextIOWrapper(f, encoding="latin-1", errors="replace"):
                    yield line.rstrip("\r\n").split("|")

        db = sqlite3.connect(out)
        db.execute("PRAGMA journal_mode = OFF")
        db.execute("PRAGMA synchronous = OFF")
        db.execute("PRAGMA temp_store = FILE")   # keep memory use small on a small Pi
        db.executescript("""
            CREATE TABLE us_callbook (callsign TEXT PRIMARY KEY, first_name TEXT,
                last_name TEXT, name TEXT, city TEXT, state TEXT, zip TEXT,
                op_class TEXT, grant_date TEXT);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TEMP TABLE hd (usi INTEGER PRIMARY KEY, call TEXT, grant_date TEXT);
            CREATE TEMP TABLE en (usi INTEGER PRIMARY KEY, first TEXT, last TEXT, name TEXT,
                city TEXT, state TEXT, zip TEXT);
            CREATE TEMP TABLE am (usi INTEGER PRIMARY KEY, cls TEXT);
        """)
        # HD = licence header: active licences only (A); field 2 joins the files
        db.executemany("INSERT OR REPLACE INTO hd VALUES (?, ?, ?)",
                       ((int(r[1]), r[4].upper(), fcc_date(r[7])) for r in rows("HD.dat", 1)
                        if len(r) > 8 and r[5] == "A" and r[1].isdigit() and r[4]))

        def en_rows():
            for r in rows("EN.dat", 2):
                if len(r) > 18 and r[5] == "L" and r[1].isdigit():   # L = the licensee
                    first, last = r[8].strip().title(), r[10].strip().title()
                    # clubs have no first / last name: keep their name as the FCC has it
                    name = " ".join(x for x in (first, last) if x) or r[7].strip()
                    yield (int(r[1]), first, last, name, r[16].strip().title(),
                           r[17].strip().upper(), r[18].strip())
        db.executemany("INSERT OR REPLACE INTO en VALUES (?, ?, ?, ?, ?, ?, ?)", en_rows())
        db.executemany("INSERT OR REPLACE INTO am VALUES (?, ?)",
                       ((int(r[1]), r[5].strip()) for r in rows("AM.dat", 3)
                        if len(r) > 5 and r[1].isdigit()))
        self.msg = "US callbook: building, step 4 of 4 ..."
        # More than one active licence for a call: the newest grant wins
        # (SQLite takes the other columns from the row that has the MAX).
        db.executescript("""
            CREATE TEMP TABLE pick AS
                SELECT usi, call, MAX(grant_date) AS grant_date FROM hd GROUP BY call;
            INSERT INTO us_callbook
                SELECT p.call, e.first, e.last, e.name, e.city, e.state, e.zip,
                       COALESCE(a.cls, ''), p.grant_date
                FROM pick p JOIN en e ON e.usi = p.usi LEFT JOIN am a ON a.usi = p.usi;
        """)
        count = db.execute("SELECT COUNT(*) FROM us_callbook").fetchone()[0]
        db.executemany("INSERT INTO meta VALUES (?, ?)",
                       (("data_date", data_date), ("fcc_modified", modified),
                        ("built", time.strftime("%Y-%m-%d")), ("count", str(count))))
        db.commit()
        db.close()
        if count < 100000:
            os.remove(out)
            raise OSError("only %d calls in the FCC file, kept the old callbook" % count)


# ----------------------------------------------------------------------------- HamQTH (online)

class LoginError(Exception):
    pass


class HamQTH:
    """Names for calls the FCC file doesn't have, from HamQTH.com (free account needed).

    One background thread does the lookups, most important first, a few a second.
    Every answer goes into a cache file (lookup_cache.db), so each station is asked
    for only once a month; "not found" is asked again after a week.
    """
    FOUND_DAYS, MISSING_DAYS = 30, 7

    def __init__(self):
        self.user, self.password, self.on = "", "", False
        self.names = {}          # call -> (name, state, found, fetched, city); window thread
        self.pending = {}        # call -> [priority, last decoded]; shared, under lock
        self.inflight = set()
        self.lock = threading.Lock()
        self.results = queue.Queue()     # (call, row) for the window thread
        self.wake = threading.Event()
        self.token, self.token_until = None, 0.0
        self.msg = ""
        self.stopped = False     # wrong password: wait for new settings
        self.load()
        threading.Thread(target=self.run, daemon=True).start()

    def connect(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        db = sqlite3.connect(CACHE)
        db.execute("CREATE TABLE IF NOT EXISTS lookup_cache (callsign TEXT PRIMARY KEY, "
                   "source TEXT, name TEXT, state TEXT, found INTEGER, fetched_at REAL, qth TEXT)")
        try:
            db.execute("ALTER TABLE lookup_cache ADD COLUMN qth TEXT")   # cache from 1.6.0 tests
        except sqlite3.Error:
            pass                                                         # already there
        return db

    def load(self):
        try:
            db = self.connect()
            now = time.time()
            for call, name, state, found, fetched, qth in db.execute(
                    "SELECT callsign, name, state, found, fetched_at, qth FROM lookup_cache"):
                row = (name or "", state or "", found, fetched, qth)
                if self.fresh(row, now):
                    self.names[call] = row
            db.close()
        except (OSError, sqlite3.Error):
            pass

    def fresh(self, row, now):
        if row[2] and row[4] is None:     # stored before the city was kept: ask again
            return False
        days = self.FOUND_DAYS if row[2] else self.MISSING_DAYS
        return now - row[3] < days * 86400

    # -- window thread
    def get(self, call):
        row = self.names.get(call)
        return row if row and self.fresh(row, time.time()) else None

    def want(self, call, prio):
        """Ask for a lookup; 0 = calling me, 1 = new country / band, 2 = the rest."""
        with self.lock:
            if call in self.inflight:
                return
            p = self.pending.get(call)
            if p:
                p[0], p[1] = min(p[0], prio), time.time()
            else:
                self.pending[call] = [prio, time.time()]
        self.wake.set()

    def configure(self, user, password, on):
        self.user, self.password, self.on = user, password, on
        self.token, self.stopped, self.msg = None, False, ""
        self.wake.set()

    def summary(self):
        if not self.on:
            return "HamQTH: off"
        if not self.user or not self.password:
            return "HamQTH: no login set"
        return self.msg or "HamQTH: %s names" % "{:,}".format(
            sum(1 for r in self.names.values() if r[2]))

    # -- lookup thread
    def run(self):
        db = None
        while True:
            self.wake.wait(5)
            self.wake.clear()
            while self.on and not self.stopped and self.user and self.password:
                with self.lock:
                    now = time.time()
                    for c, (prio, seen) in list(self.pending.items()):
                        if prio > 0 and now - seen > 35:      # gone from the band: forget it
                            del self.pending[c]
                    if not self.pending:
                        break
                    call = min(self.pending,
                               key=lambda c: (self.pending[c][0], -self.pending[c][1]))
                    del self.pending[call]
                    self.inflight.add(call)
                try:
                    name, state, found, city = self.lookup(call)
                    row = (name, state, found, time.time(), city)
                    try:
                        db = db or self.connect()
                        db.execute("INSERT OR REPLACE INTO lookup_cache (callsign, source, name, "
                                   "state, found, fetched_at, qth) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   (call, "hamqth") + row)
                        db.commit()
                    except (OSError, sqlite3.Error):
                        pass
                    self.results.put((call, row))
                    self.msg = ""
                    time.sleep(0.3)                           # a few a second at most
                except LoginError as e:
                    self.stopped, self.msg = True, str(e)
                except Exception as e:                        # no internet, HamQTH down ...
                    # don't ask for this call again for an hour; wait a minute
                    self.results.put((call, ("", "", 0,
                                             time.time() - self.MISSING_DAYS * 86400 + 3600, "")))
                    self.msg = "HamQTH: no answer (%s), trying again in a minute" % (
                        str(e)[:60] or type(e).__name__)
                    time.sleep(60)
                finally:
                    with self.lock:
                        self.inflight.discard(call)

    def login(self):
        body = json.dumps({"callsign": self.user, "password": self.password}).encode()
        req = urllib.request.Request(HAMQTH_URL + "login", data=body, headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                j = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (401, 422):
                raise LoginError("HamQTH: wrong callsign or password (click HamQTH)")
            if e.code == 429:
                raise LoginError("HamQTH: too many wrong passwords, wait and try again later")
            raise
        self.token = j["token"]
        self.token_until = time.time() + int(j.get("expiresIn", 3600))

    def lookup(self, call):
        """(name, state, 1, city) or ("", "", 0, "") when HamQTH has no record."""
        for attempt in (1, 2):
            if not self.token or time.time() > self.token_until - 300:
                self.login()                                  # a login lasts one hour
            url = "%scallbook/%s?app=decode-split" % (HAMQTH_URL, urllib.parse.quote(call))
            req = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + self.token, "Accept": "application/json",
                "User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    j = json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return "", "", 0, ""
                if e.code == 401 and attempt == 1:            # login ran out early
                    self.token = None
                    continue
                raise
            name = j.get("name") or (j.get("address") or {}).get("name") or j.get("nick") or ""
            state = (j.get("region") or {}).get("usState") or ""
            city = j.get("qth") or (j.get("address") or {}).get("city") or ""
            return name.strip(), state.strip(), 1, city.strip()
        return "", "", 0, ""


# ----------------------------------------------------------------------------- WSJT-X UDP

class Reader:
    def __init__(self, data):
        self.d = data
        self.p = 0

    def take(self, fmt):
        v = struct.unpack_from(">" + fmt, self.d, self.p)
        self.p += struct.calcsize(">" + fmt)
        return v[0]

    def u32(self):
        return self.take("I")

    def i32(self):
        return self.take("i")

    def u64(self):
        return self.take("Q")

    def dbl(self):
        return self.take("d")

    def bool(self):
        return self.take("B") != 0

    def utf8(self):
        n = self.u32()
        if n == 0xFFFFFFFF:
            return ""
        s = self.d[self.p:self.p + n].decode("utf-8", "replace")
        self.p += n
        return s


def put_utf8(s):
    b = s.encode("utf-8")
    return struct.pack(">I", len(b)) + b


GRID = re.compile(r"^[A-R]{2}[0-9]{2}$")
CALLISH = re.compile(r"^[A-Z0-9/<>]*[0-9][A-Z0-9/<>]*$")


def parse_message(msg):
    """Return (sender call, grid, is_cq) for an FT8 / FT4 style message."""
    t = msg.upper().split()
    if not t:
        return "", "", False
    grid = t[-1] if GRID.match(t[-1]) and t[-1] != "RR73" else ""
    if t[0] == "CQ" or t[0].startswith("CQ_"):
        rest = t[1:]
        if len(rest) >= 2 and not CALLISH.match(rest[0]):      # CQ DX / CQ POTA / CQ NA ...
            rest = rest[1:]
        elif len(rest) >= 2 and rest[0].isdigit():              # CQ 145 ...
            rest = rest[1:]
        return (rest[0] if rest else ""), grid, True
    if len(t) >= 2:
        return t[1], grid, False
    return "", grid, False


# ----------------------------------------------------------------------------- window

def tree_cols(tree):
    shown = tree.cget("displaycolumns")
    return tree.cget("columns") if shown in ("#all", ("#all",)) else shown


BG = {"cq": "#c8f0c8", "newband": "#ffd59a", "new": "#ff9a9a", "me": "#fff27a"}
WORKED_FG = "#909090"
LEGEND = [("CQ", BG["cq"], "black"), ("New country", BG["new"], "black"),
          ("New on band", BG["newband"], "black"), ("My call", BG["me"], "black"),
          ("Worked on band", "white", WORKED_FG)]


class App:
    def __init__(self, root):
        self.root = root
        self.cty = Cty(CTY)
        self.worked = Worked(self.cty)
        self.mycall = self.read_mycall()
        self.home = self.cty.lookup(self.mycall) if self.mycall else None
        self.home_found = self.home is not None
        self.home = self.home or FALLBACK_HOME
        self.band = ""
        self.mode = ""
        self.wsjtx_addr = None
        self.wsjtx_id = "WSJT-X"
        self.rows = {}           # tree item -> decode dict
        self.store = []          # recent decodes, for re-filtering
        self.last_packet = 0.0
        self.cq_only = tk.BooleanVar(value=False)
        self.new_only = tk.BooleanVar(value=False)
        self.hide_worked = tk.BooleanVar(value=False)
        self.per_period = tk.BooleanVar(value=True)
        self.by_db = tk.BooleanVar(value=False)
        self.weekly = tk.BooleanVar(value=True)
        self.callbook = Callbook()
        self.hamqth = HamQTH()
        self.seen = {}           # call -> (decodes, first seen), for "decoded twice"
        self.last_check = 0.0    # when "Weekly update" last asked the FCC
        self.local_mode = "country"
        self.period_ms = None    # time stamp of the decodes now shown

        root.title("Decode split")
        self.load_settings()
        style = ttk.Style()
        style.configure("Treeview", font=FONT, rowheight=20)
        style.configure("Treeview.Heading", font=HEAD_FONT)

        top = ttk.Frame(root, padding=4)
        top.pack(fill="x")
        self.status = ttk.Label(top, text="waiting for WSJT-X ...")
        self.status.pack(side="left")
        ttk.Button(top, text="Clear", command=self.clear).pack(side="right")
        self.local_box = ttk.Combobox(top, values=list(LOCAL_MODES.values()), state="readonly",
                                      width=12)
        self.local_box.set(LOCAL_MODES[self.local_mode])
        self.local_box.bind("<<ComboboxSelected>>", self.set_local)
        self.local_box.pack(side="right", padx=(0, 6))
        ttk.Label(top, text="Local:").pack(side="right", padx=(6, 2))

        opts = ttk.Frame(root, padding=(4, 0, 4, 4))
        opts.pack(fill="x")
        for text, var, cmd in (("CQ / 73 only", self.cq_only, self.refilter),
                               ("New countries only", self.new_only, self.refilter),
                               ("Hide worked", self.hide_worked, self.refilter),
                               ("Clear each period", self.per_period, None),
                               ("Sort by dB", self.by_db, self.refilter)):
            ttk.Checkbutton(opts, text=text, variable=var, command=cmd
                            ).pack(side="left", padx=(0, 12))

        cb = ttk.Frame(root, padding=(4, 0, 4, 4))
        cb.pack(fill="x")
        ttk.Button(cb, text="Update US callbook",
                   command=lambda: self.callbook.start(force=True)).pack(side="left")
        ttk.Checkbutton(cb, text="Weekly update", variable=self.weekly
                        ).pack(side="left", padx=(8, 12))
        self.cb_status = ttk.Label(cb, text=self.callbook.summary())
        self.cb_status.pack(side="left")
        self.hq_status = ttk.Label(cb, text=self.hamqth.summary())
        self.hq_status.pack(side="right")
        ttk.Button(cb, text="HamQTH", command=self.hamqth_dialog
                   ).pack(side="right", padx=(12, 6))

        legend = ttk.Frame(root, padding=(4, 0, 4, 4))
        legend.pack(fill="x")
        for text, bg, fg in LEGEND:
            tk.Label(legend, text=" %s " % text, bg=bg, fg=fg, font=HEAD_FONT,
                     relief="solid", borderwidth=1).pack(side="left", padx=(0, 6))

        pane = ttk.PanedWindow(root, orient="horizontal")
        pane.pack(fill="both", expand=True)
        self.pane, self.pane_w = pane, 0
        # local and DX lists equal: the divider goes back to the middle when the window
        # changes width (dragging it by hand still works until then)
        pane.bind("<Configure>", self.even_split)
        self.us = self.make_tree(pane, "", ("utc", "db", "call", "grid", "country", "name",
                                            "state", "msg"),
                                 ("UTC", "dB", "Call", "Grid", "Country", "Name", "State",
                                  "Message"),
                                 (55, 35, 80, 50, 110, 100, 40, 200))
        # no State column for DX: a US state hardly ever shows up there
        self.dx = self.make_tree(pane, "DX", ("utc", "db", "call", "country", "cont", "name",
                                              "msg"),
                                 ("UTC", "dB", "Call", "Country", "", "Name", "Message"),
                                 (55, 35, 80, 120, 30, 100, 200))

        self.show_local()
        self.open_socket()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.after(100, self.poll)
        root.after(1000, self.tick)

    def make_tree(self, pane, title, cols, heads, widths):
        frame = ttk.Labelframe(pane, text=title, padding=2)
        tree = ttk.Treeview(frame, columns=cols, show="headings")
        for c, h, w in zip(cols, heads, widths):
            tree.heading(c, text=h)
            if c == "db":
                tree.heading(c, command=self.toggle_sort)
            # only Message stretches: it gets whatever room the other columns leave
            tree.column(c, width=w, stretch=(c == "msg"),
                        anchor="e" if c == "db" else "w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, bg in BG.items():
            tree.tag_configure(tag, background=bg)
        tree.tag_configure("worked", foreground=WORKED_FG)
        tree.bind("<Double-1>", lambda e, t=tree: self.reply(t))
        pane.add(frame, weight=1)
        return tree

    # -- settings
    def read_mycall(self):
        try:
            for line in open(WSJTX_INI, encoding="utf-8", errors="replace"):
                if line.startswith("MyCall="):
                    return line.split("=", 1)[1].strip().upper()
        except OSError:
            pass
        return ""

    def load_settings(self):
        try:
            s = json.load(open(SETTINGS))
            self.root.geometry(s.get("geometry", DEFAULT_SIZE))
            self.cq_only.set(s.get("cq_only", False))
            self.new_only.set(s.get("new_only", False))
            self.hide_worked.set(s.get("hide_worked", False))
            self.per_period.set(s.get("per_period", True))
            self.by_db.set(s.get("by_db", False))
            self.weekly.set(s.get("weekly_callbook", True))
            if s.get("local") in LOCAL_MODES:
                self.local_mode = s["local"]
            self.hamqth.configure(s.get("hamqth_user", ""), s.get("hamqth_pass", ""),
                                  s.get("hamqth_on", False))
        except (OSError, ValueError):
            self.root.geometry(DEFAULT_SIZE)

    def save_settings(self):
        try:
            with open(SETTINGS, "w") as f:
                json.dump({"geometry": self.root.geometry(), "cq_only": self.cq_only.get(),
                           "new_only": self.new_only.get(), "per_period": self.per_period.get(),
                           "by_db": self.by_db.get(), "hide_worked": self.hide_worked.get(),
                           "local": self.local_mode, "weekly_callbook": self.weekly.get(),
                           "hamqth_user": self.hamqth.user, "hamqth_pass": self.hamqth.password,
                           "hamqth_on": self.hamqth.on}, f)
            if not WINDOWS:
                os.chmod(SETTINGS, 0o600)        # it holds the HamQTH password
        except OSError:
            pass

    def quit(self):
        self.save_settings()
        self.root.destroy()

    def hamqth_dialog(self):
        w = tk.Toplevel(self.root)
        w.title("HamQTH")
        w.transient(self.root)
        w.resizable(False, False)
        f = ttk.Frame(w, padding=12)
        f.pack()
        user = tk.StringVar(value=self.hamqth.user)
        pw = tk.StringVar(value=self.hamqth.password)
        on = tk.BooleanVar(value=self.hamqth.on)
        ttk.Label(f, text="Names for DX calls (and US calls the FCC file doesn't have)\n"
                          "come from HamQTH.com. You need a free account there."
                  ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(f, text="Callsign").grid(row=1, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(f, textvariable=user, width=20).grid(row=1, column=1, sticky="w")
        ttk.Label(f, text="Password").grid(row=2, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(f, textvariable=pw, width=20, show="*").grid(row=2, column=1, sticky="w")
        ttk.Checkbutton(f, text="Use HamQTH", variable=on).grid(row=3, column=1, sticky="w",
                                                                pady=(4, 0))
        ttk.Label(f, text="Data provided by HamQTH.com").grid(row=4, column=0, columnspan=2,
                                                              sticky="w", pady=(10, 0))

        def save():
            self.hamqth.configure(user.get().strip().upper(), pw.get(), on.get())
            self.save_settings()
            w.destroy()
        b = ttk.Frame(f)
        b.grid(row=5, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(b, text="Save", command=save).pack(side="left", padx=(0, 6))
        ttk.Button(b, text="Cancel", command=w.destroy).pack(side="left")
        w.grab_set()

    # -- UDP
    def read_udp(self):
        """WSJT-X's UDP Server address and port, from its settings file."""
        host, port = HOST, PORT
        try:
            for line in open(WSJTX_INI, encoding="utf-8", errors="replace"):
                key, _, val = line.strip().partition("=")
                if key == "UDPServer" and val and not val.startswith("@"):
                    host = val
                elif key == "UDPServerPort" and val.isdigit():
                    port = int(val)
        except OSError:
            pass
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:            # a name such as "localhost"
            try:
                ip = ipaddress.ip_address(socket.gethostbyname(host))
            except (OSError, ValueError):
                ip = ipaddress.ip_address(HOST)
        if ip.version != 4:
            ip = ipaddress.ip_address(HOST)   # IPv6 not handled: use the default
        return str(ip), port, ip.is_multicast

    def open_socket(self):
        host, port, multicast = self.read_udp()
        self.udp_text = "%s %s:%d" % ("multicast" if multicast else "unicast", host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if multicast:
            # Sharing is the point of multicast. Not for unicast: on Linux it would let a
            # second program onto the port with no error, and only one of them gets the data.
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        elif WINDOWS:
            # Windows' own "this port is mine": a second program can't take it over.
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            # Windows can't bind to a group address: take the port, then join the group.
            self.sock.bind(("" if (multicast and WINDOWS) else host, port))
            if multicast:
                # WSJT-X sends multicast on the loop-back interface unless other
                # interfaces are picked; join there and on the default interface.
                joined = False
                for iface in ("127.0.0.1", "0.0.0.0"):
                    mreq = struct.pack("4s4s", socket.inet_aton(host), socket.inet_aton(iface))
                    try:
                        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                        joined = True
                    except OSError:
                        pass
                if not joined:
                    raise OSError("could not join group %s" % host)
            self.status.config(text="waiting for WSJT-X on %s ..." % self.udp_text)
        except OSError as e:
            txt = "%s: %s" % (self.udp_text, e)
            if not multicast:
                txt += " - another program has the port? Set WSJT-X UDP Server to 224.0.0.1 to share it"
            self.status.config(text=txt)
        self.sock.setblocking(False)

    def maybe_lookup(self, d):
        """Queue a HamQTH lookup, but only for stations that matter (keeps it small
        on a busy band): calling CQ, calling me, or a new country / new on the band.
        A station must be decoded twice first (drops false decodes), unless it calls me."""
        if not self.hamqth.on or not d["call"] or d["name"]:
            return
        key = base_call(d["call"])
        if not key or not CALLISH.match(key) or "." in key or self.hamqth.get(key):
            return
        me = "me" in d["tags"]
        alert = bool({"new", "newband"} & set(d["tags"]))
        if not (me or alert or d["cq"]):
            return
        now = time.time()
        n, first = self.seen.get(key, (0, now))
        if now - first > 600:
            n, first = 0, now
        self.seen[key] = (n + 1, first)
        if len(self.seen) > 5000:
            self.seen = {k: v for k, v in self.seen.items() if now - v[1] < 600}
        if me:
            self.hamqth.want(key, 0)
        elif n + 1 >= 2:
            self.hamqth.want(key, 1 if alert else 2)

    def take_lookups(self):
        """Answers from the HamQTH thread: fill in the rows already on screen."""
        while True:
            try:
                call, row = self.hamqth.results.get_nowait()
            except queue.Empty:
                return
            self.hamqth.names[call] = row
            if not row[2]:
                continue
            for d in self.store:
                if d["call"] and not d["name"] and base_call(d["call"]) == call:
                    d["name"], d["state"] = row[0], row[1]
            for item, d in self.rows.items():
                if d["call"] and base_call(d["call"]) == call:
                    tree = self.us if self.us.exists(item) else self.dx
                    tree.set(item, "name", d["name"])
                    if tree is self.us:
                        tree.set(item, "state", d["state"])

    def poll(self):
        self.take_lookups()
        for _ in range(200):
            try:
                data, addr = self.sock.recvfrom(4096)
            except (BlockingIOError, OSError):
                break
            try:
                self.handle(data, addr)
            except (struct.error, IndexError):
                pass
        self.root.after(100, self.poll)

    def handle(self, data, addr):
        r = Reader(data)
        if r.u32() != MAGIC:
            return
        r.u32()                       # schema
        kind = r.u32()
        ident = r.utf8()
        self.wsjtx_addr = addr
        self.wsjtx_id = ident
        self.last_packet = time.time()
        if kind == 1:                 # status
            mhz = r.u64() / 1e6
            self.mode = r.utf8()
            b = band_of(mhz)
            if b != self.band:
                self.band = b
                self.recolour()
            self.dial = mhz
        elif kind == 2:               # decode
            new = r.bool()
            ms = r.u32()
            snr = r.i32()
            dt = r.dbl()
            df = r.u32()
            mode = r.utf8()
            msg = r.utf8()
            low = r.bool()
            self.add_decode(dict(ms=ms, snr=snr, dt=dt, df=df, mode=mode, msg=msg, low=low))
        elif kind == 3:               # clear: WSJT-X Erase button
            # window 0 = Band Activity, 1 = Rx Frequency, 2 = both; older WSJT-X sends none
            window = r.take("B") if r.p < len(r.d) else 0
            if window in (0, 2):
                self.clear()
        elif kind == 5:               # QSO logged
            r.take("q"); r.u32(); r.take("B")      # QDateTime off (julian day, ms, spec)
            call = r.utf8()
            r.utf8()                               # grid
            mhz = r.u64() / 1e6
            self.worked.add(call, band_of(mhz))
            self.recolour()

    # -- rows
    def classify(self, d):
        call, grid, cq = parse_message(d["msg"])
        ent = self.cty.lookup(call) if call else None
        last = d["msg"].upper().split()[-1:] or [""]
        rec = self.callbook.get(call) if call else None
        if not rec and call:
            h = self.hamqth.get(base_call(call))
            if h and h[2]:
                rec = (h[0], h[1])
        d.update(call=call, grid=grid, cq=cq, ent=ent, end73=last[0] in ("73", "RR73"),
                 name=rec[0] if rec else "", state=rec[1] if rec else "")
        me = bool(self.mycall) and self.mycall in d["msg"].upper().split()
        if me:
            tags = ["me"]
        elif ent and ent[0] not in self.worked.ent:
            tags = ["new"]
        elif ent and (ent[0], self.band) not in self.worked.ent_band:
            tags = ["newband"]
        elif cq:
            tags = ["cq"]
        else:
            tags = []
        if call and (call.strip("<>"), self.band) in self.worked.call_band:
            tags.append("worked")
        d["tags"] = tuple(tags)
        return d

    def shown(self, d):
        if self.cq_only.get() and not (d["cq"] or d["end73"] or "me" in d["tags"]):
            return False
        if self.new_only.get() and not ({"new", "newband", "me"} & set(d["tags"])):
            return False
        if self.hide_worked.get() and "worked" in d["tags"] and "me" not in d["tags"]:
            return False
        return True

    def add_decode(self, d):
        # All decodes of one period carry the same time stamp: a new stamp = a new period.
        if self.per_period.get() and self.period_ms is not None and d["ms"] != self.period_ms:
            self.clear()
        self.period_ms = d["ms"]
        d = self.classify(d)
        self.maybe_lookup(d)
        d["us"] = self.is_local(d)
        self.store.append(d)
        del self.store[:-MAX_ROWS * 2]
        if self.shown(d):
            self.insert(d)

    def insert(self, d):
        utc = "%02d%02d%02d" % (d["ms"] // 3600000, d["ms"] // 60000 % 60, d["ms"] // 1000 % 60)
        if d["us"]:
            tree = self.us
            name = d["ent"][0] if d["ent"] else "?"
            vals = (utc, d["snr"], d["call"], d["grid"], name, d["name"], d["state"], d["msg"])
        else:
            tree = self.dx
            name, cont = (d["ent"][0], d["ent"][1]) if d["ent"] else ("?", "")
            vals = (utc, d["snr"], d["call"], name, cont, d["name"], d["msg"])
        pos = 0
        if self.by_db.get():
            # strongest on top; a new decode goes below others with the same dB
            kids = tree.get_children()
            pos = len(kids)
            for i, k in enumerate(kids):
                if self.rows[k]["snr"] < d["snr"]:
                    pos = i
                    break
        item = tree.insert("", pos, values=vals, tags=d["tags"])
        self.rows[item] = d
        kids = tree.get_children()
        for old in kids[MAX_ROWS:]:
            self.rows.pop(old, None)
            tree.delete(old)

    def clear(self):
        for t in (self.us, self.dx):
            for i in t.get_children():
                t.delete(i)
        self.rows.clear()
        self.store.clear()

    def refilter(self):
        for t in (self.us, self.dx):
            for i in t.get_children():
                t.delete(i)
        self.rows.clear()
        for d in self.store:
            if self.shown(d):
                self.insert(d)

    # -- local = my country or my continent
    def is_local(self, d):
        if not d["ent"]:
            return False
        if self.local_mode == "continent":
            return d["ent"][1] == self.home[1]
        return d["ent"][0] == self.home[0]

    def show_local(self):
        if self.local_mode == "continent":
            title = CONTINENTS.get(self.home[1], self.home[1])
            cols = ("utc", "db", "call", "grid", "country", "name", "state", "msg")
        else:
            title = self.home[0]
            cols = ("utc", "db", "call", "grid", "name", "state", "msg")
        if not self.home_found:
            title += "  (MyCall not set in WSJT-X)" if not self.mycall else \
                "  (%s not found in cty.dat)" % self.mycall
        self.us.master.configure(text=title)
        self.us.configure(displaycolumns=cols)
        # give Message whatever room the other columns leave
        self.root.update_idletasks()
        for tree, shown in ((self.us, cols), (self.dx, tree_cols(self.dx))):
            fixed = sum(tree.column(c, "width") for c in shown if c != "msg")
            tree.column("msg", width=max(120, tree.winfo_width() - fixed - 4))

    def set_local(self, event=None):
        label = self.local_box.get()
        self.local_mode = [k for k, v in LOCAL_MODES.items() if v == label][0]
        self.local_box.selection_clear()
        for d in self.store:
            d["us"] = self.is_local(d)
        self.show_local()
        self.refilter()

    def even_split(self, event=None):
        w = self.pane.winfo_width()
        if w > 1 and w != self.pane_w:
            self.pane_w = w
            self.pane.sashpos(0, w // 2)

    def toggle_sort(self):
        self.by_db.set(not self.by_db.get())
        self.refilter()

    def recolour(self):
        for d in self.store:
            self.classify(d)
        self.refilter()

    def tick(self):
        if self.last_packet:
            age = time.time() - self.last_packet
            txt = "%s  %s  %s   [%s]" % (self.band or "?", self.mode, self.mycall, self.udp_text)
            if age > 30:
                txt += "   (no word from WSJT-X for %d s)" % age
            self.status.config(text=txt)
        if self.callbook.ready:              # a rebuilt callbook is ready: use it
            self.callbook.swap()
            self.recolour()
        if self.weekly.get() and time.time() - self.last_check > CHECK_EVERY:
            self.last_check = time.time()
            self.callbook.start(force=False)
        self.cb_status.config(text=self.callbook.summary())
        self.hq_status.config(text=self.hamqth.summary())
        self.root.after(1000, self.tick)

    # -- double-click -> WSJT-X Reply
    def reply(self, tree):
        sel = tree.focus()
        d = self.rows.get(sel)
        if not d or not self.wsjtx_addr:
            return
        pkt = struct.pack(">III", MAGIC, 2, 4) + put_utf8(self.wsjtx_id)
        pkt += struct.pack(">IidI", d["ms"], d["snr"], d["dt"], d["df"])
        pkt += put_utf8(d["mode"]) + put_utf8(d["msg"])
        pkt += struct.pack(">BB", 1 if d["low"] else 0, 0)
        try:
            self.sock.sendto(pkt, self.wsjtx_addr)
        except OSError:
            pass


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
