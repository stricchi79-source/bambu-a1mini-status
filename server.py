"""Schermino di stato e comandi per la Bambu Lab A1 mini (LAN), da aprire sul telefono.

Uso: python server.py [nome_stampante] [porta] [--ntfy ARGOMENTO]
     (default: l'unica in printers.toml, porta 8090, notifiche spente)
Dal telefono apri http://IP-DEL-PC:8090  (vista con anche la Ender: http://IP-DEL-PC:8090/entrambe)

Con --ntfy arriva una notifica sul telefono (app gratuita ntfy, e quindi anche sullo
smartwatch che ripete le notifiche) quando la stampa inizia, va in pausa, finisce,
si interrompe o va in errore.

- GET  /status   stato in JSON (via MQTT, riusa bambu_server.py)
- GET  /cam.jpg  ultimo fotogramma della telecamera (porta 6000)
- POST /cmd      {"action": "pause"|"resume"|"stop"|"light_on"|"light_off"|"speed", "level": 1-4}

L'avvio di nuove stampe NON è previsto da qui: lo fa Claude, dopo conferma.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)   # bambu_server.py sta accanto a questo file

# Avviato con pythonw (esecuzione automatica, senza finestra): niente console,
# quindi i messaggi vanno in server.log accanto a questo file.
if sys.stdout is None or sys.stderr is None:
    _log = open(os.path.join(HERE, "server.log"), "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log
    print(f"--- avvio {time.strftime('%Y-%m-%d %H:%M:%S')} ---", flush=True)
import bambu_server as bs  # noqa: E402

import argparse  # noqa: E402
import urllib.request  # noqa: E402

_ap = argparse.ArgumentParser(description="Schermino di stato per Bambu A1 mini")
_ap.add_argument("printer", nargs="?", default=None, help="nome in printers.toml (se ce n'è più d'una)")
_ap.add_argument("port", nargs="?", type=int, default=int(os.environ.get("A1_PORT", 8090)))
_ap.add_argument("--ntfy", default=os.environ.get("A1_NTFY", ""),
                 help="argomento ntfy per le notifiche (nome lungo e casuale), vuoto = spente")
_ap.add_argument("--ntfy-server", default=os.environ.get("A1_NTFY_SERVER", "https://ntfy.sh"))
_ap.add_argument("--nome", default="A1 mini", help="nome della stampante nelle notifiche")
# compatibilità con "python server.py 8090"
_argv = sys.argv[1:]
if _argv and _argv[0].isdigit():
    _argv = [""] + _argv
ARGS = _ap.parse_args(_argv)

PRINTER_NAME = ARGS.printer or None
PORT = ARGS.port

NAME, PRINTER = bs.get_printer(PRINTER_NAME)

STAGES = {
    1: "livellamento piatto", 2: "preriscaldamento piatto", 3: "calibrazione assi", 4: "cambio filamento",
    5: "pausa (M400)", 6: "pausa: filamento finito", 7: "riscaldamento ugello", 8: "calibrazione estrusione",
    9: "scansione piatto", 10: "controllo primo layer", 11: "riconoscimento piatto", 12: "calibrazione lidar",
    13: "homing", 14: "pulizia ugello", 15: "controllo temperatura ugello", 16: "pausa utente",
    17: "pausa: coperchio", 18: "calibrazione lidar", 19: "calibrazione flusso", 20: "pausa: anomalia ugello",
    21: "pausa: anomalia piatto", 22: "scarico filamento", 23: "pausa: passo saltato", 24: "carico filamento",
    25: "calibrazione rumore motori", 26: "pausa: AMS perso", 27: "pausa: ventola", 29: "raffreddamento",
    30: "pausa da G-code", 31: "calibrazione rumore motori", 32: "pausa: ugello coperto", 33: "pausa: taglierina",
    34: "pausa: errore primo layer", 35: "pausa: ugello intasato",
}
SPEEDS = {1: "silenziosa", 2: "standard", 3: "sport", 4: "ludicrous"}


def _pct_fan(v):
    """Ventole Bambu: 0-15 -> percentuale."""
    try:
        return round(int(v) / 15 * 100)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- stato

def snapshot() -> dict:
    try:
        state, updated = PRINTER.get_state(refresh=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "printer": NAME, "ip": PRINTER.ip}
    pr = state.get("print") or {}
    st = bs.parse_status(NAME, PRINTER, state, updated)
    vt = pr.get("vt_tray") or {}
    lights = {x.get("node"): x.get("mode") for x in (pr.get("lights_report") or []) if isinstance(x, dict)}
    stg = pr.get("stg_cur")
    return {
        "ok": True,
        "printer": NAME,
        "ip": PRINTER.ip,
        "age": round(time.time() - updated, 1) if updated else None,
        "state": st["state"],
        "state_description": st["state_description"],
        "stage": STAGES.get(stg) if isinstance(stg, int) and stg > 0 else None,
        "job": (st["progress"].get("job_name") or "").removesuffix(".gcode.3mf").removesuffix(".3mf"),
        "percent": st["progress"].get("percent") or 0,
        "layer": st["progress"].get("layer") or 0,
        "total_layers": st["progress"].get("total_layers") or 0,
        "remaining_min": st["progress"].get("remaining_minutes") or 0,
        "nozzle": st["temperatures"]["nozzles"][0] if st["temperatures"]["nozzles"] else {},
        "bed": st["temperatures"]["bed"],
        "speed_level": pr.get("spd_lvl"),
        "speed_name": SPEEDS.get(pr.get("spd_lvl")),
        "speed_pct": pr.get("spd_mag"),
        "fan_part": _pct_fan(pr.get("cooling_fan_speed")),
        "fan_aux": _pct_fan(pr.get("big_fan1_speed")),
        "wifi": pr.get("wifi_signal"),
        "light": lights.get("chamber_light"),
        "filament": {"type": vt.get("tray_type") or None, "color": "#" + (vt.get("tray_color") or "")[:6]
                     if vt.get("tray_color") else None},
        "error": st.get("print_error_code"),
        "hms": st.get("hms_alerts") or [],
        "sdcard": pr.get("sdcard"),
    }


def command(action: str, level: int | None = None) -> dict:
    state, _ = PRINTER.get_state(refresh=False)
    g = (state.get("print") or {}).get("gcode_state", "UNKNOWN")
    if action == "pause":
        if g not in ("RUNNING", "PREPARE"):
            return {"ok": False, "message": "Nessuna stampa da mettere in pausa."}
        res = PRINTER.print_command("pause")
    elif action == "resume":
        if g != "PAUSE":
            return {"ok": False, "message": "La stampa non è in pausa."}
        res = PRINTER.print_command("resume")
    elif action == "stop":
        if g not in ("RUNNING", "PAUSE", "PREPARE"):
            return {"ok": False, "message": "Nessuna stampa da fermare."}
        res = PRINTER.print_command("stop")
    elif action in ("light_on", "light_off"):
        res = PRINTER.send_and_wait({"system": {
            "command": "ledctrl", "led_node": "chamber_light", "led_mode": "on" if action == "light_on" else "off",
            "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}})
    elif action == "speed":
        if level not in SPEEDS:
            return {"ok": False, "message": "Velocità non valida."}
        if g not in ("RUNNING", "PAUSE", "PREPARE"):
            return {"ok": False, "message": "La velocità si cambia durante la stampa."}
        res = PRINTER.send_and_wait({"print": {"command": "print_speed", "param": str(level)}})
    else:
        return {"ok": False, "message": "Comando sconosciuto."}
    ok = not res.get("acknowledged") or res.get("result") in (None, "success", "SUCCESS")
    return {"ok": ok, "acknowledged": res.get("acknowledged"),
            "message": "Fatto" if ok else f"Rifiutato: {res.get('reason') or res.get('result')}"}


# --------------------------------------------------------------------------- notifiche (ntfy)

def notifica(titolo: str, testo: str, tag: str, priorita: int = 4) -> None:
    if not ARGS.ntfy:
        return
    corpo = json.dumps({"topic": ARGS.ntfy, "title": f"{ARGS.nome}: {titolo}", "message": testo,
                        "tags": [tag], "priority": priorita}).encode()

    def invia():
        try:
            req = urllib.request.Request(ARGS.ntfy_server.rstrip("/") + "/", data=corpo,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).close()
        except Exception as e:  # noqa: BLE001
            print("ntfy:", e, flush=True)

    threading.Thread(target=invia, daemon=True).start()


class Osservatore:
    """Controlla lo stato ogni pochi secondi e avvisa sui cambi importanti."""

    ATTIVI = ("PREPARE", "RUNNING", "PAUSE")

    def __init__(self):
        self.stato = None
        self.errore = 0
        self.inizio = None

    def controlla(self, pr: dict) -> None:
        st = pr.get("gcode_state")
        if not st:
            return
        nome = (pr.get("subtask_name") or pr.get("gcode_file") or "stampa").removesuffix(".gcode.3mf")
        prima = self.stato
        if prima is not None and st != prima:
            if st in ("PREPARE", "RUNNING") and prima not in self.ATTIVI:
                self.inizio = time.time()
                notifica("stampa iniziata", nome, "printer", 2)
            elif st == "RUNNING" and prima == "PAUSE":
                notifica("stampa ripresa", nome, "arrow_forward", 2)
            elif st == "PAUSE":
                fase = STAGES.get(pr.get("stg_cur"))
                notifica("stampa in pausa ⏸", nome + (f" ({fase})" if fase else ""), "pause_button")
            elif st == "FINISH" and prima in self.ATTIVI:
                extra = ""
                if self.inizio:
                    d = time.time() - self.inizio
                    extra = f" in {int(d // 3600)}h {int(d % 3600 // 60):02d}m"
                notifica("stampa finita ✅", f"{nome} completata{extra}.", "white_check_mark")
            elif st == "FAILED" and prima in self.ATTIVI:
                notifica("stampa interrotta", nome, "x", 4)
        err = pr.get("print_error") or 0
        if isinstance(err, int) and err and err != self.errore:
            notifica("errore ⚠️", f"Codice {err:08X} durante {nome}.", "warning", 5)
        self.errore = err if isinstance(err, int) else 0
        if st not in self.ATTIVI:
            self.inizio = None if st in ("IDLE",) else self.inizio
        self.stato = st


def sorveglia():
    oss = Osservatore()
    while True:
        try:
            state, _ = PRINTER.get_state(refresh=True)
            oss.controlla(state.get("print") or {})
        except Exception as e:  # noqa: BLE001
            print("sorveglianza:", e, flush=True)
        time.sleep(5)


# --------------------------------------------------------------------------- Ender (vista "entrambe")

ENDER_STATUS_URL = os.environ.get("ENDER_STATUS_URL", "http://127.0.0.1:80/status")


def ender_status() -> dict:
    """Stato della Ender-3 V3 KE letto dal suo server (ender_status/server.py) sullo stesso PC."""
    import urllib.request
    try:
        with urllib.request.urlopen(ENDER_STATUS_URL, timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"down": True, "error": str(e)}


# --------------------------------------------------------------------------- telecamera

class Camera:
    """Stream JPEG della A1/P1 sulla porta 6000 (TLS). Attivo solo se qualcuno guarda."""

    def __init__(self, ip: str, code: str):
        self.ip, self.code = ip, code
        self.frame: bytes | None = None
        self.frame_time = 0.0
        self.wanted_until = 0.0
        self.error: str | None = None
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def want(self):
        self.wanted_until = time.time() + 20

    def _recv(self, s, n):
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("chiusa dalla stampante")
            buf += chunk
        return buf

    def _run(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        auth = struct.pack("<IIII", 0x40, 0x3000, 0, 0) + b"bblp".ljust(32, b"\0") + self.code.encode().ljust(32, b"\0")
        while True:
            if time.time() > self.wanted_until:
                time.sleep(0.5)
                continue
            try:
                with socket.create_connection((self.ip, 6000), timeout=10) as raw, \
                        ctx.wrap_socket(raw, server_hostname=self.ip) as s:
                    s.sendall(auth)
                    while time.time() < self.wanted_until:
                        hdr = self._recv(s, 16)
                        size = int.from_bytes(hdr[0:3], "little")
                        data = self._recv(s, size)
                        if data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9":
                            with self._lock:
                                self.frame, self.frame_time, self.error = data, time.time(), None
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
                time.sleep(3)

    def get(self):
        with self._lock:
            return self.frame, self.frame_time


CAM = Camera(PRINTER.ip, PRINTER.access_code)


# --------------------------------------------------------------------------- HTTP

class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=HERE, **k)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/status"):
            self._json(snapshot())
        elif self.path.startswith("/cam.jpg"):
            CAM.want()
            frame, t = CAM.get()
            if not frame or time.time() - t > 15:
                return self._json({"error": CAM.error or "in attesa della telecamera"}, 503)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(frame)))
            self.end_headers()
            self.wfile.write(frame)
        elif self.path.startswith("/ender"):
            self._json(ender_status())
        elif self.path.split("?")[0].rstrip("/") in ("/entrambe", "/entrambe.html"):
            self.path = "/entrambe.html"
            super().do_GET()
        elif self.path.rstrip("/") in ("", "/index.html") or self.path.startswith(("/?", "/manifest", "/icon")):
            super().do_GET()
        else:
            self.send_error(404)

    def do_POST(self):
        if not self.path.startswith("/cmd"):
            return self.send_error(404)
        req = None
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            res = command(str(req.get("action")), req.get("level"))
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "message": str(e)}
        print(f"[cmd] {self.client_address[0]} {req} -> {res}", flush=True)
        self._json(res)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"A1 mini: http://0.0.0.0:{PORT}  (stampante {NAME} @ {PRINTER.ip})", flush=True)
    if ARGS.ntfy:
        print(f"Notifiche ntfy attive: {ARGS.ntfy_server.rstrip('/')}/{ARGS.ntfy}", flush=True)
        threading.Thread(target=sorveglia, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
