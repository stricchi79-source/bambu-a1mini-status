"""
Server MCP per stampanti Bambu Lab in rete locale (modalità solo LAN + sviluppatore).

- MQTT su TLS (porta 8883): stato, temperature, avanzamento, pausa / ripresa / stop.
- FTPS implicito (porta 990): elenco e caricamento file sulla scheda SD.
Le stampanti sono definite in printers.toml (oppure nel file indicato
dalla variabile d'ambiente BAMBU_CONFIG).
"""

from __future__ import annotations

import ftplib
import json
import os
import posixpath
import socket
import ssl
import sys
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from mcp.server.fastmcp import FastMCP

MQTT_PORT = 8883
MQTT_USER = "bblp"
CONNECT_TIMEOUT = 10.0
FIRST_REPORT_TIMEOUT = 8.0
COMMAND_ACK_TIMEOUT = 6.0
PUSHALL_MIN_INTERVAL = 30.0  # i modelli P1 limitano la frequenza di pushall
FTPS_PORT = 990
FTPS_TIMEOUT = 30.0
UPLOAD_EXTENSIONS = {".3mf", ".gcode"}

GCODE_STATES = {
    "IDLE": "inattiva",
    "PREPARE": "in preparazione",
    "RUNNING": "in stampa",
    "PAUSE": "in pausa",
    "FINISH": "stampa completata",
    "FAILED": "stampa fallita / interrotta",
    "SLICING": "slicing in corso",
}


def log(msg: str) -> None:
    # stdout è riservato al protocollo MCP: i log vanno su stderr
    print(f"[bambu-mcp] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

def config_path() -> Path:
    """BAMBU_CONFIG se impostata, altrimenti printers.toml accanto a questo file o nella cartella sopra."""
    env = os.environ.get("BAMBU_CONFIG")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    for cand in (here / "printers.toml", here.parent / "printers.toml"):
        if cand.exists():
            return cand
    return here / "printers.toml"


def load_config() -> dict[str, dict[str, str]]:
    path = config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"File di configurazione non trovato: {path}. "
            "Copia printers.example.toml in printers.toml e compilalo."
        )
    with path.open("rb") as f:
        data = tomllib.load(f)

    printers: dict[str, dict[str, str]] = {}
    for name, cfg in (data.get("printers") or {}).items():
        missing = [k for k in ("ip", "serial", "access_code") if not cfg.get(k)]
        if missing:
            raise ValueError(f"Stampante '{name}': mancano i campi {', '.join(missing)}")
        printers[name] = {
            "ip": str(cfg["ip"]).strip(),
            "serial": str(cfg["serial"]).strip(),
            "access_code": str(cfg["access_code"]).strip(),
            "model": str(cfg.get("model", "")).strip(),
        }
    if not printers:
        raise ValueError(f"Nessuna stampante definita in {path} (sezioni [printers.<nome>]).")
    return printers


# ---------------------------------------------------------------------------
# Connessione MQTT a una singola stampante
# ---------------------------------------------------------------------------

def deep_merge(dst: dict, src: dict) -> None:
    """I modelli P1/A1 inviano report incrementali: si fondono nello stato in cache."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v


class BambuPrinter:
    def __init__(self, name: str, ip: str, serial: str, access_code: str, model: str = ""):
        self.name = name
        self.ip = ip
        self.serial = serial
        self.access_code = access_code
        self.model = model

        self.report_topic = f"device/{serial}/report"
        self.request_topic = f"device/{serial}/request"

        self._lock = threading.RLock()
        self._state: dict[str, Any] = {}
        self._last_update = 0.0
        self._last_pushall = 0.0
        self._connected = threading.Event()
        self._first_report = threading.Event()
        self._connect_error: str | None = None
        self._acks: dict[str, dict] = {}
        self._ack_cond = threading.Condition(self._lock)
        self._seq = int(time.time()) % 100000
        self._client: mqtt.Client | None = None

    # -- ciclo di vita ------------------------------------------------------

    def _build_client(self) -> mqtt.Client:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"claude-mcp-{self.serial}-{os.getpid()}",
            protocol=mqtt.MQTTv311,
        )
        client.username_pw_set(MQTT_USER, self.access_code)
        # La stampante usa un certificato autofirmato: niente verifica.
        client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        client.tls_insecure_set(True)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def ensure_connected(self) -> None:
        with self._lock:
            if self._client is None:
                self._connect_error = None
                self._client = self._build_client()
                try:
                    self._client.connect_async(self.ip, MQTT_PORT, keepalive=60)
                    self._client.loop_start()
                except Exception as e:  # noqa: BLE001
                    self._client = None
                    raise ConnectionError(f"[{self.name}] connessione a {self.ip} fallita: {e}") from e

        deadline = time.time() + CONNECT_TIMEOUT
        while not self._connected.wait(0.2):
            if self._connect_error or time.time() > deadline:
                break
        if not self._connected.is_set():
            err = self._connect_error or "timeout"
            self.close()
            raise ConnectionError(
                f"[{self.name}] impossibile connettersi via MQTT a {self.ip}:{MQTT_PORT} ({err}). "
                "Controlla IP, codice di accesso e che sulla stampante siano attive "
                "la modalità LAN e la modalità sviluppatore."
            )

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            # Prima disconnect, poi loop_stop: nell'ordine inverso loop_stop può
            # restare bloccato. Il tutto in un thread con timeout, per sicurezza.
            def _shutdown():
                try:
                    client.disconnect()
                    client.loop_stop()
                except Exception:  # noqa: BLE001
                    pass
            t = threading.Thread(target=_shutdown, daemon=True)
            t.start()
            if threading.current_thread() is not getattr(client, "_thread", None):
                t.join(5.0)
        self._connected.clear()

    # -- callback MQTT ------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            self._connect_error = str(reason_code)
            log(f"{self.name}: connessione rifiutata ({reason_code})")
            # Credenziali sbagliate: inutile riprovare all'infinito
            threading.Thread(target=self.close, daemon=True).start()
            return
        log(f"{self.name}: connesso a {self.ip}")
        client.subscribe(self.report_topic, qos=0)
        self._connected.set()
        self._send({"pushing": {"command": "pushall"}}, track=False)
        self._last_pushall = time.time()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._connected.clear()
        log(f"{self.name}: disconnesso ({reason_code})")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return
        with self._lock:
            pr = payload.get("print")
            if isinstance(pr, dict):
                cmd = pr.get("command")
                if cmd in (None, "push_status"):
                    deep_merge(self._state, payload)
                    self._last_update = time.time()
                    # Il primo report può essere parziale: si considera pronto solo
                    # quando è arrivato il report completo (risposta a pushall).
                    if "gcode_state" in (self._state.get("print") or {}):
                        self._first_report.set()
                else:
                    seq = str(pr.get("sequence_id", ""))
                    if seq:
                        self._acks[seq] = pr
                        self._ack_cond.notify_all()
            elif isinstance(payload.get("system"), dict) and payload["system"].get("sequence_id"):
                # risposta a un comando "system" (es. luce)
                self._acks[str(payload["system"]["sequence_id"])] = payload["system"]
                self._ack_cond.notify_all()
            else:
                deep_merge(self._state, payload)
                self._last_update = time.time()

    # -- invio comandi ------------------------------------------------------

    def _next_seq(self) -> str:
        with self._lock:
            self._seq += 1
            return str(self._seq)

    def _send(self, message: dict, track: bool = True) -> str:
        seq = self._next_seq()
        for section in message.values():
            if isinstance(section, dict):
                section["sequence_id"] = seq
        client = self._client
        if client is None:
            raise ConnectionError(f"[{self.name}] non connesso")
        info = client.publish(self.request_topic, json.dumps(message), qos=1)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError(f"[{self.name}] invio fallito (rc={info.rc})")
        return seq

    def get_state(self, refresh: bool = True) -> tuple[dict, float]:
        self.ensure_connected()
        now = time.time()
        if refresh and now - self._last_pushall > PUSHALL_MIN_INTERVAL:
            self._send({"pushing": {"command": "pushall"}}, track=False)
            self._last_pushall = now
            time.sleep(1.0)
        if not self._first_report.wait(FIRST_REPORT_TIMEOUT):
            raise TimeoutError(f"[{self.name}] connesso, ma nessun report di stato ricevuto.")
        with self._lock:
            return json.loads(json.dumps(self._state)), self._last_update  # copia profonda

    def print_command(self, command: str) -> dict:
        """Invia pause / resume / stop e attende la conferma della stampante."""
        return self.send_and_wait({"print": {"command": command, "param": ""}})

    def send_and_wait(self, message: dict) -> dict:
        """Invia un messaggio MQTT e attende la risposta con lo stesso sequence_id."""
        self.ensure_connected()
        seq = self._send(message)
        deadline = time.time() + COMMAND_ACK_TIMEOUT
        with self._ack_cond:
            while seq not in self._acks:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return {"sent": True, "acknowledged": False}
                self._ack_cond.wait(remaining)
            ack = self._acks.pop(seq)
        return {
            "sent": True,
            "acknowledged": True,
            "result": ack.get("result"),
            "reason": ack.get("reason") or None,
        }

    def current_gcode_state(self) -> str:
        with self._lock:
            return (self._state.get("print") or {}).get("gcode_state", "UNKNOWN")


# ---------------------------------------------------------------------------
# FTPS (TLS implicito, porta 990) per i file sulla scheda SD
# ---------------------------------------------------------------------------

class ImplicitFTPS(ftplib.FTP_TLS):
    """FTP_TLS con TLS implicito e riuso della sessione TLS sul canale dati.

    Le stampanti Bambu richiedono che la connessione dati riusi la sessione TLS
    del canale di controllo, altrimenti i trasferimenti falliscono.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        # Come ftplib, ma senza conn.unwrap(): la stampante non risponde al
        # close_notify TLS e unwrap() resterebbe in attesa fino al timeout.
        self.voidcmd("TYPE I")
        conn = self.transfercmd(cmd, rest)
        try:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback:
                    callback(buf)
        finally:
            conn.close()
        return self.voidresp()


def ftps_connect(p: "BambuPrinter") -> ImplicitFTPS:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # certificato autofirmato della stampante
    ftp = ImplicitFTPS(context=ctx, timeout=FTPS_TIMEOUT)
    try:
        ftp.connect(p.ip, FTPS_PORT)
        ftp.login(MQTT_USER, p.access_code)
        ftp.prot_p()
    except (OSError, ftplib.Error) as e:
        try:
            ftp.close()
        except Exception:  # noqa: BLE001
            pass
        raise ConnectionError(
            f"[{p.name}] connessione FTPS a {p.ip}:{FTPS_PORT} fallita: {e}. "
            "Controlla codice di accesso, modalità LAN/sviluppatore e che la scheda SD sia inserita."
        ) from e
    return ftp


def ftps_list(p: "BambuPrinter", path: str) -> list[dict]:
    ftp = ftps_connect(p)
    try:
        entries = []
        try:
            for name, facts in ftp.mlsd(path, facts=["type", "size", "modify"]):
                if name in (".", ".."):
                    continue
                entries.append({"name": name, "type": facts.get("type"),
                                "size": int(facts["size"]) if facts.get("size") else None,
                                "modified": facts.get("modify")})
        except ftplib.error_perm:
            # MLSD non supportato: si ripiega su NLST
            entries = [{"name": posixpath.basename(n.rstrip("/"))} for n in ftp.nlst(path)]
        return sorted(entries, key=lambda e: e["name"].lower())
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            ftp.close()


def ftps_size(p: "BambuPrinter", remote_path: str) -> int | None:
    """Dimensione di un file sulla SD, None se non esiste."""
    ftp = ftps_connect(p)
    try:
        try:
            return ftp.size(remote_path)
        except ftplib.error_perm:
            return None
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            ftp.close()


def ftps_upload(p: "BambuPrinter", local: Path, remote_path: str) -> int:
    ftp = ftps_connect(p)
    try:
        with local.open("rb") as f:
            ftp.storbinary(f"STOR {remote_path}", f, blocksize=64 * 1024)
        try:
            return ftp.size(remote_path) or -1
        except ftplib.Error:
            return -1
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            ftp.close()


# ---------------------------------------------------------------------------
# Interpretazione dei report
# ---------------------------------------------------------------------------

def _unpack_temp(value: Any) -> tuple[float | None, float | None]:
    """Su H2D/X2D le temperature sono impacchettate: bit 0-15 attuale, 16-31 obiettivo."""
    if not isinstance(value, int):
        return None, None
    return float(value & 0xFFFF), float((value >> 16) & 0xFFFF)


def _num(v: Any) -> float | None:
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def parse_temperatures(state: dict, model: str = "") -> dict:
    pr = state.get("print", {}) or {}
    device = pr.get("device") or state.get("device") or {}

    nozzles: list[dict] = []
    ext_info = ((device.get("extruder") or {}).get("info")) or []
    for ext in ext_info:
        cur, tgt = _unpack_temp(ext.get("temp"))
        if cur is None:
            continue
        idx = ext.get("id", len(nozzles))
        nozzles.append({
            "nozzle": {0: "destro", 1: "sinistro"}.get(idx, str(idx)) if len(ext_info) > 1 else "principale",
            "id": idx,
            "current_c": cur,
            "target_c": tgt,
        })
    if not nozzles and "nozzle_temper" in pr:
        nozzles.append({
            "nozzle": "principale",
            "id": 0,
            "current_c": _num(pr.get("nozzle_temper")),
            "target_c": _num(pr.get("nozzle_target_temper")),
        })

    bed_cur, bed_tgt = _unpack_temp(((device.get("bed") or {}).get("info") or {}).get("temp"))
    if bed_cur is None:
        bed_cur, bed_tgt = _num(pr.get("bed_temper")), _num(pr.get("bed_target_temper"))

    ch_cur, ch_tgt = _unpack_temp(((device.get("ctc") or {}).get("info") or {}).get("temp"))
    # La serie A1 non ha sensore di camera ma invia comunque chamber_temper (valore fittizio)
    no_chamber = model.upper().replace(" ", "").startswith("A1")
    if ch_cur is None and pr.get("chamber_temper") is not None and not no_chamber:
        ch_cur, ch_tgt = _num(pr.get("chamber_temper")), None

    out = {"nozzles": nozzles, "bed": {"current_c": bed_cur, "target_c": bed_tgt}}
    if ch_cur is not None:
        out["chamber"] = {"current_c": ch_cur, "target_c": ch_tgt}
    return out


def parse_progress(state: dict) -> dict:
    pr = state.get("print", {}) or {}
    remaining = pr.get("mc_remaining_time")
    out = {
        "job_name": pr.get("subtask_name") or pr.get("gcode_file") or None,
        "percent": pr.get("mc_percent"),
        "layer": pr.get("layer_num"),
        "total_layers": pr.get("total_layer_num"),
        "remaining_minutes": remaining,
    }
    if isinstance(remaining, (int, float)) and remaining > 0:
        out["estimated_finish"] = time.strftime("%H:%M", time.localtime(time.time() + remaining * 60))
    return out


def parse_status(name: str, printer: BambuPrinter, state: dict, updated: float) -> dict:
    pr = state.get("print", {}) or {}
    gstate = pr.get("gcode_state") or "UNKNOWN"
    out = {
        "printer": name,
        "model": printer.model or None,
        "ip": printer.ip,
        "state": gstate,
        "state_description": GCODE_STATES.get(gstate, gstate.lower()),
        "progress": parse_progress(state),
        "temperatures": parse_temperatures(state, printer.model),
        "speed_level": {1: "silenziosa", 2: "standard", 3: "sport", 4: "ludicrous"}.get(pr.get("spd_lvl")),
        "wifi_signal": pr.get("wifi_signal"),
        "last_update_seconds_ago": round(time.time() - updated, 1) if updated else None,
    }
    err = pr.get("print_error")
    if err:
        out["print_error_code"] = f"{int(err):08X}" if isinstance(err, int) else err
    hms = pr.get("hms") or []
    if hms:
        out["hms_alerts"] = [
            f"HMS_{(h.get('attr', 0) >> 16):04X}_{(h.get('attr', 0) & 0xFFFF):04X}_"
            f"{(h.get('code', 0) >> 16):04X}_{(h.get('code', 0) & 0xFFFF):04X}"
            for h in hms if isinstance(h, dict)
        ]
    return out


# ---------------------------------------------------------------------------
# Registro stampanti
# ---------------------------------------------------------------------------

_printers: dict[str, BambuPrinter] = {}
_registry_lock = threading.Lock()


def get_printer(name: str | None) -> tuple[str, BambuPrinter]:
    cfg = load_config()  # riletto ogni volta: le modifiche al file valgono subito
    if not name:
        if len(cfg) > 1:
            raise ValueError(
                f"Sono configurate più stampanti ({', '.join(cfg)}): specifica il parametro 'printer'."
            )
        name = next(iter(cfg))
    if name not in cfg:
        lowered = {k.lower(): k for k in cfg}
        if name.lower() in lowered:
            name = lowered[name.lower()]
        else:
            raise ValueError(f"Stampante '{name}' sconosciuta. Disponibili: {', '.join(cfg)}")

    c = cfg[name]
    with _registry_lock:
        p = _printers.get(name)
        if p is None or (p.ip, p.serial, p.access_code) != (c["ip"], c["serial"], c["access_code"]):
            if p is not None:
                p.close()
            p = BambuPrinter(name, c["ip"], c["serial"], c["access_code"], c["model"])
            _printers[name] = p
        p.model = c["model"]
    return name, p


# ---------------------------------------------------------------------------
# Tool MCP
# ---------------------------------------------------------------------------

mcp = FastMCP("bambu-lab")


@mcp.tool()
def list_printers() -> list[dict]:
    """Elenca le stampanti Bambu Lab configurate (nome, modello, IP)."""
    return [
        {"name": n, "model": c["model"] or None, "ip": c["ip"],
         "serial": c["serial"][:4] + "…" + c["serial"][-3:]}
        for n, c in load_config().items()
    ]


@mcp.tool()
def get_status(printer: str | None = None) -> dict:
    """Stato completo di una stampante: stato, avanzamento, temperature, errori.

    printer: nome della stampante come in printers.toml (facoltativo se ce n'è una sola).
    """
    name, p = get_printer(printer)
    state, updated = p.get_state()
    return parse_status(name, p, state, updated)


@mcp.tool()
def get_temperatures(printer: str | None = None) -> dict:
    """Temperature attuali e obiettivo di ugello/i, piatto ed eventualmente camera."""
    name, p = get_printer(printer)
    state, _ = p.get_state()
    return {"printer": name, **parse_temperatures(state, p.model)}


@mcp.tool()
def get_print_progress(printer: str | None = None) -> dict:
    """Avanzamento della stampa: percentuale, layer, tempo rimanente e ora di fine stimata."""
    name, p = get_printer(printer)
    state, _ = p.get_state()
    gstate = (state.get("print") or {}).get("gcode_state", "UNKNOWN")
    return {"printer": name, "state": gstate,
            "state_description": GCODE_STATES.get(gstate, gstate.lower()),
            **parse_progress(state)}


def _control(printer: str | None, command: str, allowed: set[str], verb: str) -> dict:
    name, p = get_printer(printer)
    state, _ = p.get_state()
    gstate = (state.get("print") or {}).get("gcode_state", "UNKNOWN")
    if gstate not in allowed:
        return {
            "printer": name,
            "ok": False,
            "message": f"Impossibile {verb}: la stampante è '{GCODE_STATES.get(gstate, gstate)}' ({gstate}).",
        }
    res = p.print_command(command)
    ok = res.get("result", "success") in ("success", None) if res["acknowledged"] else True
    msg = f"Comando '{command}' inviato a {name}"
    if not res["acknowledged"]:
        msg += " (nessuna conferma ricevuta: verifica lo stato con get_status)"
    elif not ok:
        msg += f" ma rifiutato: {res.get('reason') or res.get('result')}"
    return {"printer": name, "ok": ok, "previous_state": gstate, "message": msg}


@mcp.tool()
def pause_print(printer: str | None = None) -> dict:
    """Mette in pausa la stampa in corso."""
    return _control(printer, "pause", {"RUNNING", "PREPARE"}, "mettere in pausa")


@mcp.tool()
def resume_print(printer: str | None = None) -> dict:
    """Riprende una stampa in pausa."""
    return _control(printer, "resume", {"PAUSE"}, "riprendere")


@mcp.tool()
def stop_print(printer: str | None = None) -> dict:
    """Ferma DEFINITIVAMENTE la stampa in corso (non si può riprendere).

    Chiedi sempre conferma all'utente prima di usarlo.
    """
    return _control(printer, "stop", {"RUNNING", "PAUSE", "PREPARE"}, "fermare")


@mcp.tool()
def list_files(printer: str | None = None, path: str = "/") -> dict:
    """Elenca i file sulla scheda SD della stampante (via FTPS).

    path: cartella da elencare, ad es. "/" o "/cache".
    """
    name, p = get_printer(printer)
    return {"printer": name, "path": path, "files": ftps_list(p, path)}


@mcp.tool()
def upload_file(local_path: str, printer: str | None = None, remote_name: str | None = None,
                remote_dir: str = "/") -> dict:
    """Carica un file .3mf o .gcode sulla scheda SD della stampante (via FTPS).

    Non avvia la stampa. Se esiste già un file con lo stesso nome viene sovrascritto.
    local_path: percorso del file sul PC.
    remote_name: nome sulla stampante (predefinito: il nome del file locale).
    remote_dir: cartella di destinazione sulla SD (predefinita: radice "/").
    """
    local = Path(local_path).expanduser()
    if not local.is_file():
        raise FileNotFoundError(f"File locale non trovato: {local}")
    rname = remote_name or local.name
    if Path(rname).suffix.lower() not in UPLOAD_EXTENSIONS:
        raise ValueError(f"Estensione non ammessa per '{rname}': usa {', '.join(sorted(UPLOAD_EXTENSIONS))}.")
    if "/" in rname or "\\" in rname:
        raise ValueError("remote_name deve essere un nome di file, non un percorso: usa remote_dir.")
    name, p = get_printer(printer)
    remote_path = posixpath.join(remote_dir or "/", rname)
    t0 = time.time()
    remote_size = ftps_upload(p, local, remote_path)
    local_size = local.stat().st_size
    return {
        "printer": name,
        "remote_path": remote_path,
        "bytes": local_size,
        "verified": remote_size == local_size if remote_size >= 0 else None,
        "seconds": round(time.time() - t0, 1),
    }


START_OK_STATES = {"IDLE", "FINISH", "FAILED"}
START_WAIT = 25.0


@mcp.tool()
def start_print(file_name: str, confirmed: bool = False, printer: str | None = None,
                remote_dir: str = "/", plate: int = 1, bed_leveling: bool = True,
                timelapse: bool = False, use_ams: bool = False) -> dict:
    """Avvia la stampa di un file GIÀ CARICATO sulla scheda SD (.gcode.3mf / .3mf con G-code, o .gcode).

    OBBLIGATORIO: prima di chiamarlo chiedi SEMPRE conferma esplicita all'utente in chat
    (file, stampante, filamento, piatto) e passa confirmed=True solo dopo il suo "sì".
    Senza confirmed=True il comando non viene inviato.

    file_name: nome del file sulla SD (come restituito da upload_file / list_files).
    plate: numero del piatto nel 3mf (di solito 1).
    bed_leveling: livellamento automatico prima della stampa.
    use_ams: False = bobina esterna (A1 mini senza AMS lite).
    """
    if not confirmed:
        return {"ok": False, "message": "Avvio NON inviato: serve la conferma esplicita dell'utente "
                                        "(chiedila in chat, poi richiama con confirmed=True)."}
    name, p = get_printer(printer)
    state, _ = p.get_state()
    gstate = (state.get("print") or {}).get("gcode_state", "UNKNOWN")
    if gstate not in START_OK_STATES:
        return {"printer": name, "ok": False,
                "message": f"La stampante è '{GCODE_STATES.get(gstate, gstate)}' ({gstate}): avvio annullato."}

    remote_path = posixpath.join(remote_dir or "/", file_name)
    size = ftps_size(p, remote_path)
    if size is None:
        return {"printer": name, "ok": False,
                "message": f"File '{remote_path}' non trovato sulla SD: caricalo prima con upload_file."}

    lower = file_name.lower()
    task = posixpath.splitext(file_name[:-len(".gcode.3mf")] if lower.endswith(".gcode.3mf") else file_name)[0]
    sd_path = remote_path.lstrip("/")
    if lower.endswith(".3mf"):
        msg = {"print": {
            "command": "project_file",
            "param": f"Metadata/plate_{plate}.gcode",
            "url": f"file:///sdcard/{sd_path}",
            "subtask_name": task,
            "project_id": "0", "profile_id": "0", "task_id": "0", "subtask_id": "0",
            "md5": "",
            "timelapse": timelapse,
            "bed_type": "auto",
            "bed_leveling": bed_leveling,
            "bed_levelling": bed_leveling,
            "flow_cali": False,
            "vibration_cali": False,
            "layer_inspect": False,
            "use_ams": use_ams,
            "ams_mapping": "",
        }}
    elif lower.endswith(".gcode"):
        msg = {"print": {"command": "gcode_file", "param": f"/sdcard/{sd_path}"}}
    else:
        return {"printer": name, "ok": False, "message": "Formato non supportato: usa .gcode.3mf/.3mf o .gcode."}

    ack = p.send_and_wait(msg)
    if ack.get("acknowledged") and ack.get("result") not in (None, "success", "SUCCESS"):
        return {"printer": name, "ok": False, "file": remote_path,
                "message": f"La stampante ha rifiutato l'avvio: {ack.get('reason') or ack.get('result')}"}

    # Verifica che la stampa parta davvero
    deadline = time.time() + START_WAIT
    new_state = gstate
    while time.time() < deadline:
        time.sleep(2)
        new_state = p.current_gcode_state()
        if new_state in ("PREPARE", "RUNNING", "SLICING"):
            break
    started = new_state in ("PREPARE", "RUNNING", "SLICING")
    return {
        "printer": name,
        "ok": started,
        "file": remote_path,
        "state": new_state,
        "state_description": GCODE_STATES.get(new_state, new_state.lower()),
        "acknowledged": ack.get("acknowledged"),
        "message": "Stampa avviata." if started else
                   "Comando inviato ma la stampante non risulta partita: controlla display e get_status.",
    }


@mcp.tool()
def set_light(on: bool, printer: str | None = None) -> dict:
    """Accende (on=True) o spegne la luce della stampante (chamber_light)."""
    name, p = get_printer(printer)
    res = p.send_and_wait({"system": {"command": "ledctrl", "led_node": "chamber_light",
                                      "led_mode": "on" if on else "off", "led_on_time": 500,
                                      "led_off_time": 500, "loop_times": 0, "interval_time": 0}})
    return {"printer": name, "light": "on" if on else "off", "acknowledged": res.get("acknowledged"),
            "result": res.get("result")}


if __name__ == "__main__":
    mcp.run()
