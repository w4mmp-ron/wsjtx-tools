# wsjtx-tools

Two small helpers for WSJT-X on Linux (Raspberry Pi OS, Debian, Ubuntu):

- **hrdlog-upload**: sends each new contact in your WSJT-X log to HRDLog.net, a few seconds after WSJT-X logs it.
- **decode-split**: a window that shows the WSJT-X decodes in two lists, US calls and DX calls, with worked-before colours.

Both are plain Python 3 (with Tk for the window). They need nothing but WSJT-X on the same computer.

## Install

Download `wsjtx-tools_<version>_all.deb` from the [Releases](https://github.com/w4mmp-ron/wsjtx-tools/releases) page, then:

```
sudo apt install ./wsjtx-tools_1.0.1_all.deb
```

The same file works on any CPU (Pi, PC).

To remove it: `sudo apt remove wsjtx-tools`. Your settings in your home folder are kept.

---

## hrdlog-upload

```
systemctl --user enable --now hrdlog-upload
nano ~/.config/hrdlog-upload.conf
systemctl --user restart hrdlog-upload
tail -f ~/.local/state/hrdlog-upload/hrdlog-upload.log
```

1. The first command starts the service once for your user (not as root). It creates the config file, and marks the contacts already in your log as seen. Those are **not** sent, only new ones.
2. In the config file, fill in `callsign =` and `code =`. The code is the **upload code** on your HRDLog.net profile page (also in their sign-up e-mail). It is not your password.
3. Restart the service. After your next QSO the log says `sent <call>`.

The rest works by itself:
- **No network or HRDLog down:** the contacts wait and are sent later (every 5 minutes).
- **Wrong callsign or code:** the log says "HRDLog refused the login". Fix the config and restart; nothing is lost.
- **Already on HRDLog:** the log says "already on HRDLog"; nothing is sent twice.

The service starts when you log in. To have it start at boot even with nobody logged in:

```
sudo loginctl enable-linger $USER
```

Other commands:

```
hrdlog-upload --status      # callsign, how many contacts seen, how many waiting
hrdlog-upload --dry-run     # show what would be sent, send nothing
journalctl _SYSTEMD_USER_UNIT=hrdlog-upload.service
```

The config file holds:
- `files`: the ADIF files to watch, comma separated. The default is the WSJT-X log, `~/.local/share/WSJT-X/wsjtx_log.adi`. Other logs can be added, for example fldigi's `~/.fldigi/logs/logbook.adif`.
- `poll`: how many seconds between checks (default 10).

The seen list, the queue of contacts waiting to go, and the log are in `~/.local/state/hrdlog-upload/`.

**After an upgrade** of the package: `systemctl --user daemon-reload` and `systemctl --user restart hrdlog-upload`.

---

## decode-split

```
decode-split
```

Or from the menu: **Ham Radio, then Decode split (US / DX)**. It can be started before or after WSJT-X.

The window has two lists side by side:
- **US** (left): time, dB, call, grid and message.
- **DX** (right): the same with the country and continent. Alaska and Hawaii count as DX.

Colours:

| Colour | Meaning |
|---|---|
| green | CQ |
| red | a country you have never worked |
| orange | a country new on this band |
| yellow | your call is in the message |
| grey text | this call already worked on this band |

- **Double-click** a line: the same as a double-click in WSJT-X. It calls that station.
- **CQ / 73 only**: shows CQs and messages ending in 73 or RR73 (good for tail-ending; the call in the list is the station just finishing), plus anything with your call.
- **New countries only**: just the red and orange lines.
- **Clear each period** (on by default): the lists empty when the first decode of a new period arrives. Untick it to keep the history.
- **Clear**, or **Erase** under Band Activity in WSJT-X, empties both lists.

### WSJT-X settings it needs

File > Settings > Reporting:
- **UDP Server** `127.0.0.1`, **UDP Server port number** `2237` (the WSJT-X default).
- **Accept UDP requests** ticked (needed for the double-click).

Only one program can listen on that port. If the window says "port 2237 busy", another program (for example GridTracker or JTAlert) is using it.

The countries come from WSJT-X's own `cty.dat`, and worked-before from your WSJT-X log (`~/.local/share/WSJT-X/`). The window size and the tick boxes are kept in `~/.config/decode-split.json`.

---

## Build the .deb yourself

```
git clone https://github.com/w4mmp-ron/wsjtx-tools
cd wsjtx-tools
bash build-deb.sh
```

This makes `wsjtx-tools_<version>_all.deb` in the same folder. The version is in `packaging/DEBIAN/control`.

## How the HRDLog upload works

HRDLog.net has no published API page. This uses the same call as other logging programs (Wavelog, QLog, CQRLOG): a form POST to `https://robot.hrdlog.net/NewEntry.aspx` with `Callsign`, `Code` (the upload code), `App` and `ADIFData` (one ADIF record). HRDLog answers `<insert>1` (stored), `<insert>0` (already there) or an `<error>`.

## License

MIT, see [LICENSE](LICENSE).

73, W4MMP
