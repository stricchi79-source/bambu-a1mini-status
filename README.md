# Bambu A1 mini – schermino sul telefono e server MCP

Una pagina da aprire sul telefono per vedere e controllare una **Bambu Lab A1 mini** in rete locale (modalità solo LAN), più un server [MCP](https://modelcontextprotocol.io) per farla usare a un assistente AI (Claude & co.).

- **Stato** in tempo reale: avanzamento, layer, tempo che manca e ora di fine, fase (livellamento, riscaldamento…), temperature, ventola, Wi‑Fi, filamento caricato.
- **Telecamera** della stampante, direttamente nella pagina.
- **Comandi**: pausa / riprendi, stop (con conferma), luce, velocità (silenziosa, standard, sport, ludicrous).
- **Vista "entrambe"** (`/entrambe`): A1 mini ed [Ender-3 V3 KE](https://github.com/stricchi79-source/ender-ke-status) nella stessa pagina, con il riepilogo di quale stampa finisce prima.
- Sette temi rétro (pompa di benzina, sala giochi, terminale, radio, juke-box, missione Luna, ugello rovente) con il pulsante 🎨.
- Dal telefono **non** si avviano stampe: l'avvio resta una scelta fatta al PC.

## Requisiti

- Bambu Lab A1 mini (o un altro modello A1/P1, non provato) con **Modalità solo LAN** e **Modalità sviluppatore** attive: con i firmware recenti, senza modalità sviluppatore la stampante non accetta comandi da programmi di terze parti.
- Python 3.11 o più recente sul PC: `pip install -r requirements.txt`
- PC e telefono sulla stessa rete della stampante.

## Configurazione

Copia `printers.example.toml` in `printers.toml` e inserisci i dati della stampante (sul display: Impostazioni → Rete per IP e codice di accesso, Impostazioni → Dispositivo per il numero di serie).

`printers.toml` contiene il codice di accesso della stampante: è escluso da git, non pubblicarlo. Viene cercato in quest'ordine: variabile `BAMBU_CONFIG`, questa cartella, la cartella sopra.

## Uso

```bash
python server.py
```

Poi dal telefono apri `http://IP-DEL-PC:8090` (per esempio `http://192.168.1.14:8090`) e, dal menu del browser, **Aggiungi a schermata Home**: si apre come un'app. La vista con entrambe le stampanti è `http://IP-DEL-PC:8090/entrambe`; legge lo stato della Ender dal suo server sullo stesso PC (`ENDER_STATUS_URL`, predefinito `http://127.0.0.1:80/status`).

Argomenti facoltativi: nome della stampante in `printers.toml` (se ce n'è più d'una) e porta (predefinita `8090`).

### Avvio automatico (Windows)

Crea un collegamento nella cartella Esecuzione automatica (`Win + R` → `shell:startup`) con destinazione:

```
pythonw.exe -u "C:\percorso\server.py"
```

Con `pythonw` gira senza finestra e scrive i messaggi in `server.log`.

## Server MCP

`bambu_server.py` è un server MCP (stdio) che parla con la stampante via **MQTT** (porta 8883) e **FTPS** (porta 990):

| Tool | Cosa fa |
|---|---|
| `list_printers`, `get_status`, `get_temperatures`, `get_print_progress` | lettura dello stato |
| `pause_print`, `resume_print`, `stop_print` | controllo della stampa in corso |
| `set_light` | luce on/off |
| `list_files`, `upload_file` | file sulla scheda SD (`.gcode.3mf`, `.gcode`) |
| `start_print` | avvia un file già caricato; **non parte senza `confirmed=True`**, da passare solo dopo il "sì" dell'utente |

Con Claude Code:

```bash
claude mcp add bambu-lab -- python /percorso/bambu_server.py
```

I file `.gcode.3mf` per la stampante si ottengono per esempio con OrcaSlicer: `orca-slicer --slice 0 --export-3mf pezzo.gcode.3mf …`.

## Note tecniche

- Il primo report MQTT può essere parziale: il server aspetta quello completo (con `gcode_state`) prima di rispondere.
- Sul canale dati FTPS la stampante non risponde alla chiusura TLS: il caricamento chiude la connessione senza aspettarla, altrimenti resterebbe appeso fino al timeout.
- La A1 invia una temperatura della camera fittizia (5 °C): per i modelli A1 non viene mostrata.
- La telecamera (porta 6000) viene letta solo mentre qualcuno la sta guardando.

## Licenza

MIT. Non affiliato a Bambu Lab.
