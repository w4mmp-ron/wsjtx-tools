# wsjtx-tools

Two small helpers for WSJT-X on Linux (Raspberry Pi OS, Debian, Ubuntu). decode-split also runs on Windows:

- **hrdlog-upload**: sends each new contact in your WSJT-X log to HRDLog.net, a few seconds after WSJT-X logs it.
- **decode-split**: a window that shows the WSJT-X decodes in two lists, US calls and DX calls, with worked-before colours.

Both are plain Python 3 (with Tk for the window). They need nothing but WSJT-X on the same computer.

## Install

Download `wsjtx-tools_<version>_all.deb` from the [Releases](https://github.com/w4mmp-ron/wsjtx-tools/releases) page, then:

```
sudo apt install ./wsjtx-tools_<version>_all.deb
```

The same file works on any CPU (Pi, PC).

To remove it: `sudo apt remove wsjtx-tools`. Your settings in your home folder are kept.

### Windows (decode-split only)

Download `decode-split.exe` from the same [Releases](https://github.com/w4mmp-ron/wsjtx-tools/releases) page, put it anywhere (the Desktop is fine) and double-click it. There is nothing to install and Python is not needed.

The first time, Windows may say it "protected your PC", because the file is not signed: click **More info**, then **Run anyway**.

To remove it, delete `decode-split.exe` and `%APPDATA%\decode-split.json`.

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

A row under the buttons shows the same colours as a legend.

- **Double-click** a line: the same as a double-click in WSJT-X. It calls that station.
- **CQ / 73 only**: shows CQs and messages ending in 73 or RR73 (good for tail-ending; the call in the list is the station just finishing), plus anything with your call.
- **New countries only**: just the red and orange lines.
- **Hide worked**: hides calls already worked on the current band (the grey ones). A message with your call in it is always shown.
- **Clear each period** (on by default): the lists empty when the first decode of a new period arrives. Untick it to keep the history.
- **Sort by dB**: strongest signal on top in both lists; clicking the dB column heading does the same. Off = newest on top.
- **Clear**, or **Erase** under Band Activity in WSJT-X, empties both lists.

### WSJT-X settings it needs

File > Settings > Reporting:
- **UDP Server** `127.0.0.1`, **UDP Server port number** `2237` (the WSJT-X default).
- **Accept UDP requests** ticked (needed for the double-click).

decode-split reads these from WSJT-X's settings when it starts, so there is nothing to set in decode-split itself. The top line of the window shows what it uses, for example `[unicast 127.0.0.1:2237]`.

With `127.0.0.1` only one program can listen. If the window says "Address already in use", another program (for example GridTracker or a logger) has the port. To run them together, use multicast.

### Sharing WSJT-X with other programs (multicast)

1. In WSJT-X, File > Settings > Reporting, set **UDP Server** to `224.0.0.1` and keep the port `2237`.
2. Under **Outgoing interfaces**, on Windows tick the **loopback** interface (it keeps the data on this PC). On Linux, tick your network interface (on a Raspberry Pi usually `eth0`, or `wlan0` on WiFi). If the list shows `lo`, tick that instead; it keeps the data on this computer. On many systems `lo` is not in the list, and with nothing ticked WSJT-X sends nothing. With `eth0` the data also goes onto your home network (not past your router), so programs on other computers there can listen too. Click OK.
3. **Close WSJT-X and start it again.** WSJT-X may not save the new address until it closes, and decode-split reads it from WSJT-X's settings file.
4. Close decode-split and start it again. The top line shows `[multicast 224.0.0.1:2237]` once decodes arrive.
5. Set the other programs to listen to the same address and port (look for "multicast" in their settings). A program that has no multicast setting can't share; it stops getting data when WSJT-X is on multicast.

Double-click still works: the reply goes straight back to WSJT-X. To go back, set UDP Server to `127.0.0.1` again, then restart WSJT-X and decode-split.

The countries come from WSJT-X's own `cty.dat`, and worked-before from your WSJT-X log (`~/.local/share/WSJT-X/wsjtx_log.adi`): a call counts as worked on a band when the log has a contact with that call on that band, any mode. Contacts you log while decode-split is running count straight away. The window size and the tick boxes are kept in `~/.config/decode-split.json`.

On Windows the same files are in `%LOCALAPPDATA%\WSJT-X\` (WSJT-X's settings, `cty.dat` and log), and decode-split keeps its own settings in `%APPDATA%\decode-split.json`.

---

## Build the .deb yourself

```
git clone https://github.com/w4mmp-ron/wsjtx-tools
cd wsjtx-tools
bash build-deb.sh
```

This makes `wsjtx-tools_<version>_all.deb` in the same folder. The version is in `packaging/DEBIAN/control`.

## Build the Windows .exe yourself

```
python -m pip install pyinstaller
build-exe.cmd
```

Run these in the `wsjtx-tools` folder, with Python 3 from python.org (it includes Tk). This makes `dist\decode-split.exe`.

## How the HRDLog upload works

HRDLog.net has no published API page. This uses the same call as other logging programs (Wavelog, QLog, CQRLOG): a form POST to `https://robot.hrdlog.net/NewEntry.aspx` with `Callsign`, `Code` (the upload code), `App` and `ADIFData` (one ADIF record). HRDLog answers `<insert>1` (stored), `<insert>0` (already there) or an `<error>`.

## License

MIT, see [LICENSE](LICENSE).

73, W4MMP
