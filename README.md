# sony-str-dn1050-remote

A small web remote for the **Sony STR-DN1050** AV receiver. It covers the EQ, speaker levels, sound field, volume, inputs and more, and works from any browser on your LAN.

Sony's own control apps for this receiver (TV SideView, the old network remote) no longer exist. The receiver still speaks Sony's control protocol on TCP port 33335, so this project talks to it directly. Sony never published the protocol for this model, so everything here was worked out by poking at a real unit. The [write-up below](#how-it-was-figured-out) explains how.

- One Python file, standard library only, no dependencies
- Serves a mobile-friendly page (light and dark mode) that updates live when someone uses the IR remote
- Probably works on related 2013–2014 Sony receivers (STR-DN850, STR-DN1040, STR-DN2030 …), but only the DN1050 has been tested

## Usage

On the receiver, enable **Settings → Network → External Control → On** (Network Standby helps too). Then:

```sh
python3 sony_remote.py --amp 192.168.1.50        # or: SONY_AMP=192.168.1.50 python3 sony_remote.py
```

Open `http://localhost:8765/`, or `http://<this-machine>:8765/` from a phone.

### Docker (recommended for an always-on box such as a NAS)

Give the receiver a DHCP reservation, set `SONY_AMP` in `docker-compose.yaml`, then:

```sh
docker compose up -d
```

It uses the stock `python:3.13-alpine` image with the script mounted in, so there's nothing to build. It runs on a read-only filesystem with all capabilities dropped and is capped at 64 MB RAM and a quarter of a CPU. In practice it idles at about 14 MB and close to 0% CPU. Your sound-field names persist in `./data`.

The receiver can be off for as long as you like. The server keeps serving the page, shows "receiver not responding", retries every 30 seconds and reconnects by itself when the receiver comes back. TCP keepalive and a silence timeout catch connections that die without closing.

### systemd

To run it at login as a systemd user service, put this in `~/.config/systemd/user/sony-remote.service`:

```ini
[Unit]
Description=Sony STR-DN1050 web remote
After=network-online.target

[Service]
Environment=SONY_AMP=192.168.1.50
ExecStart=/usr/bin/python3 %h/sony-str-dn1050-remote/sony_remote.py
Restart=on-failure

[Install]
WantedBy=default.target
```

Then run `systemctl --user enable --now sony-remote`.

> The server has no authentication. Anyone who can reach the port can change your volume. Keep it on a trusted LAN, or pass `--bind 127.0.0.1`.

## What it controls

| Area | Control | Range / notes |
|---|---|---|
| Main | Volume, mute | 0–74 |
| | Input | Names read from the receiver, including any you renamed on it |
| | Sound field | 16 modes; unnamed ones can be renamed in the UI |
| Tone | Bass / treble per speaker group | ±10 dB, 1 dB steps. Groups whose speakers aren't set up are hidden |
| Speakers | Level per channel (FL, FR, C, SL, SR, SBL, SBR, height, sub) | ±10 dB, 0.5 dB steps |
| Processing | Sound Optimizer | Off / Normal / Low |
| | Calibration type | Full Flat / Engineer / Front Reference / Off |
| | D.Range Comp | Off / Auto / On. The receiver usually refuses this unless a Dolby source is playing |
| | Pure Direct | Read-only |

**Not possible over the network:** crossover frequency, speaker size and speaker distance. The receiver reports a crossover/speaker-config block, but it rejects every write format I tried. Distance doesn't show up in any readable register.

**Why only bass and treble?** This was the original goal: taming sibilance with "lots of EQ sliders". The DN1050 hardware only has a bass and a treble shelf per speaker group, and no protocol trick can add more bands. Cutting the treble shelf 2–4 dB is the best the receiver itself can do. For real multi-band EQ, put an EQ before the receiver.

## How it was figured out

These are my notes from the reverse-engineering session, roughly in order. They should help if you want to extend this to another model.

### 1. Find the receiver and see what it exposes

A port sweep of the LAN turned up a host with ports **80, 8080, 33335 and 50001** open.

- `:8080` and `:50001` are UPnP/DLNA. The device description identifies the unit (`STR-DN1050`). It also advertises Sony `X_IRCC` (IR-over-IP) and `X_Tandem` (v3.0), plus a CERS action list (`register`, `getStatus`, `getRemoteCommandList` …).
- `:80` is a small "Sony Network Device Setting" web UI with only network settings and firmware updates. It has no audio settings.
- The CERS endpoints need a registration that seems to want approval on the TV screen, so I dropped them.
- That left **`:33335`**. A Crestron driver note for the DN1050 confirmed it's the control port, and that "External Control" must be on.

### 2. The framing

A public gist ([sorah/str-dn2030_ctl.rb](https://gist.github.com/sorah/8161dc9ee36ea30bbae8)) had captures for the related STR-DN2030 on the same port. From its sample replies the framing is:

```
02  <len>  <payload: len bytes>  <checksum>       checksum = -(len + Σpayload) & 0xFF
```

The single bytes `FD` and `FE` are ACK and NAK. I checked the checksum rule against the gist's replies, then against live replies from the DN1050.

Later I found Sony's generic **RS-232C Amplifier/Receiver Control Protocol** spec (a scanned PDF). It turned out to be the same protocol carried over TCP, and it confirmed:

- **Categories:** `A0` amp, `A1` tuner, `A3` sound processor ("Sound Adaptor"). Replies come back with bit 3 set (`A8`, `A9`, `AB`).
- **Error replies:** `<cat> 0E` = "not executable now", `<cat> 0F` = "no such function".
- **Opcodes:** status/volume/mute/input/power, `SF_SELECT` (`A3 42`) and a sound-field code table from an older model.

The DN1050 has many more `A3` registers than that spec lists. Everything else here was found by experiment.

### 3. Read-only scanning

I sent `<cat> <op> 00` for every category `A0`–`AF` and every opcode `80`–`FF`. Opcodes `80` and up are "requests" in Sony's scheme, so this was safe. Responders on the DN1050:

```
A0 80  A0 82  A0 90  A0 A0(model name)  A1 82  A3 82  A3 84  A3 90  A3 92  A3 93
A3 97  A3 98  A3 A0  A3 B0  A3 B1  A4 82  A5 80
```

Arguments to the `A3` requests turned out to be ignored.

### 4. Map registers to menu items with a human in the loop

I ran a recorder that polled every register about once a second and also logged frames the receiver pushes on its own. Meanwhile someone stood at the receiver and changed one menu item at a time, about 5 seconds apart. The timestamps lined up:

| Menu action | Register that moved |
|---|---|
| EQ front bass / treble | `A3 A0` (word 0 / word 2) |
| Speaker level sub / centre | `A3 B1` (word 9 / word 2, 0.5 dB steps) |
| Crossover | `A3 B0` (read only) |
| Sound Optimizer | `A3 92` (0/1/2) |
| Sound field | `A3 82` |
| Pure Direct | `A3 98` |
| D.Range Comp | `A3 84` |
| Calibration type | `A3 90` |

A second, very useful finding: the receiver **pushes** a fresh status frame whenever something changes. The app keeps one connection open and parses these pushes, which is why the UI tracks the IR remote live.

### 5. Find the write formats

Sony's convention is that the set opcode is the request opcode minus `0x40` (`92` → `52`, `82` → `42`). The surprises:

- **ACK doesn't mean applied.** Writing the whole EQ table back to `A3 60` returned `FD` but changed nothing. The real format is per group: `A3 60 <group> <bass i16> <mid i16> <treble i16>`. The 15-word EQ table is 5 groups of (bass, mid, treble), and mid is always `7FFF` (n/a) on this model.
- **Values are int16 in 1/256 dB.** `0x0100` = +1.0 dB, `0xFE80` = −1.5 dB. EQ rejects anything that isn't a whole dB; levels accept 0.5 dB steps.
- **Speaker-level channel IDs aren't table indices.** Writing to index 2 (centre) was NAKed. A scan of IDs 0–255, each with a +0.5 dB test write that was undone straight away, found Centre = `0x10` and Sub = `0x50`. Front L/R are `0x00`/`0x01`. The grouping implies surround `0x2x`, surround back `0x3x` and height `0x4x`. Those three are untested, because the test unit is 3.1.
- **Single-byte registers** (`A3 50/52/53/57`) take just the value byte, even though the read reply has a leading `00`.
- **Context-dependent refusals** come back as `AB 0E`: Surround EQ with no surround speakers, and D.Range Comp in most modes.
- **Never accepted:** crossover/speaker config (`A3 70`) and Pure Direct (`A3 58`).

### 6. Sound-field codes

`A3 42 <code>` for 0x00–0x7F, with readback, showed which codes the unit accepts:

```
00 16 19 1B 1E 1F 20 21 23 24 25 26 27 30 33 38
```

Names for 0x00–0x1F come from Sony's older table (2ch Stereo, Jazz Club, Live Concert, Stadium, Concert Hall A/B). `0x21` was identified on the unit as A.F.D. Auto. The rest are unnamed in any document I could find, so the UI labels them `Mode xx` and lets you rename them. Names are saved to `sound_field_names.json`. If you identify them, a PR adding them to `DEFAULT_SF_NAMES` would be very welcome.

### Lessons

- Check every write by reading it back. This receiver ACKs plenty of things it then ignores.
- Write the current value back first (a no-op write) before trying new values. Restore immediately after every test.
- Off-by-one errors in length handling are easy to make. One of my early probes sent the checksum byte as part of a value and briefly changed two settings before I noticed and restored them.

## Protocol reference (STR-DN1050)

```
A0 82 00               status → A8 82 <zone> <audio-in> <video-in> <flags: b0 power, b1 mute, b2 headphones> <12V>
A0 92 00 03 00         volume → A8 92 00 03 <i16>              set: A0 52 00 03 <i16>   (0–74)
A0 53 00 <0|1>         mute
A0 42 00 <input>       select input
A0 8B 00 <page 0|1>    input list: 10 × 21-byte entries
                         [idx][audio id][video id][icon][preset name ×8][user name ×8][skip flags]
A3 82                  sound field → AB 82 <code> 00            set: A3 42 <code>
A3 84                  D.Range Comp (0 off, 1 auto, 2 on)       set: A3 44 <v>   (usually 0E)
A3 90                  calibration (0 full flat, 2 front ref, 3 engineer, 4 off)  set: A3 50 <v>
A3 92                  sound optimizer (0 off, 1 normal, 2 low) set: A3 52 <v>
A3 93 / A3 97          unidentified (0/1 and 0/2), writable via 53 / 57
A3 98                  pure direct (read only)
A3 A0                  EQ: 5 groups × (bass, mid, treble) i16 1/256 dB, 7FFF = n/a
                         set: A3 60 <group> <bass> <mid> <treble>   groups: 0 front, 1 centre, 4 surround
A3 B0                  speaker config / crossover (read only)
A3 B1                  levels: 10 × i16 (FL FR C SL SR SBL SBR FHL FHR SW)
                         set: A3 71 <id> <i16>   ids 00 01 10 20 21 30 31 40 41 50
Replies: FD ack · FE nak · <cat> 0E not executable · <cat> 0F no such function
```

Leave at least 100 ms between commands.

## Disclaimer

This project is not affiliated with Sony. It changes receiver settings with commands that were reverse-engineered, not documented. Every command here was tested on one unit and is bounded to the ranges the receiver's own menu allows. Use it at your own risk.

## License

MIT
