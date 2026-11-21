# ai-llm-api-proxy

Proxy OpenAI-compatibile da mettere davanti a **llama.cpp** (`llama-server`). Qualunque client
lo chiami (Open WebUI, app, script), il proxy:

- usa **solo il proprio system prompt**: quelli inviati dal client vengono rimossi;
- **espone i tool di un server MCP** e li **esegue lato proxy**, ripetendo i giri di tool finché
  il modello dà la risposta finale;
- fa **streaming reale** token per token, anche tra un giro di tool e l'altro;
- inoltra le **statistiche di llama.cpp** (`usage`, `timings`: token, tempi, velocità,
  `prompt_progress`), sommate su tutti i giri di tool della risposta;
- annuncia in chat l'uso dei tool con **frasi naturali** («Cerco tra i contatti...»), generate dal
  modello una volta per tool e pensate per le conversazioni vocali;
  con `SEND_CHAT_MARKDOWN=true` aggiunge il **box nativo dei tool** di Open WebUI (argomenti e
  risultato) e mostra ogni ragionamento nel punto in cui avviene;
- **compatta la memoria** quando il prompt si avvicina al contesto massimo, facendo riassumere
  i messaggi più vecchi allo stesso modello (`compact_tool.py`). Espone anche il tool
  `compact_memory`, che il modello può chiamare di sua iniziativa;
- salva ogni conversazione nel **database history** (messaggi, ragionamenti, tool call e risultati):
  quando il client la riprende, il modello ritrova l'intero scambio, come senza proxy;
- con `PROXY_API_KEYS` accetta solo client con una **API key** valida.

```
Client  →  proxy :8081  →  llama.cpp :8080
              │
              └──→  server MCP (streamable HTTP o SSE)
```

## Requisiti

- `llama-server` avviato con `--jinja` e un modello che supporti il tool calling
  (es. Qwen 2.5/3, Llama 3.1+, Mistral).
- Python 3.10+ (Debian 12/13 vanno bene).
- Server MCP raggiungibile via HTTP (streamable HTTP o SSE). Per un server solo stdio,
  mettici davanti un bridge come `mcp-proxy`.

## Documentazione

| Documento | Contenuto |
|---|---|
| [docs/PROXMOX.md](docs/PROXMOX.md) | creazione del container LXC (community script), risorse, installazione, primo avvio, aggiornamenti |
| [docs/NGINX.md](docs/NGINX.md) | esposizione su internet: nginx + Let's Encrypt, Nginx Proxy Manager, API key, firewall, alternative VPN |
| [docs/MILESTONES.md](docs/MILESTONES.md) | attività svolte e prossimi passi |

## Avvio rapido

```bash
git clone https://github.com/andreadegiovine/ai-llm-api-proxy /opt/llm-proxy && cd /opt/llm-proxy
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env && cp system_prompt.example.txt system_prompt.txt
nano .env                 # almeno LLAMA_URL, MCP_URL, MCP_TRANSPORT
venv/bin/python proxy.py  # ascolta su PROXY_HOST:PROXY_PORT (default 0.0.0.0:8081)
```

Per l'installazione come servizio systemd segui [docs/PROXMOX.md](docs/PROXMOX.md).
Nei client usa `http://IP_PROXY:8081/v1` come endpoint OpenAI.

## Aggiornamento

Da root, nel container del proxy. `.env` e `system_prompt.txt` sono esclusi da git, quindi
l'aggiornamento non li tocca.

1. Scarica il codice nuovo:

   ```bash
   cd /opt/llm-proxy && git pull
   ```

2. Aggiorna le dipendenze Python (se `requirements.txt` non è cambiato non fa nulla):

   ```bash
   /opt/llm-proxy/venv/bin/pip install -r /opt/llm-proxy/requirements.txt
   ```

3. Controlla se sono state aggiunte variabili a `.env.example`. Le righe con `<` sono variabili
   nuove: copiale in `.env` se vuoi cambiarne il valore di default, altrimenti puoi ignorarle.

   ```bash
   diff <(grep -o '^[A-Z_]*=' /opt/llm-proxy/.env.example) <(grep -o '^[A-Z_]*=' /opt/llm-proxy/.env)
   ```

4. Solo se il `git pull` ha modificato `deploy/llm-proxy.service`, reinstalla il servizio:

   ```bash
   cp /opt/llm-proxy/deploy/llm-proxy.service /etc/systemd/system/ && systemctl daemon-reload
   ```

5. Riavvia il servizio e controlla il log (devono comparire la connessione al server MCP e l'elenco dei tool):

   ```bash
   systemctl restart llm-proxy && journalctl -u llm-proxy -n 30 --no-pager
   ```

Per gli aggiornamenti di routine, i passi 1, 2 e 5 in un solo comando:

```bash
cd /opt/llm-proxy && git pull && venv/bin/pip install -r requirements.txt && systemctl restart llm-proxy
```

Se le tue variabili sono nel servizio systemd (vedi
[Variabili fisse nel servizio systemd](#variabili-fisse-nel-servizio-systemd)), puoi copiare a ogni
aggiornamento il nuovo `.env.example` in `.env`, così hai sempre tutte le variabili nuove con il loro
default, e reinstallare il servizio senza perdere la configurazione:

```bash
cd /opt/llm-proxy && git pull && venv/bin/pip install -r requirements.txt && cp .env.example .env
```

```bash
cp /opt/llm-proxy/deploy/llm-proxy.service /etc/systemd/system/ && systemctl daemon-reload && systemctl restart llm-proxy
```

Non copiare `.env.example` in `.env` se la tua configurazione è ancora in `.env`: la perderesti.

Se `git pull` si rifiuta di procedere perché ci sono modifiche locali a file della repo, guarda
quali sono con `git status`. Per scartarle e tornare alla versione della repo usa
`git checkout -- <file>`. La configurazione va solo in `.env` e `system_prompt.txt`.

## Configurazione

Tutta la configurazione è nel file `.env` della cartella del proxy. Ogni variabile è descritta in
[`.env.example`](.env.example). `.env`, `system_prompt.txt` e `progress_phrases.json` sono esclusi da git,
quindi un `git pull` non sovrascrive la configurazione locale.

| File | Ruolo |
|---|---|
| `proxy.py` | proxy, streaming, tool MCP, API key |
| `compact_tool.py` | compattazione della memoria e tool `compact_memory` |
| `file_refs.py` | file aperti dai tool sostituiti dal loro percorso |
| `progress.py` | frasi di attesa durante ragionamenti e tool lunghi |
| `progress_phrases.example.json` | frasi di attesa di default, da copiare in `progress_phrases.json` per personalizzarle |
| `.env.example` | modello della configurazione, da copiare in `.env` |
| `system_prompt.example.txt` | modello del system prompt, da copiare in `system_prompt.txt` |
| `deploy/llm-proxy.service` | servizio systemd |
| `deploy/nginx-llm-proxy.conf` | configurazione nginx per l'esposizione su internet |

Le variabili impostate da systemd hanno la precedenza su quelle di `.env`: per non doverle
reinserire dopo ogni aggiornamento, conviene metterle nel servizio (vedi sotto).

### Variabili fisse nel servizio systemd

Le variabili che hai personalizzato (indirizzi di llama.cpp e MCP, API key, opzioni come
`SEND_CHAT_MARKDOWN`) conviene metterle in `/etc/llm-proxy/llm-proxy.env`, fuori dalla repo. Il servizio
della repo lo carica (`EnvironmentFile=-/etc/llm-proxy/llm-proxy.env`; il `-` lo rende facoltativo) e le
sue variabili hanno la precedenza su `.env`. Così `.env` può essere sostituito a ogni aggiornamento con
`.env.example` senza perdere la configurazione.

1. Crea il file con le variabili, nello stesso formato di `.env`:

   ```bash
   mkdir -p /etc/llm-proxy && nano /etc/llm-proxy/llm-proxy.env
   ```

   Esempio:

   ```ini
   LLAMA_URL=http://192.168.1.10:8080
   LLAMA_API_KEY=
   MCP_URL=http://192.168.1.11:8000/mcp
   MCP_TRANSPORT=http
   PROXY_API_KEYS=chiave-openwebui,chiave-telefono
   SEND_CHAT_MARKDOWN=true
   ```

2. Rendilo leggibile solo da root (systemd lo legge prima di avviare il servizio con l'utente
   `llmproxy`):

   ```bash
   chmod 600 /etc/llm-proxy/llm-proxy.env
   ```

3. Installa (o reinstalla) il servizio della repo, che carica il file, e riavvialo:

   ```bash
   cp /opt/llm-proxy/deploy/llm-proxy.service /etc/systemd/system/ && systemctl daemon-reload && systemctl restart llm-proxy
   ```

4. Verifica che il processo abbia ricevuto le variabili:

   ```bash
   tr '\0' '\n' < /proc/$(systemctl show -p MainPID --value llm-proxy)/environ | grep -E "LLAMA|MCP|SEND_CHAT|HISTORY"
   ```

Note:

- non mettere `HISTORY_DB` in questo file: la imposta già il servizio
  (`/var/lib/llm-proxy/history.db`), e un valore in `EnvironmentFile` avrebbe la precedenza;
- le variabili aggiunte nelle versioni future arrivano con il loro default dal nuovo `.env`: per
  cambiarne una, aggiungila a `/etc/llm-proxy/llm-proxy.env`, non a `.env`;
- dopo ogni modifica di `/etc/llm-proxy/llm-proxy.env` serve `systemctl restart llm-proxy`;
- se in passato hai creato il drop-in `/etc/systemd/system/llm-proxy.service.d/override.conf` per caricare
  lo stesso file, ora non serve più: lasciarlo non fa danni (il file viene letto due volte), oppure
  rimuovilo con `rm /etc/systemd/system/llm-proxy.service.d/override.conf && systemctl daemon-reload`.

## Cronologia delle conversazioni (database history)

I client conservano solo il testo visibile delle risposte: tool call, risultati e ragionamenti
restano al proxy. Senza di loro il modello, nei turni successivi, vede risposte che annunciano
un'azione ("Scarico l'immagine e la invio subito:") senza la chiamata al tool, e tende a imitarle.
Per questo il proxy salva ogni conversazione nel database `history` (SQLite):

- ogni nuova conversazione riceve un **UUID**; il database contiene l'intero scambio in ordine:
  messaggi utente, risposte, ragionamenti, tool call, risultati dei tool, immagini e audio dei tool;
- quando il client riprende la conversazione, il proxy la riconosce e ricostruisce i messaggi per
  llama.cpp: **system prompt**, poi i **messaggi letti dal database**, poi il nuovo messaggio. Le
  risposte dell'assistente si prendono sempre dal database, mai dal client: anche se il client le rimanda
  modificate (o senza annunci, box e ragionamenti), il modello ritrova lo scambio originale;
- se c'è stata una **compattazione**, il riassunto è salvato come messaggio: alla ripresa viene messo
  in coda al system prompt al posto dei messaggi che riassume, seguito dai messaggi recenti;
- **rigenerazione o modifica** di un messaggio nel client: i messaggi fino al punto in cui la
  conversazione diverge vengono copiati su una nuova conversazione (nuovo UUID), e il ramo originale
  resta intatto;
- se il client si disconnette a metà risposta, viene salvato quanto fatto fino a quel momento, compreso
  il testo già inviato.

**Come viene riconosciuta la conversazione.** Dai **messaggi dell'utente**: il proxy non li modifica
mai e il client li rimanda identici. Il turno N è identificato dalla sequenza dei primi N messaggi
dell'utente. Se il client invia anche l'id della chat (`HISTORY_CHAT_ID_HEADER`, default
`X-OpenWebUI-Chat-Id`) la ricerca è limitata a quella chat: è il modo più preciso, e Open WebUI lo invia
se nella sua configurazione è impostato `ENABLE_FORWARD_USER_INFO_HEADERS=true`. Con l'id della chat,
se i messaggi utente non coincidono (es. il client ha arricchito un messaggio con documenti o risultati
di ricerca solo in quella richiesta), il proxy riprende la conversazione della chat con lo stesso numero
di messaggi utente e lo segnala nel log come `WARNING`. Una chat mai vista dal proxy (es. precedente al
database) viene registrata con i messaggi inviati dal client e, dal turno successivo, è completa.

Con le funzioni del proxy attive **i tool inviati dal client vengono scartati** (es. i tool integrati di
Open WebUI): il modello vede solo i tool del server MCP, e la history vale per tutte le richieste.
Il database si configura con `ENABLE_HISTORY`, `HISTORY_DB` e `HISTORY_RETENTION_DAYS` (le conversazioni
non usate da più giorni vengono eliminate all'avvio). Con il servizio systemd della repo il file è
`/var/lib/llm-proxy/history.db`. I database delle versioni precedenti vengono aggiornati all'avvio.

### Risultati dei tool

I risultati dei tool arrivano al modello **interi** finché entrano nel contesto, e restano nella
cronologia. Ogni tipo di contenuto restituito da un tool MCP arriva al modello così:

| Contenuto | Come arriva al modello |
|---|---|
| testo | nel risultato del tool |
| immagine | in un messaggio subito dopo i risultati dei tool, come fa Open WebUI; nel risultato compare `[immagine 1]`, `[immagine 2]`… |
| audio (wav, mp3) | nello stesso messaggio, come audio (`input_audio`); nel risultato compare `[audio 1]`, `[audio 2]`… |
| risorsa incorporata di testo (JSON, CSV, Markdown…) | il suo testo nel risultato, preceduto dall'indirizzo della risorsa |
| risorsa incorporata binaria | immagini e audio come sopra; i file di testo codificati vengono decodificati |
| collegamento a una risorsa | nome, indirizzo, tipo, dimensione e descrizione nel risultato |

La numerazione di immagini e audio è unica per tutti i tool dello stesso giro, nell'ordine in cui
compaiono nei risultati.

**File aperti sostituiti dal loro percorso.** I file aperti dai tool (testi, pagine, immagini)
occupano contesto a ogni giro. Per questo il proxy tiene interi solo gli ultimi `FILE_KEEP_LAST` file
aperti (default 3); i precedenti diventano il loro percorso, e il modello li riapre se gli servono di nuovo:

```
[Contenuto di src/app.py tolto per risparmiare contesto: se ti serve di nuovo, riaprilo ripetendo la chiamata file_leggi.]
[immagine 1: screenshot/screenshot_….png, tolta per risparmiare contesto: se ti serve di nuovo, riaprila dal percorso]
```

- **Quali tool aprono file:** quelli che il server MCP marca in sola lettura (`readOnlyHint`) e che hanno
  un argomento con il percorso (`percorso`, `path`, `file`, `sorgente`, `url`, `uri`…); con il server
  della repo `ai-mcp-server`: `file_leggi`, `vedi_immagine`, `screenshot_pagina`, `leggi_pagina_web`,
  `file_elenca`, `file_cerca`. Contano anche i risultati con un'immagine di cui si conosce il percorso
  (il tool restituisce una sola immagine e il testo indica un solo percorso o URL d'immagine, es.
  `![...](url)` o un file `.png`, `.jpg`…: nel risultato compare `[immagine 1: percorso]`) e le risorse
  con un indirizzo.
- **Quando:** quando si apre un file oltre gli ultimi `FILE_KEEP_LAST`, il più vecchio diventa il suo
  percorso. Se la stessa chiamata viene ripetuta (stesso tool, stessi argomenti), la copia precedente
  diventa subito il percorso. Nella compattazione il primo passo è sostituire tutti i file aperti: spesso
  basta a rientrare nella soglia senza riassunto, altrimenti il riassunto riceve i percorsi.
- **Cosa resta intero:** i risultati dell'ultimo giro (il modello non li ha ancora letti), gli errori, i
  risultati brevi (circa 300 token: di uno screenshot, per esempio, viene tolta solo l'immagine), le
  anteprime di una ricerca (più immagini senza percorso) e le immagini caricate dall'utente.
- **Database:** conserva sempre i contenuti originali. Le sostituzioni vengono ricalcolate a ogni
  richiesta nello stesso modo, così il prompt resta stabile tra un turno e l'altro.
- **Costo:** ogni sostituzione cambia un messaggio già elaborato, quindi llama.cpp rielabora il prompt
  da quel punto una volta. Con `FILE_KEEP_LAST` più alto le sostituzioni sono più rare.
- `FILE_KEEP_LAST=-1` disattiva la funzione (anche nella compattazione).

Limiti:
- llama.cpp accetta solo testo, immagini e audio (wav e mp3). Degli altri file binari (PDF, archivi,
  documenti Office) e degli audio in altri formati il risultato riporta nome, tipo e dimensione.
- Con modelli che non accettano immagini o audio, llama.cpp rifiuterebbe la richiesta: imposta
  `TOOL_IMAGES=false` o `TOOL_AUDIO=false`. Nel risultato resta l'indicazione del contenuto non mostrato.

**Risultati più grandi del contesto.** Con `MAX_TOOL_RESULT_RATIO=auto` (default) il proxy calcola a
ogni giro lo spazio davvero libero e accorcia solo i risultati che non ci stanno, così non si arriva mai
all'errore di contesto superato di llama.cpp:

```
spazio per i risultati = n_ctx × COMPACT_THRESHOLD − spazio per la risposta − system prompt
                         − definizioni dei tool − riassunto massimo (COMPACT_SUMMARY_TOKENS)
                         − chiamata del tool − stima per immagini e audio (1024 token ciascuno)
```

- Lo spazio è diviso tra i risultati dello stesso giro: quelli piccoli restano interi, i grandi si
  dividono il resto.
- Un risultato accorciato mantiene inizio e fine, e al centro indica quanti caratteri mancano e perché.
- Al giro successivo la compattazione riassume i messaggi precedenti e tiene interi la chiamata e i
  suoi risultati: il prompt resta sotto la soglia di compattazione, con lo spazio per la risposta.
- I conteggi usano i token reali di llama.cpp (`/tokenize`). Per immagini e audio si usa una stima.
- Anche quando un risultato grande viene poi riassunto, il testo inviato al riassunto è misurato in
  token reali, perché JSON e codice hanno più token per carattere del testo normale.

Valori possibili: `auto`; `0` = nessun limite (un risultato più grande del contesto fa rispondere a
llama.cpp con un errore); un numero (es. `0.25`) = limite fisso, in frazione del contesto, per ogni risultato.

## Come funziona la compattazione

- `n_ctx` viene letto da llama.cpp (`GET /props`, fallback `GET /slots`) e aggiornato ogni
  `N_CTX_TTL` secondi: se cambi modello o contesto non serve riavviare il proxy.
- I token vengono contati con `/v1/chat/completions/input_tokens`; sulle versioni che non
  lo hanno si usa `/apply-template` + `/tokenize`, e in ultima istanza una stima.
- I messaggi recenti restano intatti, quelli vecchi diventano un riassunto aggiunto al system
  prompt. Le conversazioni molto lunghe vengono riassunte a blocchi.
- I ragionamenti non entrano nei riassunti: si riassumono solo il testo dei messaggi e le chiamate
  ai tool, e i blocchi di ragionamento rimandati dai client vengono tolti prima.
- Per le conversazioni nel database history il riassunto è salvato nel database. Per le richieste
  senza history i riassunti sono tenuti in cache (in memoria) per prefisso di conversazione: il client
  rimanda tutta la cronologia a ogni turno, ma il riassunto viene calcolato una volta sola.

## Uso dei tool e ragionamenti in chat

Prima di usare un tool il proxy lo annuncia in chat con una frase naturale (vedi
[Annunci dei tool](#annunci-dei-tool)), per esempio:

```
Cerco tra i contatti...
```

`SEND_CHAT_MARKDOWN` decide se aggiungere markup che solo alcuni client sanno interpretare:

| | `false` (default) | `true` |
|---|---|---|
| Ragionamenti | campo standard `reasoning_content` | blocchi `<think>...</think>` nel testo, nel punto in cui avvengono |
| Dopo ogni tool | nulla | box `<details type="tool_calls">` con argomenti, risultato e immagini restituite |
| Adatto a | qualunque client OpenAI-compatibile | **Open WebUI** |

Con `true`, in Open WebUI una risposta con un tool appare come: ragionamento → annuncio →
box nativo del tool (argomenti in ingresso, risultato in uscita; i tool falliti sono segnati come
errore) → ragionamento → risposta. Il box compare quando il tool ha finito; mentre lavora resta
visibile l'annuncio.

Le immagini restituite da un tool (es. le anteprime di `cerca_immagini`, uno screenshot) compaiono
sotto il box, anche quando è chiuso, come quando Open WebUI esegue i tool da solo: il proxy le mette
nell'attributo `files` del box. Vengono mostrate all'utente anche con `TOOL_IMAGES=false`. Restano
salvate in base64 nella chat di Open WebUI, che quindi occupa più spazio; il proxy toglie i box dalla
cronologia rimandata dal client, quindi non pesano sul modello. Con `false` le immagini dei tool
arrivano solo al modello: per mostrarle all'utente il modello deve scrivere il loro markdown nella
risposta (`![descrizione](URL)`).

Con `false` non c'è markup: i client che non interpretano questi tag non vedono `<think>` o
`<details …>` grezzi in chat. Open WebUI in questo caso unisce tutti i ragionamenti in un solo
blocco in cima alla risposta, perché tratta così `reasoning_content` quando c'è già del testo.

In entrambi i casi ragionamenti, box e annunci non tornano al modello nei turni
successivi: se un client li rimanda nella cronologia, il proxy li toglie.

## Annunci dei tool

Il proxy annuncia l'uso di un tool con frasi brevi e naturali, adatte alle conversazioni vocali: per
esempio «Cerco tra i contatti...» o «Verifico se conosco il contatto...» al posto del nome del tool.

- **Generazione una volta sola.** Quando il proxy si collega al server MCP, chiede allo stesso modello
  (llama.cpp) 5 frasi per ogni tool, partendo da titolo e descrizione. La generazione avviene in
  background, **5 tool per richiesta** (il ragionamento si fa una volta per gruppo), e **solo per i tool
  che non hanno ancora frasi**: al riavvio, o quando il proxy si ricollega al server MCP, i tool già
  fatti non vengono rigenerati.
- **Come vengono generate.** Per ogni gruppo di 5 tool:
  1. **generazione**: il modello scrive 5 frasi per tool seguendo un prompt con regole precise (italiano
     corretto, verbo all'inizio alla prima persona come «Cerco» o «Controllo», da 2 a 6 parole, lo scopo
     dell'azione per l'utente e non i passaggi tecnici, solo azioni che il tool fa davvero, frasi valide per
     qualunque uso del tool, niente riempitivi né costruzioni pesanti), con esempi buoni e da evitare;
  2. **revisione**: un secondo prompt, con le stesse regole, chiede al modello di rileggere le frasi
     proposte e di correggere o sostituire quelle sbagliate;
  3. **validazione frase per frase**: un terzo prompt chiede al modello di controllare ogni singola frase
     (verbo al presente e non al passato o all'infinito, azione che il tool fa davvero, valida per ogni uso,
     naturale e chiara, senza riempitivi né costruzioni vietate) e di elencare solo quelle da correggere,
     con la correzione e un motivo breve. Corregge solo violazioni chiare delle regole, non preferenze di
     stile; le correzioni devono essere in italiano corretto e non identiche alle altre frasi del tool
     (frasi simili vanno bene). Nel log compare ogni correzione con il motivo;
  4. **controllo automatico** delle regole verificabili nel codice: la prima parola deve essere un verbo
     alla prima persona (finisce in «o»), niente «Vado a», «Sto per», «Procedo a/con», niente riempitivi
     finali («adesso», «subito»…), al massimo 7 parole. Le frasi scartate compaiono nel log con il motivo.

  Se la revisione non dà frasi valide si usano quelle generate; se la validazione non risponde per un
  tool si usano le frasi della revisione. Un tool con meno di 3 frasi valide viene
  rigenerato una volta alla fine; se ne restano meno di 2 usa le frasi generiche. Il modello lavora senza
  ragionamento, con i parametri di campionamento (temperatura, top_p…) impostati in `llama-server`; con
  `ANNOUNCEMENTS_THINKING=true` il ragionamento si attiva (a prescindere da `ENABLE_THINKING`): frasi un
  po' più curate, ma generazione molto più lenta, anche 15 minuti per gruppo, con llama.cpp occupato. I
  testi completi sono `GENERATION_PROMPT`, `REVIEW_PROMPT`, `VALIDATION_PROMPT` e `RULES` in
  `announcements.py`. Ogni gruppo di 5 tool richiede circa 2 minuti.
- **File degli annunci: `tool_announcements.json`**, nella stessa cartella del database history. Con il
  servizio systemd della repo è `/var/lib/llm-proxy/tool_announcements.json`; il percorso si cambia con
  `ANNOUNCEMENTS_FILE`. Le frasi sono raggruppate per **URL del server MCP**, così tool con lo stesso
  nome su server diversi non si sovrascrivono:

  ```json
  {
    "http://192.168.1.11:8000/mcp": {
      "rubrica_cerca": {
        "titolo": "Cerca contatti",
        "frasi": ["Cerco tra i contatti...", "Verifico se conosco il contatto...", "Recupero il contatto..."],
        "versione": 2,
        "generato": "2026-11-20 10:15:00"
      }
    }
  }
  ```

  Il file si può modificare a mano: le modifiche valgono dal riavvio successivo del proxy. Per
  rigenerare le frasi di un tool basta cancellarne la voce e riavviare.
- **Versione del prompt.** `versione` indica con quale versione del prompt sono state generate le frasi.
  Quando il prompt viene migliorato, le voci generate con la versione precedente vengono rigenerate
  automaticamente. Per conservare le frasi di un tool che hai corretto a mano, aggiungi
  `"manuale": true` alla sua voce: non verrà mai rigenerata.
- **Frasi generiche.** Finché le frasi di un tool non sono pronte, o se la generazione non riesce (si
  riprova alla connessione successiva), il proxy usa frasi generiche come «Un attimo...» o
  «Ci lavoro...».
- **Log.** Per ogni richiesta il log riporta durata, `finish_reason` e lunghezza di testo e ragionamento;
  per un tool senza frasi valide anche la parte finale del testo ricevuto. `finish_reason=length`
  significa che il ragionamento ha esaurito i token prima della risposta. Per seguire la generazione:
  `journalctl -u llm-proxy -f | grep annunci`.
- **Una volta per tool.** All'interno di una risposta l'annuncio compare solo quando cambia il tool:
  cinque download di fila producono un solo «Scarico i file...». La frase è scelta a caso tra quelle del
  tool, senza ripetere l'ultima usata.
- Gli annunci non tornano al modello: se un client li rimanda nella cronologia, il proxy li toglie.

Se llama.cpp ha un solo slot, ogni generazione sostituisce la conversazione che aveva in cache: il
messaggio successivo dell'utente viene rielaborato per intero. Succede solo la prima volta per ogni tool.

## Frasi di attesa

Quando ragionamenti lunghi e tool si susseguono, la chat può restare a lungo senza testo e sembrare
bloccata (con un assistente vocale: silenzio). Per questo il proxy scrive una frase di attesa dopo
`PROGRESS_FIRST_SECONDS` secondi senza testo visibile (default 10), poi una ogni `PROGRESS_EVERY_SECONDS`
(default 20) finché il silenzio continua:

```
Sto ragionando...

Ancora qualche secondo...
```

- **Testo visibile:** il testo scritto dal modello, gli annunci dei tool, gli avvisi del proxy e i box
  dei tool. I ragionamenti non contano, perché non vengono letti ad alta voce. Ogni testo visibile fa
  ripartire il conteggio dai 10 secondi.
- **Frasi adatte alla fase**, scelte a caso senza ripetere l'ultima e preferendo quelle non ancora usate
  nella risposta; quando quelle della fase sono esaurite si usano le generiche:

  | Fase | Quando | Esempio |
  |---|---|---|
  | `attesa` | llama.cpp elabora la richiesta, prima del primo token | «Sto elaborando la richiesta...» |
  | `risultati` | dopo un tool, llama.cpp legge i risultati | «Sto leggendo i risultati...» |
  | `ragionamento` | il modello ragiona | «Sto ragionando...» |
  | `preparazione` | il modello scrive gli argomenti di una chiamata (es. un file lungo) | «Sto preparando l'operazione...» |
  | `strumento` | un tool è in esecuzione | «L'operazione richiede un po' di tempo...» |
  | `generiche` | in tutte le fasi, dopo quelle della fase | «Sto ancora elaborando...» |

- **Personalizzazione:** le frasi di default sono in `progress_phrases.example.json`. Per cambiarle:
  ```bash
  cp /opt/llm-proxy/progress_phrases.example.json /opt/llm-proxy/progress_phrases.json && nano /opt/llm-proxy/progress_phrases.json && systemctl restart llm-proxy
  ```
  `progress_phrases.json` è escluso da git, quindi gli aggiornamenti non lo toccano. Un altro percorso si
  indica con `PROGRESS_PHRASES_FILE`.
- Con `SEND_CHAT_MARKDOWN=true` la frase chiude il blocco `<think>` in corso, così resta visibile.
- Le frasi non tornano al modello: non vengono salvate nella sua risposta e, se un client le rimanda
  nella cronologia, il proxy le toglie.
- Nei momenti senza frasi il proxy invia dei keepalive SSE invisibili (anche mentre llama.cpp elabora un
  prompt lungo), per evitare timeout del client o del reverse proxy.
- `PROGRESS_FIRST_SECONDS=0` disattiva le frasi.

## Attivare o disattivare il ragionamento

`ENABLE_THINKING` decide se il modello ragiona (thinking) prima di rispondere:

| Valore | Effetto |
|---|---|
| vuoto (default) | il proxy non interviene: decidono llama.cpp (`--reasoning`), il template del modello o il client |
| `false` | ragionamento disattivato in ogni richiesta: risposte più rapide e meno token |
| `true` | ragionamento attivato in ogni richiesta, per i modelli che di default non ragionano |

Con `true` o `false` l'impostazione del proxy prevale su quella inviata dal client. Il proxy invia a
llama.cpp `chat_template_kwargs.enable_thinking` (letto dai template Qwen e simili) e, per disattivare,
anche `reasoning_effort: "none"`, che le versioni recenti di llama.cpp applicano con qualunque template.
Su versioni meno recenti, con modelli che non usano `enable_thinking`, la disattivazione potrebbe non
avere effetto: in quel caso usa `--reasoning off` all'avvio di `llama-server`.

## Statistiche (token e velocità)

Il proxy restituisce i campi `usage` e `timings` di llama.cpp. In streaming si trovano nell'ultimo chunk, insieme a
`finish_reason`, più il chunk standard con `choices: []` se il client chiede
`stream_options.include_usage`. Quando una risposta richiede più giri di tool:

- `completion_tokens`, `prompt_n`, `predicted_n` e i tempi sono la **somma** di tutti i giri;
  le velocità (`*_per_second`, `*_per_token_ms`) sono ricalcolate sui totali;
- `prompt_tokens` è il contesto dell'ultima richiesta a llama.cpp, e `cache_n` è calcolato in modo
  che `cache_n + prompt_n + predicted_n` resti il contesto reale occupato;
- il tempo di esecuzione dei tool MCP non è incluso.

## Tool call scritte come testo

A volte i modelli "thinking" scrivono la chiamata a un tool **dentro il ragionamento**
(o nel testo della risposta) invece di emetterla come tool call, ad esempio:

```
<tool_call> <function=terminale_esegui> <parameter=comando> ls -la </parameter> </function> </tool_call>
```

llama.cpp riconosce le tool call solo nella risposta, quindi le vede come semplice testo: il turno
finisce senza tool call e la risposta si interrompe. Quando un giro finisce senza tool call, il
proxy cerca queste chiamate nel ragionamento e nel testo (formato XML dei modelli Qwen e formato
JSON `<tool_call>{"name": ..., "arguments": ...}</tool_call>`) e le esegue come se fossero arrivate
correttamente. Vengono considerati solo i tool esistenti, e ogni recupero è segnalato nel log
come `WARNING`. Queste chiamate vengono anche tolte dalla cronologia, perché il modello non
le prenda a esempio.

Se nel log compaiono spesso, conviene aggiornare llama.cpp e verificare che `llama-server`
usi `--jinja` con il template corretto del modello.

## System prompt

Il modello riceve **solo** il system prompt del proxy (`system_prompt.txt`). Tutti i messaggi
`system` e `developer` inviati dal client vengono rimossi, in qualunque punto della conversazione.

Con Open WebUI questo significa che non arrivano al modello neanche il prompt impostato sul
modello in Open WebUI, le memorie dell'utente e il contesto RAG/Knowledge, se Open WebUI li
inserisce come messaggio di sistema. Le istruzioni vanno quindi messe in `system_prompt.txt`.

In coda al system prompt il proxy aggiunge la **data e l'ora di inizio della conversazione**, nel fuso
`TIMEZONE` (default `Europe/Rome`), con l'invito a usare lo strumento apposito per la data e l'ora
correnti. Restano le stesse per tutta la conversazione e nei suoi rami: così il system prompt non
cambia tra un messaggio e l'altro e llama.cpp riusa la parte di prompt già elaborata.

Subito dopo aggiunge l'**ID della conversazione** (l'UUID del database history), per i tool del server
MCP che lo richiedono. Anche questo resta lo stesso per tutta la conversazione; un ramo (rigenerazione o
modifica di un messaggio) è una nuova conversazione e ha un nuovo ID. Le richieste senza history (database
disattivato o non disponibile) non hanno un ID.

## Giri di tool e protezione dai loop

Di default (`MAX_TOOL_ROUNDS=0`) una risposta può usare **quanti giri di tool servono**. Il proxy
interrompe solo i **loop**: se il modello ripete `TOOL_REPEAT_LIMIT` volte di seguito (default 3) la
stessa chiamata, cioè stesso tool e stessi argomenti, ottenendo lo stesso risultato, il giro successivo
è senza tool. Il risultato conta: controllare più volte lo stato di un comando in background, con un
risultato che cambia, non è un loop.

Con `MAX_TOOL_ROUNDS` maggiore di 0 c'è anche un tetto ai giri, come rete di sicurezza contro un modello
che chiama tool sempre diversi senza fermarsi (es. `MAX_TOOL_ROUNDS=100`).

Nel giro senza tool il modello viene avvisato: invece di annunciare un'azione che non può eseguire
("Procedo con l'invio!"), spiega cosa ha fatto e cosa resta da fare e chiede come procedere.
Eventuali tool call in quel giro vengono ignorate. In chat compare
`Interrotta la ripetizione di "<tool>"` oppure `Limite di N giri di strumenti raggiunto`, e nel log un
`WARNING`. Per proseguire basta rispondere «continua».

## Inoltro puro e log degli scambi

Per capire se un comportamento del modello dipende dal proxy si possono confrontare due modalità:

- `ENABLE_PROXY_FEATURES=true` (default): tutte le funzioni del proxy;
- `ENABLE_PROXY_FEATURES=false`: **inoltro puro**. `/v1/chat/completions` arriva a llama.cpp esattamente
  come lo invia il client e la risposta torna al client così com'è, anche in streaming: niente system
  prompt, tool MCP, history, compattazione, annunci, protezione dai loop o recupero delle tool call. Il
  proxy non si collega al server MCP: i tool restano quelli del client (es. quelli configurati in
  Open WebUI), come se il client fosse collegato direttamente a llama.cpp.

Con `LOG_EXCHANGES=true` ogni scambio viene scritto in un file per modalità, nella cartella del database
history (con il servizio `/var/lib/llm-proxy/`): `exchanges-avanzata.jsonl` con le funzioni attive ed
`exchanges-inoltro.jsonl` con l'inoltro puro. Il percorso si può fissare con `EXCHANGE_LOG`. Una riga JSON
per evento:

| Evento | Contenuto |
|---|---|
| `client_request` | la richiesta come arriva dal client |
| `llama_request` | cosa viene inviato a llama.cpp, cioè cosa vede il modello (con le funzioni attive, una per ogni giro di tool) |
| `llama_response` | la risposta di llama.cpp, ricomposta anche in streaming: testo, ragionamento, tool call, `finish_reason` |
| `client_response` | cosa riceve il client (con le funzioni attive comprende annunci e box dei tool) |

Gli eventi della stessa richiesta hanno lo stesso `request_id`; immagini e audio in base64 sono abbreviati.
Il log contiene le conversazioni intere: attivalo solo quando serve e cancellalo dopo l'analisi.
Per seguire una richiesta:

```bash
grep '"request_id": "<id>"' /var/lib/llm-proxy/exchanges-avanzata.jsonl
```

## Diagnostica

Per ogni risposta il log riporta una riga `risposta conclusa` con giri, tool eseguiti,
`finish_reason` dell'ultimo giro e lunghezza del testo finale. Per capire perché un turno si è
chiuso (per esempio con un'azione annunciata ma non eseguita):

```bash
journalctl -u llm-proxy --since "10 min ago" | grep -E "risposta conclusa|WARNING"
```

| Nel log | Significato |
|---|---|
| `ultimo giro senza tool` | il modello ha ripetuto in loop la stessa chiamata, oppure ha usato tutti i `MAX_TOOL_ROUNDS` giri (il `WARNING` precedente dice quale) |
| `WARNING ... finish_reason=tool_calls ma senza tool call` | il modello ha provato a chiamare un tool, ma la chiamata era malformata o non riconosciuta da llama.cpp |
| `WARNING ... finish_reason=length` | risposta troncata dal limite di token |
| `finish_reason=stop` senza avvisi | il modello ha scelto di chiudere il turno |

## Limiti

- Viene gestito solo `/v1/chat/completions`. Gli altri endpoint passano invariati, senza
  system prompt né tool.
- Senza id della chat, due conversazioni con gli stessi identici messaggi dell'utente non si
  distinguono: il proxy riprende la più recente. Con `X-OpenWebUI-Chat-Id` questo non succede.
- La cache in memoria dei riassunti, usata solo per le richieste senza history, si svuota al riavvio.
