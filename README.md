# Bambu A1 mini – schermino sul telefono e server MCP

Una pagina da aprire sul telefono per vedere e controllare una **Bambu Lab A1 mini** in rete locale (modalità solo LAN), più un server [MCP](https://modelcontextprotocol.io) per farla usare a un assistente AI (Claude & co.).

- **Stato** in tempo reale: avanzamento, layer, tempo che manca e ora di fine, fase (livellamento, riscaldamento…), temperature, ventola, Wi‑Fi, filamento caricato.
- **Telecamera** della stampante, direttamente nella pagina.
- **Comandi**: pausa / riprendi, stop (con conferma), luce, velocità (silenziosa, standard, sport, ludicrous).
- **Notifiche** su telefono e smartwatch con [ntfy](https://ntfy.sh): inizio, pausa, fine, interruzione, errori.
- **Vista "entrambe"** (`/entrambe`): A1 mini ed [Ender-3 V3 KE](https://github.com/stricchi79-source/ender-ke-status) nella stessa pagina, con il riepilogo di quale stampa finisce prima.
- Sette temi rétro (pompa di benzina, sala giochi, terminale, radio, juke-box, missione Luna, ugello rovente) con il pulsante 🎨.
- Dal telefono **non** si avviano stampe: l'avvio resta una scelta fatta al PC.

## Requisiti

- Bambu Lab A1 mini (o un altro modello A1/P1, non provato; X2D: vedi sotto) con **Modalità solo LAN** e **Modalità sviluppatore** attive: con i firmware recenti, senza modalità sviluppatore la stampante non accetta comandi da programmi di terze parti.
- Python 3.11 o più recente sul PC: `pip install -r requirements.txt`
- PC e telefono sulla stessa rete della stampante.

## Bambu Lab X2D (e X1 / H2D)

Con `model = "X2D"` in `printers.toml` la pagina si adatta:

- titolo e notifiche con il nome del modello;
- **due ugelli** (il destro in grande, il sinistro sotto), **temperatura della camera** con la sua meta e la ventola della camera;
- riquadro **AMS** con i quattro slot: tipo, colore, percentuale rimasta e slot in uso evidenziato;
- **telecamera**: questi modelli la trasmettono in RTSP (porta 322), non sulla porta 6000 come A1/P1. Serve **ffmpeg** sul PC (Windows: `winget install ffmpeg`, poi riapri il terminale; in alternativa la variabile `FFMPEG` con il percorso di `ffmpeg.exe`). In "Impostazioni → Rete" della stampante va attivata anche la visione in diretta via LAN, se presente.

Il riconoscimento è dal campo `model`: valori che iniziano con `X1`, `X2` o `H2` usano la telecamera RTSP. Non ancora provato su una X2D vera: stato e comandi usano lo stesso MQTT degli altri modelli; lo slot AMS attivo su doppio estrusore è letto da un campo (`snow`) dedotto dalla H2D.

## Configurazione

Copia `printers.example.toml` in `printers.toml` e inserisci i dati della stampante (sul display: Impostazioni → Rete per IP e codice di accesso, Impostazioni → Dispositivo per il numero di serie).

`printers.toml` contiene il codice di accesso della stampante: è escluso da git, non pubblicarlo. Viene cercato in quest'ordine: variabile `BAMBU_CONFIG`, questa cartella, la cartella sopra.

## Uso

```bash
python server.py
```

Poi dal telefono apri `http://IP-DEL-PC:8090` (per esempio `http://192.168.1.14:8090`) e, dal menu del browser, **Aggiungi a schermata Home**: si apre come un'app. La vista con entrambe le stampanti è `http://IP-DEL-PC:8090/entrambe`; legge lo stato della Ender dal suo server sullo stesso PC (`ENDER_STATUS_URL`, predefinito `http://127.0.0.1:80/status`).

Argomenti facoltativi: nome della stampante in `printers.toml` (se ce n'è più d'una) e porta (predefinita `8090`).

### Notifiche sul telefono (e sullo smartwatch)

Installa l'app gratuita [ntfy](https://ntfy.sh), iscriviti a un argomento con un nome lungo e casuale (es. `stampanti-7f3k9q2m`) e avvia il server con:

```bash
python server.py --ntfy stampanti-7f3k9q2m
```

Arriva una notifica quando la stampa inizia, va in pausa (con il motivo, es. filamento finito), riprende, finisce (con la durata), si interrompe o va in errore. Il titolo comincia con il nome della stampante (`--nome`, predefinito "A1 mini"): si può usare lo stesso argomento della [Ender](https://github.com/stricchi79-source/ender-ke-status) (`--ntfy … --nome Ender`) per avere tutto in un posto solo. Se lo smartwatch ripete le notifiche del telefono, le vedi anche al polso: basta abilitare ntfy nell'app dell'orologio.

Chi conosce il nome dell'argomento può leggere le notifiche: sceglilo lungo e casuale, e non pubblicarlo.

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
- La telecamera (porta 6000, o RTSP 322 sui modelli X/H) viene letta solo mentre qualcuno la sta guardando.

## Licenza

MIT. Non affiliato a Bambu Lab.
