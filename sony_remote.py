#!/usr/bin/env python3
"""Sony STR-DN1050 network remote (reverse-engineered).

Talks to the receiver's "external control" TCP protocol (port 33335) and
serves a web UI. Python stdlib only.

    python3 sony_remote.py --amp <receiver-ip> [--port 8765]     (or set SONY_AMP)

Protocol (Sony RS-232C-style framing over TCP, mapped on this unit):
  frame     02 <len> <payload...> <cs>     cs = -(len + sum(payload)) & 0xff
  replies   FD ack, FE nak, <cat|8> 0E = not executable now, <cat|8> 0F = no such function
  A0 = amp, A3 = sound processor; replies come back as A8 / AB. Set opcode = get opcode - 0x40.

  A0 82 00          status: zone, audio-in, video-in, flags(bit0 power, bit1 mute, bit2 headphones)
  A0 92 00 03 00    volume -> A8 92 00 03 <i16>      set: A0 52 00 03 <i16>
  A0 53 00 <0|1>    mute
  A0 42 00 <in>     input select;  A0 8B 00 <page>  input list (21-byte entries)
  A3 82             sound field -> AB 82 <code> 00    set: A3 42 <code>
  A3 84             D.Range Comp (0 off,1 auto,2 on)  set: A3 44 <v>   (often refused)
  A3 90             calibration type                  set: A3 50 <v>
  A3 92             sound optimizer (0 off,1 normal,2 low)  set: A3 52 <v>
  A3 98             pure direct (read only over IP)
  A3 A0             EQ: 5 groups x (bass, mid, treble) i16 1/256 dB, 7FFF = n/a
                    set: A3 60 <group> <bass i16> <mid i16> <treble i16>   (+-10 dB, 1 dB steps)
  A3 B1             speaker levels: 10 x i16 1/256 dB (FL FR C SL SR SBL SBR FHL FHR SW)
                    set: A3 71 <channel id> <i16>   ids 00 01 10 20 21 30 31 40 41 50 (0.5 dB steps)
"""
import argparse, json, os, socket, struct, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NA = 0x7FFF
HERE = os.path.dirname(os.path.abspath(__file__))
NAMES_FILE = os.path.join(HERE, "sound_field_names.json")

EQ_GROUPS = {0: "Front", 1: "Center", 2: "Group 3", 3: "Group 4", 4: "Surround"}
SPEAKERS = [  # (table slot, write id, name)
    (0, 0x00, "Front L"), (1, 0x01, "Front R"), (2, 0x10, "Center"),
    (3, 0x20, "Surround L"), (4, 0x21, "Surround R"),
    (5, 0x30, "Surround Back L"), (6, 0x31, "Surround Back R"),
    (7, 0x40, "Height L"), (8, 0x41, "Height R"), (9, 0x50, "Subwoofer"),
]
DEFAULT_SF_NAMES = {  # from Sony's protocol spec; newer codes are unnamed until renamed in the UI
    0x00: "2ch Stereo", 0x16: "Jazz Club", 0x21: "A.F.D. Auto",
    0x19: "Live Concert", 0x1B: "Stadium", 0x1E: "Concert Hall A", 0x1F: "Concert Hall B",
}
KNOWN_SF = [0x00, 0x16, 0x19, 0x1B, 0x1E, 0x1F, 0x20, 0x21, 0x23, 0x24, 0x25, 0x26, 0x27, 0x30, 0x33, 0x38]
OPTIMIZER = {0: "Off", 1: "Normal", 2: "Low"}
CALIBRATION = {0: "Full Flat", 3: "Engineer", 2: "Front Reference", 4: "Off"}
DRC = {0: "Off", 1: "Auto", 2: "On"}


def frame(payload):
    b = bytes([len(payload)]) + bytes(payload)
    return b"\x02" + b + bytes([(-sum(b)) & 0xFF])


def i16(b, o):
    return struct.unpack(">h", bytes(b[o:o + 2]))[0]


class Amp:
    def __init__(self, host, port=33335):
        self.host, self.port = host, port
        self.sock = None
        self.send_lock = threading.Lock()
        self.reply = threading.Condition()
        self.last_reply = None
        self.state = {"connected": False, "inputs": [], "sf_known": list(KNOWN_SF)}
        self.state_lock = threading.Lock()
        self.version = 0
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._poller, daemon=True).start()

    # ---- connection -------------------------------------------------------
    def _connect(self):
        if self.sock:
            return self.sock
        s = socket.create_connection((self.host, self.port), 4)
        s.settimeout(None)
        self.sock = s
        self._set(connected=True)
        return s

    def _drop(self):
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self.sock = None
        self._set(connected=False)

    def _reader(self):
        buf = b""
        while True:
            s = self.sock
            if not s:
                time.sleep(0.3)
                continue
            try:
                d = s.recv(4096)
                if not d:
                    raise OSError("closed")
            except OSError:
                self._drop()
                buf = b""
                continue
            buf += d
            while buf:
                if buf[0] in (0xFD, 0xFE):
                    self._got("ack" if buf[0] == 0xFD else "nak")
                    buf = buf[1:]
                elif buf[0] == 0x02:
                    if len(buf) < 2 or len(buf) < buf[1] + 3:
                        break
                    n = buf[1]
                    self._frame(buf[2:2 + n])
                    buf = buf[n + 3:]
                else:
                    buf = buf[1:]

    def _got(self, what):
        with self.reply:
            self.last_reply = what
            self.reply.notify_all()

    def _set(self, **kw):
        with self.state_lock:
            self.state.update(kw)
            self.version += 1

    # ---- parse frames -----------------------------------------------------
    def _frame(self, p):
        if len(p) < 2:
            return
        cat, op, d = p[0], p[1], p[2:]
        if op in (0x0E, 0x0F) and len(p) == 2:
            return self._got("noexec" if op == 0x0E else "nofunc")
        try:
            if cat == 0xA8 and op == 0x82 and d[0] == 0:
                self._set(power=bool(d[3] & 1), mute=bool(d[3] & 2), headphones=bool(d[3] & 4),
                          input=d[1])
            elif cat == 0xA8 and op == 0x92 and d[0] == 0:
                self._set(volume=i16(d, 2))
            elif cat == 0xA8 and op == 0x8B and d[0] == 0:
                self._inputs(d[1:])
            elif cat == 0xAB and op == 0x82:
                with self.state_lock:
                    if d[0] not in self.state["sf_known"]:
                        self.state["sf_known"].append(d[0])
                self._set(sound_field=d[0])
            elif cat == 0xAB and op == 0x84:
                self._set(drc=d[0])
            elif cat == 0xAB and op == 0x90:
                self._set(calibration=d[1])
            elif cat == 0xAB and op == 0x92:
                self._set(optimizer=d[1])
            elif cat == 0xAB and op == 0x98:
                self._set(pure_direct=bool(d[0]))
            elif cat == 0xAB and op == 0xA0 and len(d) >= 30:
                v = [i16(d, k * 2) for k in range(15)]
                self._set(eq_raw=v)
            elif cat == 0xAB and op == 0xB1 and len(d) >= 20:
                self._set(levels_raw=[i16(d, k * 2) for k in range(10)])
        except IndexError:
            pass

    def _inputs(self, d):
        found = {}
        for k in range(0, len(d) - 20, 21):
            e = d[k:k + 21]
            if e[0] == 0xFF:
                continue
            name = bytes(e[12:20]).decode("ascii", "replace").strip() or bytes(e[4:12]).decode("ascii", "replace").strip()
            found[e[1]] = {"code": e[1], "index": e[0], "name": name}
        with self.state_lock:
            cur = {i["code"]: i for i in self.state["inputs"]}
            cur.update(found)
            self.state["inputs"] = sorted(cur.values(), key=lambda i: i["index"])
            self.version += 1

    # ---- commands ---------------------------------------------------------
    def send(self, payload, wait_ack=True, timeout=1.0):
        with self.send_lock:
            for attempt in (1, 2):
                try:
                    s = self._connect()
                    with self.reply:
                        self.last_reply = None
                    s.sendall(frame(payload))
                    break
                except OSError:
                    self._drop()
                    if attempt == 2:
                        raise IOError("Can't reach the receiver")
            res = None
            if wait_ack:
                end = time.time() + timeout
                with self.reply:
                    while self.last_reply is None and time.time() < end:
                        self.reply.wait(end - time.time())
                    res = self.last_reply
                    # an ack can be followed by a "not executable" frame; give it a moment
                    if res == "ack":
                        self.reply.wait(0.12)
                        if self.last_reply in ("noexec", "nofunc"):
                            res = self.last_reply
            time.sleep(0.11)  # receiver wants >= 100 ms between commands
            return res

    def command(self, payload, refresh=()):
        r = self.send(payload)
        if r == "noexec":
            raise ValueError("The receiver can't do that right now (not available in the current mode or setup).")
        if r in ("nak", "nofunc"):
            raise ValueError("The receiver rejected that change.")
        if r is None:
            raise IOError("No reply from the receiver.")
        for q in refresh:
            self.send(q, wait_ack=False)
        time.sleep(0.25)

    QUERIES = [
        [0xA0, 0x82, 0x00], [0xA0, 0x92, 0x00, 0x03, 0x00], [0xA3, 0x82], [0xA3, 0x84],
        [0xA3, 0x90], [0xA3, 0x92], [0xA3, 0x98], [0xA3, 0xA0, 0x00], [0xA3, 0xB1, 0x00],
    ]

    def refresh_all(self, inputs=False):
        qs = list(self.QUERIES)
        if inputs:
            qs += [[0xA0, 0x8B, 0x00, 0x00], [0xA0, 0x8B, 0x00, 0x01]]
        for q in qs:
            self.send(q, wait_ack=False)

    def _poller(self):
        n = 0
        while True:
            try:
                self.refresh_all(inputs=(n % 20 == 0))
                n += 1
            except Exception:
                pass
            time.sleep(10)

    # ---- view -------------------------------------------------------------
    def view(self):
        with self.state_lock:
            s = json.loads(json.dumps(self.state))
        names = load_names()
        out = {k: s.get(k) for k in ("connected", "power", "mute", "headphones", "volume", "input",
                                     "sound_field", "drc", "calibration", "optimizer", "pure_direct", "inputs")}
        out["sound_fields"] = [{"code": c, "name": names.get(c) or DEFAULT_SF_NAMES.get(c) or f"Mode {c:02X}",
                                "custom": c in names} for c in sorted(s["sf_known"])]
        lv = s.get("levels_raw")
        out["levels"] = [] if not lv else [
            {"slot": slot, "name": name, "db": lv[slot] / 256} for slot, _id, name in SPEAKERS if lv[slot] != NA]
        present = {x["name"] for x in out["levels"]}
        eq = s.get("eq_raw")
        out["eq"] = []
        if eq:
            for g in range(5):
                bass, _m, treble = eq[g * 3:g * 3 + 3]
                if bass == NA and treble == NA:
                    continue
                if g == 1 and lv and "Center" not in present:
                    continue
                if g == 4 and lv and "Surround L" not in present:
                    continue
                out["eq"].append({"group": g, "name": EQ_GROUPS[g],
                                  "bass": None if bass == NA else bass / 256,
                                  "treble": None if treble == NA else treble / 256})
        out["options"] = {"optimizer": OPTIMIZER, "calibration": CALIBRATION, "drc": DRC}
        return out

    # ---- setters ----------------------------------------------------------
    def apply(self, b):
        w = b.get("what")
        if w == "volume":
            v = max(0, min(74, int(b["value"])))
            self.command([0xA0, 0x52, 0x00, 0x03] + list(struct.pack(">h", v)), [[0xA0, 0x92, 0x00, 0x03, 0x00]])
        elif w == "mute":
            self.command([0xA0, 0x53, 0x00, 1 if b["on"] else 0], [[0xA0, 0x82, 0x00]])
        elif w == "input":
            self.command([0xA0, 0x42, 0x00, int(b["code"])], [[0xA0, 0x82, 0x00], [0xA3, 0x82]])
        elif w == "sound_field":
            self.command([0xA3, 0x42, int(b["code"])], [[0xA3, 0x82]])
        elif w in ("optimizer", "calibration", "drc"):
            op, read, valid = {"optimizer": (0x52, 0x92, OPTIMIZER), "calibration": (0x50, 0x90, CALIBRATION),
                               "drc": (0x44, 0x84, DRC)}[w]
            v = int(b["value"])
            if v not in valid:
                raise ValueError("Invalid value")
            self.command([0xA3, op, v], [[0xA3, read]])
        elif w == "eq":
            def enc(x):
                return NA if x is None else int(round(max(-10, min(10, float(x))))) * 256
            g = int(b["group"])
            self.command([0xA3, 0x60, g] + list(struct.pack(">hhh", enc(b.get("bass")), NA, enc(b.get("treble")))),
                         [[0xA3, 0xA0, 0x00]])
        elif w == "level":
            slot = int(b["slot"])
            wid = next(i for s, i, _n in SPEAKERS if s == slot)
            db = round(max(-10, min(10, float(b["db"]))) * 2) / 2
            self.command([0xA3, 0x71, wid] + list(struct.pack(">h", int(db * 256))), [[0xA3, 0xB1, 0x00]])
        elif w == "rename":
            names = load_names()
            code, name = int(b["code"]), str(b.get("name", "")).strip()[:40]
            if name:
                names[code] = name
            else:
                names.pop(code, None)
            save_names(names)
        elif w == "refresh":
            self.refresh_all(inputs=True)
            time.sleep(0.4)
        else:
            raise ValueError("Unknown control")


def load_names():
    try:
        with open(NAMES_FILE) as f:
            return {int(k): v for k, v in json.load(f).items()}
    except (OSError, ValueError):
        return {}


def save_names(n):
    with open(NAMES_FILE, "w") as f:
        json.dump({str(k): v for k, v in sorted(n.items())}, f, indent=1)


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>STR-DN1050 Remote</title>
<style>
:root{--bg:#f4f3ef;--card:#fff;--fg:#1d1d1b;--mut:#6b6a66;--line:#dedcd5;--acc:#2f6fdf;--acc2:#e8effc;--bad:#c2412d;--ok:#2d8a4e}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1f1f1d;--fg:#ecebe6;--mut:#9a9893;--line:#34332f;--acc:#7aa7ff;--acc2:#24304a;--bad:#ff8a73;--ok:#6cd090}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:16px 16px 48px}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:4px 0 14px}
h1{font-size:19px;margin:0}
#status{font-size:13px;color:var(--mut);display:flex;align-items:center;gap:6px}
#status i{width:8px;height:8px;border-radius:50%;background:var(--mut);display:inline-block}
#status.ok i{background:var(--ok)} #status.bad{color:var(--bad)} #status.bad i{background:var(--bad)}
nav{display:flex;gap:6px;margin-bottom:14px;overflow-x:auto}
nav button{flex:1;white-space:nowrap}
nav button.on{background:var(--acc2);border-color:var(--acc);color:var(--acc);font-weight:600}
section{display:none} section.on{display:block}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:12px}
.card h2{font-size:14px;margin:0 0 10px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.row{display:grid;grid-template-columns:minmax(90px,130px) 1fr 70px;align-items:center;gap:10px;margin:6px 0}
.row label{font-size:14px}
.val{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
input[type=range]{width:100%;accent-color:var(--acc);height:30px;margin:0}
.big{display:flex;align-items:center;gap:10px}
.big .num{font-size:34px;font-weight:700;font-variant-numeric:tabular-nums;min-width:64px;text-align:center}
button,select{font:inherit;padding:8px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button:hover,select:hover{border-color:var(--acc)}
button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.seg{display:flex;flex-wrap:wrap;gap:6px}
.chips{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:6px}
.chips button{text-align:left}
.err{color:var(--bad);font-size:13px;min-height:0;margin-top:4px}
.note{color:var(--mut);font-size:13px;margin:6px 0 0}
.pill{display:inline-block;font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line);color:var(--mut)}
.pill.on{color:var(--acc);border-color:var(--acc)}
.tools{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
</style></head><body><main>
<header><h1>STR-DN1050</h1><span id="status"><i></i><span>connecting…</span></span></header>
<nav>
  <button data-tab="main" class="on">Main</button>
  <button data-tab="tone">Tone</button>
  <button data-tab="spk">Speakers</button>
  <button data-tab="proc">Processing</button>
</nav>

<section id="main" class="on">
  <div class="card"><h2>Volume</h2>
    <div class="big"><button id="vdn">−</button><span class="num" id="vnum">–</span><button id="vup">+</button>
      <button id="mute" style="margin-left:auto">Mute</button></div>
    <input type="range" id="vol" min="0" max="74" step="1">
    <div class="err" id="e-vol"></div>
  </div>
  <div class="card"><h2>Input</h2><div class="chips" id="inputs"></div><div class="err" id="e-input"></div></div>
  <div class="card"><h2>Sound field</h2><div class="chips" id="sf"></div>
    <div class="tools"><button id="rename">Rename current mode…</button></div>
    <div class="err" id="e-sf"></div>
    <p class="note">Modes the protocol doesn't name show as "Mode xx". Select one, check the receiver's display, then use Rename.</p>
  </div>
</section>

<section id="tone">
  <div class="tools" style="margin:0 0 12px"><button id="tame">Tame sibilance (treble −3)</button><button id="flat">Flat EQ</button></div>
  <div id="eq"></div>
  <p class="note">The DN1050 EQ is bass and treble per speaker group, ±10 dB in 1 dB steps. Treble is a shelf — cutting it softens harsh "s" sounds.</p>
</section>

<section id="spk">
  <div class="card"><h2>Speaker levels</h2><div id="levels"></div><div class="err" id="e-lv"></div>
  <p class="note">±10 dB in 0.5 dB steps. Same as Speaker Settings → Level on the receiver.</p></div>
  <p class="note">Crossover frequency, speaker size and distance can only be changed from the receiver's own menu — it doesn't accept them over the network.</p>
</section>

<section id="proc">
  <div class="card"><h2>Sound Optimizer</h2><div class="seg" data-ctl="optimizer"></div><div class="err" id="e-optimizer"></div></div>
  <div class="card"><h2>Calibration type</h2><div class="seg" data-ctl="calibration"></div><div class="err" id="e-calibration"></div></div>
  <div class="card"><h2>Dynamic range compressor</h2><div class="seg" data-ctl="drc"></div><div class="err" id="e-drc"></div>
    <p class="note">The receiver usually only allows this while a Dolby source is playing.</p></div>
  <div class="card"><h2>Pure Direct</h2><span class="pill" id="pd">–</span>
    <p class="note">Read-only over the network — use the PURE DIRECT button on the remote.</p></div>
</section>
</main>
<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let S=null, busy=0, dragging=false, lastJSON='';
const fmt=(v,u=' dB')=>v==null?'–':(v>0?'+':'')+v+u;
function status(t,cls){const s=$('#status');s.className=cls||'';s.lastElementChild.textContent=t}
async function api(body){
  const r=await fetch('/api/'+(body?'set':'state'),body?{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)}:{});
  const j=await r.json(); if(!r.ok) throw new Error(j.error||r.statusText); return j;
}
async function set(body,errId){
  busy++; const e=errId&&$('#'+errId); if(e) e.textContent='';
  try{ S=await api(body); render(true); status('saved','ok') }
  catch(x){ if(e) e.textContent=x.message; else status(x.message,'bad'); await poll(true) }
  finally{ busy-- }
}
$$('nav button').forEach(b=>b.onclick=()=>{$$('nav button').forEach(x=>x.classList.toggle('on',x===b));$$('section').forEach(s=>s.classList.toggle('on',s.id===b.dataset.tab))});

// volume
$('#vol').oninput=e=>{$('#vnum').textContent=e.target.value};
$('#vol').onpointerdown=()=>dragging=true;
$('#vol').onchange=e=>{dragging=false;set({what:'volume',value:+e.target.value},'e-vol')};
$('#vup').onclick=()=>S&&set({what:'volume',value:S.volume+1},'e-vol');
$('#vdn').onclick=()=>S&&set({what:'volume',value:S.volume-1},'e-vol');
$('#mute').onclick=()=>S&&set({what:'mute',on:!S.mute},'e-vol');
$('#rename').onclick=()=>{ if(!S) return; const cur=S.sound_fields.find(f=>f.code===S.sound_field);
  const n=prompt('Name for sound field code '+S.sound_field.toString(16).toUpperCase().padStart(2,'0')+' (blank to reset):',cur?cur.name:''); if(n!==null) set({what:'rename',code:S.sound_field,name:n},'e-sf') };
$('#flat').onclick=async()=>{for(const g of S.eq) await set({what:'eq',group:g.group,bass:0,treble:0},'e-eq'+g.group)};
$('#tame').onclick=async()=>{for(const g of S.eq) if(g.group<2) await set({what:'eq',group:g.group,bass:g.bass,treble:-3},'e-eq'+g.group)};

function chips(el,items,cur,onpick){
  el.innerHTML=''; for(const it of items){const b=document.createElement('button');b.textContent=it.name;b.classList.toggle('on',it.code===cur);b.onclick=()=>onpick(it);el.appendChild(b)}
}
function slider(id,label,val,min,max,step,on){
  return `<div class="row"><label>${label}</label><input type="range" id="${id}" min="${min}" max="${max}" step="${step}" value="${val}" data-on="${on}"><span class="val" id="${id}-v">${fmt(val)}</span></div>`;
}
const timers={};
function hookSliders(root,make,errFor){
  root.querySelectorAll('input[type=range]').forEach(el=>{
    el.onpointerdown=()=>dragging=true;
    el.oninput=()=>{$('#'+el.id+'-v').textContent=fmt(+el.value)};
    el.onchange=()=>{dragging=false;clearTimeout(timers[el.id]);timers[el.id]=setTimeout(()=>set(make(el),errFor(el)),50)};
  });
}
function render(force){
  if(!S) return; const j=JSON.stringify(S); if(!force&&j===lastJSON) return; lastJSON=j;
  status(S.connected?(S.power===false?'standby':'connected'):'receiver not responding',S.connected?'ok':'bad');
  if(!dragging){ $('#vol').value=S.volume??0; $('#vnum').textContent=S.volume??'–' }
  $('#mute').classList.toggle('on',!!S.mute); $('#mute').textContent=S.mute?'Muted':'Mute';
  chips($('#inputs'),S.inputs,S.input,it=>set({what:'input',code:it.code},'e-input'));
  chips($('#sf'),S.sound_fields,S.sound_field,it=>set({what:'sound_field',code:it.code},'e-sf'));
  if(!dragging){
    $('#eq').innerHTML=S.eq.map(g=>`<div class="card"><h2>${g.name}</h2>`+
      (g.bass!=null?slider('eq'+g.group+'b','Bass',g.bass,-10,10,1,'bass'):'')+
      (g.treble!=null?slider('eq'+g.group+'t','Treble',g.treble,-10,10,1,'treble'):'')+`<div class="err" id="e-eq${g.group}"></div></div>`).join('');
    hookSliders($('#eq'),el=>{const g=+el.id.slice(2,-1);const b=$('#eq'+g+'b'),t=$('#eq'+g+'t');return{what:'eq',group:g,bass:b?+b.value:null,treble:t?+t.value:null}},el=>'e-eq'+el.id.slice(2,-1));
    $('#levels').innerHTML=S.levels.map(l=>slider('lv'+l.slot,l.name,l.db,-10,10,0.5)).join('');
    hookSliders($('#levels'),el=>({what:'level',slot:+el.id.slice(2),db:+el.value}),()=>'e-lv');
  }
  for(const k of ['optimizer','calibration','drc']){
    const el=document.querySelector(`[data-ctl=${k}]`); el.innerHTML='';
    for(const [v,name] of Object.entries(S.options[k])){const b=document.createElement('button');b.textContent=name;b.classList.toggle('on',S[k]===+v);b.onclick=()=>set({what:k,value:+v},'e-'+k);el.appendChild(b)}
  }
  $('#pd').textContent=S.pure_direct?'On':'Off'; $('#pd').classList.toggle('on',!!S.pure_direct);
}
async function poll(force){
  if(!force&&(busy||dragging)) return;
  try{ S=await api(); render(force) } catch(e){ status('app server not responding','bad') }
}
poll(true); setInterval(poll,1500);
</script></body></html>"""


def make_handler(amp):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._send(200, json.dumps(amp.view()))
            self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            if self.path != "/api/set":
                return self._send(404, json.dumps({"error": "not found"}))
            try:
                b = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                amp.apply(b)
                return self._send(200, json.dumps(amp.view()))
            except ValueError as e:
                return self._send(409, json.dumps({"error": str(e)}))
            except Exception as e:
                return self._send(502, json.dumps({"error": f"Receiver error: {e}"}))
    return H


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--amp", default=os.environ.get("SONY_AMP"), help="receiver IP (or set SONY_AMP)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bind", default="0.0.0.0")
    a = ap.parse_args()
    if not a.amp:
        ap.error("give the receiver's IP with --amp or SONY_AMP")
    amp = Amp(a.amp)
    srv = ThreadingHTTPServer((a.bind, a.port), make_handler(amp))
    print(f"STR-DN1050 remote: http://localhost:{a.port}/  (receiver {a.amp})", flush=True)
    srv.serve_forever()
