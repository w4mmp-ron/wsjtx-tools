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
import json
import os
import re
import socket
import struct
import time
import tkinter as tk
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
    FONT, HEAD_FONT = ("Consolas", 10), ("Segoe UI", 10, "bold")
else:
    WSJTX_DIR = os.path.expanduser("~/.local/share/WSJT-X")
    WSJTX_INI = os.path.expanduser("~/.config/WSJT-X.ini")
    SETTINGS = os.path.expanduser("~/.config/decode-split.json")
    FONT, HEAD_FONT = ("DejaVu Sans Mono", 10), ("DejaVu Sans", 10, "bold")
CTY = os.path.join(WSJTX_DIR, "cty.dat")
ADIF = os.path.join(WSJTX_DIR, "wsjtx_log.adi")
MAGIC = 0xADBCCBDA
MAX_ROWS = 300
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

        legend = ttk.Frame(root, padding=(4, 0, 4, 4))
        legend.pack(fill="x")
        for text, bg, fg in LEGEND:
            tk.Label(legend, text=" %s " % text, bg=bg, fg=fg, font=HEAD_FONT,
                     relief="solid", borderwidth=1).pack(side="left", padx=(0, 6))

        pane = ttk.PanedWindow(root, orient="horizontal")
        pane.pack(fill="both", expand=True)
        self.us = self.make_tree(pane, "", ("utc", "db", "call", "grid", "country", "msg"),
                                 ("UTC", "dB", "Call", "Grid", "Country", "Message"),
                                 (60, 40, 90, 55, 120, 220))
        self.dx = self.make_tree(pane, "DX", ("utc", "db", "call", "country", "cont", "msg"),
                                 ("UTC", "dB", "Call", "Country", "", "Message"),
                                 (60, 40, 90, 140, 32, 220))

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
            tree.column(c, width=w, stretch=(c in ("msg", "country")),
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
            self.root.geometry(s.get("geometry", "1100x500"))
            self.cq_only.set(s.get("cq_only", False))
            self.new_only.set(s.get("new_only", False))
            self.hide_worked.set(s.get("hide_worked", False))
            self.per_period.set(s.get("per_period", True))
            self.by_db.set(s.get("by_db", False))
            if s.get("local") in LOCAL_MODES:
                self.local_mode = s["local"]
        except (OSError, ValueError):
            self.root.geometry("1100x500")

    def quit(self):
        try:
            json.dump({"geometry": self.root.geometry(), "cq_only": self.cq_only.get(),
                       "new_only": self.new_only.get(), "per_period": self.per_period.get(),
                       "by_db": self.by_db.get(), "hide_worked": self.hide_worked.get(),
                       "local": self.local_mode}, open(SETTINGS, "w"))
        except OSError:
            pass
        self.root.destroy()

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

    def poll(self):
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
        d.update(call=call, grid=grid, cq=cq, ent=ent, end73=last[0] in ("73", "RR73"))
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
            vals = (utc, d["snr"], d["call"], d["grid"], name, d["msg"])
        else:
            tree = self.dx
            name, cont = (d["ent"][0], d["ent"][1]) if d["ent"] else ("?", "")
            vals = (utc, d["snr"], d["call"], name, cont, d["msg"])
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
            cols = ("utc", "db", "call", "grid", "country", "msg")
        else:
            title = self.home[0]
            cols = ("utc", "db", "call", "grid", "msg")
        if not self.home_found:
            title += "  (MyCall not set in WSJT-X)" if not self.mycall else \
                "  (%s not found in cty.dat)" % self.mycall
        self.us.master.configure(text=title)
        self.us.configure(displaycolumns=cols)
        # give Message whatever room the other columns leave
        self.root.update_idletasks()
        fixed = sum(self.us.column(c, "width") for c in cols if c != "msg")
        self.us.column("msg", width=max(120, self.us.winfo_width() - fixed - 4))

    def set_local(self, event=None):
        label = self.local_box.get()
        self.local_mode = [k for k, v in LOCAL_MODES.items() if v == label][0]
        self.local_box.selection_clear()
        for d in self.store:
            d["us"] = self.is_local(d)
        self.show_local()
        self.refilter()

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
