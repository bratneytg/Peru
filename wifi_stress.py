#!/usr/bin/env python3
"""
wifi_stress.py - Prueba de saturacion de tu red WiFi local (solo biblioteca estandar).

  Servidor + interfaz web (en un PC, idealmente conectado por cable al router):
      python3 wifi_stress.py
      python3 wifi_stress.py --port 8765

  Abre  http://IP_DEL_SERVIDOR:8765  desde los dispositivos conectados por WiFi
  (movil, portatil, tablet...) y pulsa "Iniciar". Con varios dispositivos a la vez
  veras cuanto aguanta el router/WiFi en conjunto.

  Cliente de consola (mas carga que la que permite un navegador):
      python3 wifi_stress.py client http://IP_DEL_SERVIDOR:8765 -n 24 -t 60 -m both

  Para saturar TODA la red domestica, incluidos dispositivos que no corren nada
  (una tele, una consola, un altavoz que solo reciben trafico de streaming),
  hace falta generar carga en la MISMA banda de radio que usan esos aparatos:
  la saturacion no "salta" de 5 GHz a 2.4 GHz ni al reves, son medios distintos.
  Mira --ayuda-bandas mas abajo.

Solo usalo en TU red. Genera trafico intenso a proposito.
"""
import argparse
import http.client
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

CHUNK = os.urandom(4 << 20)      # 4 MiB incompresible (antes 1 MiB: menos syscalls, mas Mbps por hilo)
SLICE = 256 * 1024
MAX_SECS = 7200                  # antes 600s (10 min); ahora hasta 2h para pruebas largas/nocturnas
EXTRA_PORTS = 5                  # puertos extra para que la web supere el limite de 6 conexiones/origen del navegador


BANDAS_AYUDA = """
  Por que la tele/consola/altavoz no nota nada aunque "satures el WiFi":

  1. Banda distinta. La mayoria de portatiles y moviles modernos se conectan
     por defecto a 5 GHz (mas rapido, menos alcance). Muchas teles, Chromecasts
     y altavoces inteligentes usan 2.4 GHz (mas alcance, mas lento). Son dos
     medios de radio casi independientes: saturar uno apenas afecta al otro.
     Solucion: fuerza al dispositivo que genera la carga a conectarse a la
     MISMA red/banda que el aparato que quieres afectar (en el movil/portatil,
     desactiva WiFi, entra a ajustes WiFi y conecta a mano a la red de 2.4 GHz
     si el router las separa en dos SSID, o mira si tu FRITZ!Box tiene "Band
     Steering" activado en Funkeinstellungen y pruebalo desactivado).

  2. Banda unificada (una sola SSID) con "band steering". El router decide el
     reparto y puede mover trafico poco sensible a la banda menos congestionada
     automaticamente, protegiendo el streaming.

  3. QoS / priorizacion. Internet > Filtros > Priorizacion en la FRITZ!Box
     puede marcar el trafico de streaming como "tiempo real" y servirlo antes
     que el resto aunque la red este llena.

  4. MU-MIMO / OFDMA (WiFi 5/6). El router puede atender varias transmisiones
     "a la vez" en la practica, asi que hace falta mas carga simultanea desde
     mas dispositivos para notar el efecto en todos.

  Para una saturacion que SI llegue a todo:
    - Lanza el cliente desde 2-3 dispositivos a la vez (cada uno en su propia
      terminal/pestana), con -n alto en cada uno.
    - Asegurate de que al menos uno esta en la misma banda que el aparato que
      te interesa.
    - Si tu FRITZ!Box separa SSID de 2.4 y 5 GHz, conecta un generador de carga
      a cada SSID.
"""


# --------------------------------------------------------------------------- servidor
class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.tx = 0          # bytes servidor -> clientes (descargas)
        self.rx = 0          # bytes clientes -> servidor (subidas)
        self.clients = {}

    def add(self, ip, tx=0, rx=0):
        with self.lock:
            self.tx += tx
            self.rx += rx
            c = self.clients.setdefault(ip, {"tx": 0, "rx": 0, "last": 0.0})
            c["tx"] += tx
            c["rx"] += rx
            c["last"] = time.time()

    def snapshot(self):
        now = time.time()
        with self.lock:
            return {
                "t": now,
                "tx": self.tx,
                "rx": self.rx,
                "clients": {ip: {"tx": c["tx"], "rx": c["rx"], "age": now - c["last"]}
                            for ip, c in self.clients.items()},
            }


STATS = Stats()
PING_PORT = 0
INDEX_HTML = ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "wifi-stress/1.1"

    def log_message(self, *a):
        pass

    def _send(self, body, ctype="text/plain; charset=utf-8", code=200):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        ip = self.client_address[0]
        try:
            if u.path in ("/", "/index.html"):
                self._send(INDEX_HTML.replace("__PINGPORT__", str(PING_PORT)), "text/html; charset=utf-8")
            elif u.path == "/ping":
                self._send("pong")
            elif u.path == "/stats":
                s = STATS.snapshot()
                s["you"] = ip
                self._send(json.dumps(s), "application/json")
            elif u.path == "/download":
                secs = min(float(q.get("secs", ["30"])[0]), MAX_SECS)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                end = time.time() + secs
                mv = memoryview(CHUNK)
                off = 0
                while time.time() < end:
                    self.wfile.write(mv[off:off + SLICE])
                    STATS.add(ip, tx=SLICE)
                    off = (off + SLICE) % len(CHUNK)
            else:
                self._send("not found", code=404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True

    def do_POST(self):
        ip = self.client_address[0]
        try:
            if self.path.split("?")[0] != "/upload":
                self._send("not found", code=404)
                return
            remaining = int(self.headers.get("Content-Length", "0"))
            while remaining > 0:
                data = self.rfile.read(min(remaining, 256 * 1024))
                if not data:
                    self.close_connection = True
                    return
                remaining -= len(data)
                STATS.add(ip, rx=len(data))
            self._send("ok")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, ValueError):
            self.close_connection = True


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 512   # antes 256: admite mas conexiones simultaneas en cola
    allow_reuse_address = True


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def console_meter(stop):
    prev = STATS.snapshot()
    while not stop.is_set():
        time.sleep(1)
        cur = STATS.snapshot()
        dt = cur["t"] - prev["t"]
        down = (cur["tx"] - prev["tx"]) * 8 / dt / 1e6
        up = (cur["rx"] - prev["rx"]) * 8 / dt / 1e6
        act = sum(1 for c in cur["clients"].values() if c["age"] < 3)
        sys.stdout.write(f"\r  Red total  v {down:8.1f} Mbps   ^ {up:8.1f} Mbps   dispositivos activos: {act}   ")
        sys.stdout.flush()
        prev = cur


def serve(host, port):
    global PING_PORT, INDEX_HTML
    PING_PORT = port + 1
    load_ports = [port] + [port + 2 + i for i in range(EXTRA_PORTS)]
    INDEX_HTML = HTML.replace("__PORTS__", json.dumps(load_ports))
    servers = [Server((host, port), Handler)]
    ping = Server((host, PING_PORT), Handler)   # puerto separado: el ping no hace cola tras las descargas
    threading.Thread(target=ping.serve_forever, daemon=True).start()
    for p in load_ports[1:]:
        s = Server((host, p), Handler)
        servers.append(s)
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print("\n  WiFi Stress Test - servidor activo\n")
    for ip in lan_ips() or ["<tu-ip>"]:
        print(f"    Abre desde tus dispositivos:  http://{ip}:{port}")
    print(f"\n  Usa {len(load_ports)} puertos ({', '.join(str(p) for p in load_ports)}) para que la")
    print("  propia pagina web pueda superar el limite de 6 conexiones por origen")
    print("  que imponen los navegadores.")
    print("\n  Ctrl+C para salir. Pasa --ayuda-bandas para ver por que algunos")
    print("  aparatos (tele, altavoces...) pueden no notar la saturacion.\n")
    stop = threading.Event()
    threading.Thread(target=console_meter, args=(stop,), daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        print("\n  Cerrando...")
    finally:
        stop.set()
        for s in servers:
            s.server_close()


# --------------------------------------------------------------------------- cliente CLI
def client(url, streams, secs, mode, bind_ip=None):
    u = urlparse(url if "//" in url else "http://" + url)
    host, port = u.hostname, u.port or 80
    ping_port = port + 1
    src = (bind_ip, 0) if bind_ip else None
    stop = threading.Event()
    n_down = n_up = 0
    if mode == "down":
        n_down = streams
    elif mode == "up":
        n_up = streams
    else:
        n_down = (streams + 1) // 2
        n_up = max(1, streams // 2)
    cnt_d = [0] * max(n_down, 1)
    cnt_u = [0] * max(n_up, 1)
    blob = os.urandom(2 << 20)

    def down(i):
        while not stop.is_set():
            try:
                c = http.client.HTTPConnection(host, port, timeout=10, source_address=src)
                c.request("GET", f"/download?secs={secs + 10}")
                r = c.getresponse()
                while not stop.is_set():
                    d = r.read(256 * 1024)
                    if not d:
                        break
                    cnt_d[i] += len(d)
                c.close()
            except OSError:
                time.sleep(0.2)

    def up(i):
        while not stop.is_set():
            try:
                c = http.client.HTTPConnection(host, port, timeout=10, source_address=src)
                while not stop.is_set():
                    c.request("POST", "/upload", body=blob)
                    c.getresponse().read()
                    cnt_u[i] += len(blob)
                c.close()
            except OSError:
                time.sleep(0.2)

    lat = []   # (t, ms)
    lat_stop = threading.Event()

    def pinger():
        try:
            c = http.client.HTTPConnection(host, ping_port, timeout=2, source_address=src)
            while not lat_stop.is_set():
                t0 = time.perf_counter()
                try:
                    c.request("GET", "/ping")
                    c.getresponse().read()
                    lat.append((time.time(), (time.perf_counter() - t0) * 1000))
                except OSError:
                    lat.append((time.time(), None))
                    c.close()
                    c = http.client.HTTPConnection(host, ping_port, timeout=2, source_address=src)
                time.sleep(0.2)
        except OSError:
            pass

    print(f"\n  Cliente -> {host}:{port}  modo={mode}  conexiones: v{n_down} ^{n_up}  duracion={secs}s"
          + (f"  bind={bind_ip}" if bind_ip else ""))
    threading.Thread(target=pinger, daemon=True).start()
    print("  Midiendo latencia en reposo (3 s)...")
    time.sleep(3)
    t_start = time.time()
    for i in range(n_down):
        threading.Thread(target=down, args=(i,), daemon=True).start()
    for i in range(n_up):
        threading.Thread(target=up, args=(i,), daemon=True).start()

    samples = []
    pd, pu = sum(cnt_d), sum(cnt_u)
    try:
        for s in range(secs):
            time.sleep(1)
            d, uu = sum(cnt_d), sum(cnt_u)
            md, mu = (d - pd) * 8 / 1e6, (uu - pu) * 8 / 1e6
            pd, pu = d, uu
            samples.append((md, mu))
            print(f"  {s + 1:3d}s   v {md:8.1f} Mbps   ^ {mu:8.1f} Mbps   total {md + mu:8.1f} Mbps")
    except KeyboardInterrupt:
        pass
    stop.set()
    lat_stop.set()
    t_end = time.time()

    def stats(vals):
        v = [x for x in vals if x is not None]
        if not v:
            return "n/d"
        v.sort()
        p95 = v[min(len(v) - 1, int(len(v) * 0.95))]
        return f"mediana {statistics.median(v):.1f} ms, p95 {p95:.1f} ms"

    idle = [m for t, m in lat if t < t_start]
    load = [m for t, m in lat if t_start <= t <= t_end]
    lost = sum(1 for m in load if m is None)
    steady = samples[2:] or samples
    if steady:
        avg_d = statistics.mean(s[0] for s in steady)
        avg_u = statistics.mean(s[1] for s in steady)
        peak = max(s[0] + s[1] for s in samples)
        print("\n  -- Resultado --------------------------------")
        print(f"  Media estable   v {avg_d:.1f} Mbps   ^ {avg_u:.1f} Mbps   (total {avg_d + avg_u:.1f})")
        print(f"  Pico total      {peak:.1f} Mbps")
        print(f"  Latencia reposo {stats(idle)}")
        print(f"  Latencia carga  {stats(load)}   perdidas: {lost}/{len(load)}")
    print()


def multiclient(url, streams, secs, mode, copies, bind_ips=None):
    """Lanza varias copias del cliente EN PARALELO desde este mismo proceso
    (varios procesos hijos), para sumar mas carga sin abrir varias terminales
    a mano. Si pasas bind_ips (una IP, o varias separadas por coma -- una por
    interfaz/radio WiFi del equipo), las copias se reparten entre ellas, asi
    que con dos interfaces (p.ej. WiFi interna + adaptador USB en la otra
    banda) puedes golpear ambas bandas a la vez desde un unico portatil."""
    ips = bind_ips or [None]
    print(f"\n  Lanzando {copies} clientes en paralelo contra {url} "
          f"(cada uno con -n {streams} -t {secs} -m {mode})"
          + (f"  repartidos entre: {', '.join(ips)}" if bind_ips else "") + "\n")
    procs = []
    for i in range(copies):
        cmd = [sys.executable, __file__, "client", url,
               "-n", str(streams), "-t", str(secs), "-m", mode]
        ip = ips[i % len(ips)]
        if ip:
            cmd += ["--bind-ip", ip]
        procs.append(subprocess.Popen(cmd))
    try:
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()


# --------------------------------------------------------------------------- interfaz web
HTML = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WiFi Stress Test</title>
<style>
:root{color-scheme:dark;--bg:#121211;--surface:#1a1a19;--line:#2c2c2a;--grid:#262624;
--t1:#f0efec;--t2:#c3c2b7;--t3:#8d8c84;--down:#3987e5;--up:#d95926;--lat:#199e70;--bad:#e66767;--ok:#0ca30c;--warn:#c98500}
@media (prefers-color-scheme:light){:root{color-scheme:light;--bg:#f4f3f0;--surface:#fcfcfb;--line:#dcdbd5;--grid:#ecebe6;
--t1:#0b0b0b;--t2:#52514e;--t3:#7a7973;--down:#2a78d6;--up:#eb6834;--lat:#1baf7a;--bad:#e34948;--ok:#008300;--warn:#eda100}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--t1);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1000px;margin:0 auto;padding:18px 14px 60px}
h1{font-size:20px;margin:0 0 2px} .sub{color:var(--t3);font-size:13px;margin-bottom:16px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:12px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
.lbl{font-size:12px;color:var(--t3);text-transform:uppercase;letter-spacing:.04em}
.big{font-size:34px;font-weight:650;font-variant-numeric:tabular-nums;line-height:1.15}
.big small{font-size:14px;color:var(--t3);font-weight:400}
.row{display:flex;gap:18px;flex-wrap:wrap;align-items:baseline}
.dot{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px}
.controls{display:flex;gap:14px;flex-wrap:wrap;align-items:end}
.controls label{display:flex;flex-direction:column;font-size:12px;color:var(--t3);gap:4px}
select,input[type=range]{font:inherit;color:var(--t1)}
select{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:7px 9px}
button{font:inherit;font-weight:600;border:0;border-radius:10px;padding:10px 22px;cursor:pointer;background:var(--down);color:#fff}
button.stop{background:var(--bad)} button:disabled{opacity:.5}
canvas{width:100%;display:block}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:14px}
th{font-size:12px;color:var(--t3);font-weight:500;text-align:right;padding:4px 8px;border-bottom:1px solid var(--line)}
td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--grid)} th:first-child,td:first-child{text-align:left}
.note{color:var(--t3);font-size:12.5px} .legend{display:flex;gap:16px;font-size:13px;color:var(--t2);margin-bottom:6px;flex-wrap:wrap}
.tip{position:fixed;pointer-events:none;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:6px 9px;font-size:12.5px;display:none;z-index:9}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:600;color:#fff}
</style></head><body><main>
<h1>WiFi Stress Test</h1>
<div class="sub">Mide cuanto rendimiento da tu red local y en que punto se satura. Abrelo desde varios dispositivos a la vez para sumar carga.</div>

<div class="card"><div class="controls">
  <label>Modo<select id="mode">
    <option value="ramp">Escalonado (busca el punto de saturacion)</option>
    <option value="fixed">Fijo (carga constante)</option></select></label>
  <label>Direccion<select id="dir">
    <option value="both">Bajada + subida</option><option value="down">Solo bajada</option><option value="up">Solo subida</option></select></label>
  <label id="lblN">Conexiones: <b id="nv">24</b><input id="n" type="range" min="1" max="36" value="24"></label>
  <label id="lblT">Duracion<select id="secs"><option>10</option><option selected>20</option><option>30</option><option>60</option><option>120</option></select></label>
  <button id="go">Iniciar</button>
</div>
<div class="note" style="margin-top:10px" id="portsNote">Escalonado: sube de 1 a 6 conexiones (6 s cada nivel). Cuando el rendimiento deja de crecer, ese es tu techo.</div></div>

<div class="grid">
  <div class="card"><div class="lbl">Este dispositivo</div>
    <div class="row"><div class="big" style="color:var(--down)">v <span id="myD">0</span> <small>Mbps</small></div>
    <div class="big" style="color:var(--up)">^ <span id="myU">0</span> <small>Mbps</small></div></div></div>
  <div class="card"><div class="lbl">Toda la red (todos los dispositivos)</div>
    <div class="row"><div class="big" style="color:var(--down)">v <span id="netD">0</span> <small>Mbps</small></div>
    <div class="big" style="color:var(--up)">^ <span id="netU">0</span> <small>Mbps</small></div></div>
    <div class="note">Pico total: <b id="peak">0</b> Mbps</div></div>
  <div class="card"><div class="lbl">Latencia (ping)</div>
    <div class="big" style="color:var(--lat)"><span id="latNow">-</span> <small>ms</small></div>
    <div class="note">reposo <b id="latIdle">-</b> ms - carga <b id="latLoad">-</b> ms - <span id="bloat"></span></div></div>
</div>

<div class="card"><div class="lbl">Rendimiento (Mbps)</div>
  <div class="legend"><span><i class="dot" style="background:var(--down)"></i>Bajada (red total)</span><span><i class="dot" style="background:var(--up)"></i>Subida (red total)</span></div>
  <canvas id="c1" height="210"></canvas></div>
<div class="card"><div class="lbl">Latencia bajo carga (ms)</div>
  <div class="legend"><span><i class="dot" style="background:var(--lat)"></i>Ping desde este dispositivo</span><span id="jit" class="note"></span></div>
  <canvas id="c2" height="130"></canvas></div>

<div class="card" id="resCard" style="display:none"><div class="lbl">Resultado por nivel de carga</div>
  <table><thead><tr><th>Conexiones</th><th>Este disp. v</th><th>Este disp. ^</th><th>Red total</th><th>Ping</th></tr></thead><tbody id="res"></tbody></table>
  <div class="note" id="verdict" style="margin-top:8px;color:var(--t2)"></div></div>

<div class="card"><div class="lbl">Dispositivos conectados al servidor</div>
  <table><thead><tr><th>IP</th><th>v Mbps</th><th>^ Mbps</th><th>Transferido</th></tr></thead><tbody id="devs"><tr><td colspan="4" class="note">Sin actividad todavia</td></tr></tbody></table></div>
<div class="tip" id="tip"></div>
</main>
<script>
const $=id=>document.getElementById(id), sleep=ms=>new Promise(r=>setTimeout(r,ms));
const PING_BASE=location.protocol+'//'+location.hostname+':__PINGPORT__';
const PORTS=__PORTS__;   // varios puertos = varios "origenes" = se salta el limite de 6 conexiones/origen del navegador
const portBase=i=>location.protocol+'//'+location.hostname+':'+PORTS[i%PORTS.length];
$('portsNote').textContent=`Escalonado: sube hasta ${Math.min(36,PORTS.length*6)} conexiones repartidas en ${PORTS.length} puertos (asi se evita el limite de 6 conexiones por origen del navegador). Cuando el rendimiento deja de crecer, ese es tu techo. Para aun mas carga, o para pegar en dos bandas WiFi a la vez desde el mismo equipo, usa el cliente de consola (ver --ayuda-bandas).`;
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
let running=false, myD=0, myU=0, workers=[], mode='fixed', wseq=0;
const hist=[];          // {t, nd, nu, md, mu}
const lat=[];           // {t, ms|null, phase}
let phase='idle', peak=0, lastStats=null, lastTick=null, devRates={};
const ulBlob=(()=>{const a=new Uint8Array(2<<20);for(let i=0;i<a.length;i+=65536)crypto.getRandomValues(a.subarray(i,i+65536));return new Blob([a])})();

$('n').oninput=()=>$('nv').textContent=$('n').value;
$('mode').onchange=()=>{const r=$('mode').value==='ramp';$('lblN').style.display=r?'none':'flex';$('lblT').style.display=r?'none':'flex'};
$('mode').onchange();

/* ---------- generadores de carga ---------- */
function startWorker(kind){
  const base=portBase(wseq++);
  const w={kind,stop:false,ac:new AbortController(),xhr:null};
  (async()=>{
    while(!w.stop){
      try{
        if(kind==='down'){
          const r=await fetch(base+'/download?secs=120&r='+Math.random(),{signal:w.ac.signal,cache:'no-store'});
          const rd=r.body.getReader();
          for(;;){const {done,value}=await rd.read(); if(done||w.stop)break; myD+=value.length}
        }else{
          await new Promise(res=>{
            const x=new XMLHttpRequest(); w.xhr=x; let last=0;
            x.open('POST',base+'/upload'); x.upload.onprogress=e=>{myU+=e.loaded-last;last=e.loaded};
            x.onload=x.onerror=x.onabort=()=>res(); x.send(ulBlob);
          });
        }
      }catch(e){ if(w.stop)return; await sleep(300) }
    }
  })();
  workers.push(w);
}
function stopWorker(w){w.stop=true;try{w.ac.abort()}catch(e){};try{w.xhr&&w.xhr.abort()}catch(e){}}
function setLoad(n,dir){
  let wd=0,wu=0;
  if(dir==='down')wd=n; else if(dir==='up')wu=n; else {wd=Math.ceil(n/2);wu=Math.max(1,Math.floor(n/2))}
  for(const [kind,want] of [['down',wd],['up',wu]]){
    let cur=workers.filter(w=>w.kind===kind&&!w.stop);
    while(cur.length<want){startWorker(kind);cur=workers.filter(w=>w.kind===kind&&!w.stop)}
    while(cur.length>want){stopWorker(cur.pop())}
  }
  workers=workers.filter(w=>!w.stop);
}
function stopAll(){workers.forEach(stopWorker);workers=[]}

/* ---------- ping continuo (2o puerto para no hacer cola) ---------- */
(async function pinger(){
  for(;;){
    const t0=performance.now(), ac=new AbortController(), to=setTimeout(()=>ac.abort(),2000);
    let ms=null;
    try{await fetch(PING_BASE+'/ping?'+Math.random(),{cache:'no-store',signal:ac.signal}); ms=performance.now()-t0}catch(e){}
    clearTimeout(to); lat.push({t:Date.now(),ms,phase}); if(lat.length>3000)lat.shift();
    await sleep(200);
  }
})();

/* ---------- muestreo cada 500 ms ---------- */
let pD=0,pU=0,pT=performance.now(),smD=0,smU=0;
setInterval(async()=>{
  const now=performance.now(), dt=(now-pT)/1000; pT=now;
  const mD=(myD-pD)*8/dt/1e6, mU=(myU-pU)*8/dt/1e6; pD=myD; pU=myU;
  let nd=null,nu=null;
  try{
    const s=await (await fetch(PING_BASE+'/stats',{cache:'no-store'})).json();
    if(lastStats){const d=s.t-lastStats.t; if(d>0){
      nd=(s.tx-lastStats.tx)*8/d/1e6; nu=(s.rx-lastStats.rx)*8/d/1e6;
      devRates={};
      for(const ip in s.clients){const c=s.clients[ip],p=lastStats.clients[ip]||{tx:0,rx:0};
        devRates[ip]={d:(c.tx-p.tx)*8/d/1e6,u:(c.rx-p.rx)*8/d/1e6,tot:c.tx+c.rx,age:c.age,you:ip===s.you}}
    }}
    lastStats=s;
  }catch(e){}
  hist.push({t:Date.now(),md:mD,mu:mU,nd,nu,phase}); if(hist.length>400)hist.shift();
  if(nd!=null)peak=Math.max(peak,nd+nu);
  smD=smD*.4+mD*.6; smU=smU*.4+mU*.6;
  $('myD').textContent=smD.toFixed(1); $('myU').textContent=smU.toFixed(1);
  if(nd!=null){$('netD').textContent=nd.toFixed(1);$('netU').textContent=nu.toFixed(1)}
  $('peak').textContent=peak.toFixed(0);
  const last=lat[lat.length-1]; $('latNow').textContent=last&&last.ms!=null?last.ms.toFixed(1):'-';
  renderDevs(); draw();
},500);

function renderDevs(){
  const ips=Object.keys(devRates).sort(); if(!ips.length)return;
  $('devs').innerHTML=ips.map(ip=>{const r=devRates[ip],act=r.age<3;
    return `<tr style="${act?'':'opacity:.45'}"><td>${ip}${r.you?' <span class="note">(tu)</span>':''}</td><td>${r.d.toFixed(1)}</td><td>${r.u.toFixed(1)}</td><td>${(r.tot/1e6).toFixed(0)} MB</td></tr>`}).join('');
}

/* ---------- graficas (canvas, sin librerias) ---------- */
function setup(c){const dpr=devicePixelRatio||1,w=c.clientWidth,h=+c.getAttribute('height');c.width=w*dpr;c.height=h*dpr;const g=c.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);return{g,w,h}}
function niceMax(v){if(v<=10)return 10;const p=Math.pow(10,Math.floor(Math.log10(v))),m=v/p;return (m<=1.5?1.5:m<=2?2:m<=3?3:m<=5?5:10)*p}
const SPAN=90000; let hover=null;
function drawSeries(c,series,yfmt,minMax){
  const {g,w,h}=setup(c), L=46,R=10,T=8,B=20, now=Date.now(), t0=now-SPAN;
  let mx=minMax; for(const s of series)for(const p of s.pts)if(p.v!=null&&p.t>=t0)mx=Math.max(mx,p.v);
  mx=niceMax(mx*1.05);
  const X=t=>L+(t-t0)/SPAN*(w-L-R), Y=v=>T+(1-v/mx)*(h-T-B);
  g.font='11px system-ui';g.fillStyle=css('--t3');g.strokeStyle=css('--grid');g.lineWidth=1;
  for(let i=0;i<=4;i++){const v=mx*i/4,y=Y(v);g.beginPath();g.moveTo(L,y);g.lineTo(w-R,y);g.stroke();g.textAlign='right';g.fillText(yfmt(v),L-6,y+4)}
  g.textAlign='center'; for(let s=0;s<=90;s+=30){g.fillText(s===0?'ahora':'-'+s+'s',X(now-s*1000),h-4)}
  for(const s of series){
    g.strokeStyle=css(s.color);g.lineWidth=2;g.lineJoin='round';g.beginPath();let on=false;
    for(const p of s.pts){if(p.t<t0)continue; if(p.v==null){on=false;continue} const x=X(p.t),y=Y(p.v); if(!on){g.moveTo(x,y);on=true}else g.lineTo(x,y)}
    g.stroke();
  }
  c._m={series,X,L,R,w,t0,now,Y,yfmt};
}
function draw(){
  drawSeries($('c1'),[
    {name:'Bajada',color:'--down',pts:hist.map(p=>({t:p.t,v:p.nd}))},
    {name:'Subida',color:'--up',pts:hist.map(p=>({t:p.t,v:p.nu}))}],v=>v>=100?v.toFixed(0):v.toFixed(1),20);
  drawSeries($('c2'),[{name:'Ping',color:'--lat',pts:lat.map(p=>({t:p.t,v:p.ms}))}],v=>v.toFixed(0),20);
  const win=lat.filter(p=>p.phase==='idle'&&p.ms!=null).map(p=>p.ms), ld=lat.filter(p=>p.phase==='load');
  const lv=ld.filter(p=>p.ms!=null).map(p=>p.ms), med=a=>a.length?a.slice().sort((x,y)=>x-y)[a.length>>1]:null;
  const mi=med(win.slice(-40)), ml=med(lv);
  $('latIdle').textContent=mi!=null?mi.toFixed(1):'-'; $('latLoad').textContent=ml!=null?ml.toFixed(1):'-';
  if(mi!=null&&ml!=null){const d=ml-mi,g=d<30?['Excelente','--ok']:d<80?['Aceptable','--warn']:['Bufferbloat alto','--bad'];
    $('bloat').innerHTML=`<span class="pill" style="background:${css(g[1])}">${g[0]} (+${d.toFixed(0)} ms)</span>`}
  if(lv.length>5){let j=0;for(let i=1;i<lv.length;i++)j+=Math.abs(lv[i]-lv[i-1]);const lost=ld.length-lv.length;
    $('jit').textContent=`jitter ${(j/(lv.length-1)).toFixed(1)} ms - perdidas ${lost}/${ld.length}`}
}
for(const id of ['c1','c2']){const c=$(id);
  c.onmousemove=e=>{const m=c._m;if(!m)return;const r=c.getBoundingClientRect(),x=e.clientX-r.left,t=m.t0+(x-m.L)/(m.w-m.L-m.R)*SPAN;
    const rows=m.series.map(s=>{let b=null,bd=1e9;for(const p of s.pts){const d=Math.abs(p.t-t);if(d<bd&&p.v!=null){bd=d;b=p}}return b&&bd<2000?`<span style="color:${css(s.color)}">.</span> ${s.name}: <b>${m.yfmt(b.v)}</b>`:null}).filter(Boolean);
    const tip=$('tip');if(!rows.length){tip.style.display='none';return}
    tip.innerHTML=rows.join('<br>');tip.style.display='block';tip.style.left=Math.min(e.clientX+12,innerWidth-170)+'px';tip.style.top=(e.clientY+12)+'px'};
  c.onmouseleave=()=>$('tip').style.display='none'}
addEventListener('resize',draw);

/* ---------- ejecucion de pruebas ---------- */
const avg=a=>a.length?a.reduce((x,y)=>x+y,0)/a.length:0;
async function runLevel(n,dir,secs){
  setLoad(n,dir); const t0=Date.now();
  await sleep(secs*1000); if(!running)return null;
  const w0=t0+Math.min(2500,secs*400); const H=hist.filter(p=>p.t>=w0&&p.nd!=null), M=hist.filter(p=>p.t>=w0);
  const L=lat.filter(p=>p.t>=w0&&p.ms!=null).map(p=>p.ms).sort((a,b)=>a-b);
  return{n,md:avg(M.map(p=>p.md)),mu:avg(M.map(p=>p.mu)),net:avg(H.map(p=>p.nd+p.nu)),lat:L.length?L[L.length>>1]:null};
}
$('go').onclick=async()=>{
  if(running){running=false;stopAll();phase='idle';$('go').textContent='Iniciar';$('go').className='';return}
  running=true;$('go').textContent='Detener';$('go').className='stop';
  $('res').innerHTML='';$('resCard').style.display='none';$('verdict').textContent='';peak=0;
  const dir=$('dir').value, res=[];
  phase='idle'; await sleep(3000);               // linea base de latencia
  if(!running)return; phase='load';
  if($('mode').value==='ramp'){
    const top=Math.min(36,PORTS.length*6), levels=[];
    for(let n=1;n<=top;n=n<6?n+1:n+Math.ceil(top/9)) levels.push(n);
    if(levels[levels.length-1]!==top) levels.push(top);
    for(const n of levels){
      if(!running)break;
      const r=await runLevel(n,dir,6); if(!r)break; res.push(r);
      $('resCard').style.display='block';
      $('res').innerHTML=res.map(r=>`<tr><td>${r.n}</td><td>${r.md.toFixed(1)}</td><td>${r.mu.toFixed(1)}</td><td><b>${r.net.toFixed(1)}</b></td><td>${r.lat!=null?r.lat.toFixed(0)+' ms':'-'}</td></tr>`).join('');
    }
    if(res.length>1){const best=res.reduce((a,b)=>b.net>a.net?b:a);
      let sat=res.find((r,i)=>i>0&&r.net<res[i-1].net*1.05);
      $('verdict').textContent=`Maximo medido en la red: ${best.net.toFixed(0)} Mbps con ${best.n} conexiones.`+(sat?` El rendimiento dejo de crecer a partir de ${sat.n} conexiones: ahi empieza la saturacion.`:' Sigue creciendo: prueba con mas dispositivos a la vez.')}
  }else{
    const r=await runLevel(+$('n').value,dir,+$('secs').value);
    if(r){$('resCard').style.display='block';$('res').innerHTML=`<tr><td>${r.n}</td><td>${r.md.toFixed(1)}</td><td>${r.mu.toFixed(1)}</td><td><b>${r.net.toFixed(1)}</b></td><td>${r.lat!=null?r.lat.toFixed(0)+' ms':'-'}</td></tr>`}
  }
  stopAll();running=false;phase='idle';$('go').textContent='Iniciar';$('go').className='';
};
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description="Prueba de saturacion de WiFi local")
    ap.add_argument("--ayuda-bandas", action="store_true",
                     help="explica por que unos dispositivos notan la saturacion y otros no")
    sub = ap.add_subparsers(dest="cmd")
    sv = sub.add_parser("serve", help="(por defecto) servidor + interfaz web")
    for p in (ap, sv):
        p.add_argument("--host", default="0.0.0.0")
        p.add_argument("--port", type=int, default=8765)
    cl = sub.add_parser("client", help="cliente de consola")
    cl.add_argument("url", help="p. ej. http://192.168.1.10:8765")
    cl.add_argument("-n", "--streams", type=int, default=24)      # antes 8
    cl.add_argument("-t", "--secs", type=int, default=30)         # antes 20
    cl.add_argument("-m", "--mode", choices=["down", "up", "both"], default="both")
    cl.add_argument("--bind-ip", default=None,
                     help="IP local de la interfaz de red por la que salen las conexiones "
                          "(ifconfig/ipconfig). Util para usar una interfaz WiFi concreta "
                          "cuando el equipo tiene mas de una (p.ej. adaptador USB en 2.4GHz "
                          "y WiFi interna en 5GHz).")
    mc = sub.add_parser("multiclient", help="lanza varias copias del cliente en paralelo")
    mc.add_argument("url")
    mc.add_argument("-n", "--streams", type=int, default=24)
    mc.add_argument("-t", "--secs", type=int, default=60)
    mc.add_argument("-m", "--mode", choices=["down", "up", "both"], default="both")
    mc.add_argument("-c", "--copies", type=int, default=3, help="procesos cliente en paralelo")
    mc.add_argument("--bind-ips", default=None,
                     help="una o varias IP locales separadas por coma; las copias se reparten "
                          "entre ellas (round-robin). Con dos interfaces WiFi en el mismo equipo, "
                          "pasa las dos IP para pegar en ambas bandas a la vez.")
    a = ap.parse_args()
    if a.ayuda_bandas:
        print(BANDAS_AYUDA)
        return
    if a.cmd == "client":
        client(a.url, a.streams, a.secs, a.mode, bind_ip=a.bind_ip)
    elif a.cmd == "multiclient":
        bind_ips = a.bind_ips.split(",") if a.bind_ips else None
        multiclient(a.url, a.streams, a.secs, a.mode, a.copies, bind_ips=bind_ips)
    else:
        serve(a.host, a.port)


if __name__ == "__main__":
    main()
