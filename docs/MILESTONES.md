# Milestones

Registro delle attività svolte sul progetto e di quelle in programma.
Aggiornalo a ogni modifica: sposta le voci da **Prossimi passi** a una milestone completata,
indicando data e commit.

Legenda: `[x]` completato · `[ ]` da fare · `[~]` in corso

---

## Completate

### M1 · Proxy base
_2026-11-17 · commit `c4a0120`_

- [x] Proxy OpenAI-compatibile (FastAPI + httpx) davanti a `llama-server`
- [x] System prompt da file aggiunto a ogni chat; se il client ne ha già uno, i due vengono uniti (sostituito in M15: resta solo quello del proxy)
- [x] Connessione al server MCP (streamable HTTP o SSE) e conversione dei tool MCP nel formato `tools` di OpenAI
- [x] Esecuzione dei tool MCP lato proxy, con ripetizione dei giri finché il modello dà la risposta finale
- [x] Tool definiti dal client restituiti al client senza eseguirli (in caso di chiamate miste, il client riceve solo i suoi)
- [x] Limite di giri di tool (`MAX_TOOL_ROUNDS`); all'ultimo giro `tool_choice: none` obbliga una risposta testuale
- [x] Tutti gli altri endpoint (`/v1/models`, `/health`, `/v1/embeddings`…) inoltrati invariati, anche in streaming
- [x] Supporto a `LLAMA_API_KEY` per llama-server avviato con `--api-key`

### M2 · Streaming reale
_2026-11-17 · commit `c4a0120`_

- [x] Testo e `reasoning_content` inoltrati al client token per token
- [x] Le tool call vengono accumulate dai delta e trattenute; quelle MCP sono eseguite dal proxy
- [x] Il giro successivo continua nello stesso flusso SSE: il client vede un'unica risposta, con un unico `id`
- [x] Il testo di giri diversi è separato da una riga vuota
- [x] Keepalive SSE (`: keepalive`) ogni 15 s mentre un tool lavora, per evitare i timeout
- [x] `usage` finale inoltrato se il client chiede `stream_options.include_usage`
- [x] Errori di llama.cpp e di rete restituiti come evento SSE `error`

### M3 · Avvisi dei tool in chat
_2026-11-17 · commit `c4a0120`_

- [x] Messaggio «🔧 Uso nome_strumento...» prima di ogni tool, sia in streaming sia senza
- [x] Modalità configurabile con `TOOL_STATUS`: `content`, `reasoning`, `off`
- [x] Le righe di stato del proxy vengono tolte dalla cronologia reinviata dal client, prima che arrivi al modello

### M4 · Compattazione della memoria (`compact_tool.py`)
_2026-11-17 · commit `c4a0120`_

- [x] Logica in un file separato dal proxy
- [x] `n_ctx` letto da llama.cpp (`GET /props`, fallback `GET /slots`) con cache di `N_CTX_TTL` secondi; per la modalità router viene passato il parametro `model`
- [x] Conteggio dei token con `/v1/chat/completions/input_tokens`, fallback `/apply-template` + `/tokenize`, poi stima sui caratteri
- [x] Compattazione automatica oltre `COMPACT_THRESHOLD` (80%), riducendo il prompt sotto `COMPACT_TARGET` (50%)
- [x] Riassunto fatto dallo stesso modello, a blocchi per le conversazioni molto lunghe; il ragionamento dei modelli "thinking" è disattivato per il riassunto
- [x] Riassunto aggiunto al system prompt; i messaggi recenti restano intatti e una chiamata a un tool non viene mai separata dal suo risultato
- [x] Cache dei riassunti per prefisso di conversazione: tra un turno e l'altro il riassunto non viene ricalcolato
- [x] Tool `compact_memory` esposto al modello, con parametro facoltativo `focus`
- [x] Risultati dei tool troncati oltre `MAX_TOOL_RESULT_RATIO` (25%) del contesto
- [x] Avviso in chat «🗜️ Memoria compattata (X → Y token su N)»
- [x] Se la compattazione fallisce, la chat continua con i messaggi originali

### M5 · Robustezza
_2026-11-17 · commit `c4a0120`_

- [x] Connessione MCP in background: il proxy parte anche se il server MCP non è ancora pronto
- [x] Riconnessione automatica e un nuovo tentativo della chiamata quando il server MCP viene riavviato (risolto l'errore «Session not found»)
- [x] Il modello che chiama un tool inesistente riceve un errore con l'elenco dei tool disponibili
- [x] Argomenti JSON non validi restituiti al modello come errore, così può correggersi
- [x] Compatibilità con l'SDK `mcp` 1.x e 2.x

### M6 · Pubblicazione
_2026-11-17 · commit `c4a0120`_

- [x] Repository GitHub `andreadegiovine/ai-llm-api-proxy`
- [x] Servizio systemd `llm-proxy.service` con tutte le impostazioni
- [x] `README.md` con installazione su Proxmox (LXC), configurazione e limiti
- [x] `requirements.txt`, `system_prompt.txt` di esempio, `.gitignore`

#### Verifiche svolte

Prove manuali con un finto `llama-server` e un server MCP di prova, non ancora con llama.cpp reale:

- [x] Risposta semplice con system prompt aggiunto, in streaming e senza
- [x] Tool MCP in streaming e non: avviso «🔧 Uso …», risultato e risposta finale
- [x] Tool del client restituito al client
- [x] Limite dei giri di tool rispettato
- [x] Risultato di un tool molto grande troncato
- [x] Compattazione automatica (4784 → 567 token su un contesto di 4096) e riuso della cache al turno successivo
- [x] Tool `compact_memory` chiamato dal modello
- [x] Tool lento (17 s) con keepalive
- [x] Server MCP spento all'avvio, acceso dopo, poi riavviato

### M7 · Configurazione con `.env`, installazione su Proxmox, esposizione su internet
_2026-11-17_

- [x] Configurazione letta da `.env` (python-dotenv); le variabili d'ambiente già presenti hanno la precedenza
- [x] `.env.example` con tutte le variabili commentate, da copiare prima del primo avvio
- [x] `.env` e `system_prompt.txt` esclusi da git; nella repo ci sono `.env.example` e `system_prompt.example.txt`, così i `git pull` non vanno in conflitto
- [x] Se manca `system_prompt.txt`, il proxy usa il prompt di esempio e lo segnala nel log
- [x] Avvio diretto con `python proxy.py`; host e porta in `.env` (`PROXY_HOST`, `PROXY_PORT`)
- [x] Autenticazione dei client con `PROXY_API_KEYS` (Bearer o `x-api-key`, una chiave per dispositivo); `/health` resta libero
- [x] IP reale dei client nei log dietro nginx (`FORWARDED_ALLOW_IPS`)
- [x] Disattivate le pagine `/docs` e `/openapi.json` di FastAPI
- [x] File di deploy in `deploy/`: servizio systemd con utente dedicato `llmproxy`, configurazione nginx
- [x] `docs/PROXMOX.md`: container con il community script Debian, risorse minime, installazione, firewall, aggiornamenti
- [x] `docs/NGINX.md`: nginx + Let's Encrypt, Nginx Proxy Manager, API key, firewall, checklist di sicurezza, alternative VPN e Cloudflare Tunnel

### M8 · Avvisi dei tool con il titolo, senza icone
_2026-11-18_

- [x] L'avviso usa il `title` del tool MCP tra virgolette («Uso "Data e ora corrente"...»), poi `annotations.title`, infine il nome tecnico
- [x] Rimosse le icone 🔧 e 🗜️ dagli avvisi di tool e compattazione
- [x] Il tool `compact_memory` compare come «Uso "Compattazione della memoria"...»
- [x] Dalla cronologia vengono tolte solo le righe «Uso …» che corrispondono a un tool conosciuto, per non cancellare testo del modello; le righe nei formati precedenti (con icona o senza virgolette) vengono ancora riconosciute
- [x] `docs/PROXMOX.md`: sezione «Risoluzione problemi» (es. `status=217/USER` quando manca l'utente `llmproxy`)

### M9 · Statistiche di llama.cpp verso i client
_2026-11-18_

- [x] Inoltrati `usage` e `timings` di llama.cpp (prima andavano persi, perché lo stream viene ricostruito dal proxy)
- [x] In streaming: statistiche nell'ultimo chunk con `finish_reason`, più il chunk standard `choices: []` se il client chiede `stream_options.include_usage`
- [x] Con `timings_per_token` i `timings` sono inoltrati anche sui chunk intermedi
- [x] Inoltrati `prompt_progress` (`return_progress`) e gli altri campi extra dei chunk
- [x] Statistiche sommate su tutti i giri di tool, con le velocità ricalcolate; `cache_n` calcolato in modo che `cache_n + prompt_n + predicted_n` resti il contesto reale
- [x] Anche la risposta non in streaming riporta `usage` e `timings` sommati

### M10 · Ragionamenti al punto giusto con più giri di tool
_2026-11-18_

- [x] Analizzato il comportamento di Open WebUI: con `reasoning_content`, i ragionamenti arrivati dopo il primo testo vengono aggiunti al primo blocco in cima
- [x] Nuova opzione `REASONING_FORMAT`: `native` (default, `reasoning_content`) oppure `think` (blocchi `<think>` nel testo, nel punto in cui avvengono)
- [x] Con `think`, Open WebUI mostra un blocco di ragionamento sotto ogni `Uso "<tool>"...`
- [x] Anche la risposta non in streaming conserva l'ordine: ragionamenti, testo dei giri di tool, avvisi e risposta finale
- [x] Con `native`, i ragionamenti di giri diversi sono separati da una riga vuota
- [x] I blocchi di ragionamento rimandati dai client nel testo (`<think>`, `<details type="reasoning">`) vengono tolti dalla cronologia: non tornano al modello né finiscono nei riassunti

### M11 · Guida all'aggiornamento
_2026-11-18_

- [x] Sezione «Aggiornamento» nel README: `git pull`, dipendenze, nuove variabili di `.env.example`, reinstallazione del servizio se cambiato, riavvio e controllo del log

### M12 · Box nativi dei tool in Open WebUI
_2026-11-18_

- [x] Analizzato il frontend di Open WebUI: i blocchi `<details type="tool_calls">` nel testo vengono mostrati con il componente nativo dei tool (nome, argomenti, risultato)
- [x] Nuovo valore `TOOL_STATUS=openwebui`: dopo ogni tool viene inserito il box con il `title` del tool, gli argomenti e il risultato; i tool in errore hanno `status="failed"`
- [x] Attributi in JSON su una riga e con escape HTML (virgolette, a capo, `<`, `>`, `&` verificati con lo stesso parsing del frontend)
- [x] Box anche nella risposta non in streaming, nell'ordine corretto
- [x] I box rimandati dai client vengono tolti dalla cronologia, come i ragionamenti
- [x] README: configurazione consigliata per Open WebUI (`TOOL_STATUS=openwebui`, `REASONING_FORMAT=think`)

### M13 · `SEND_CHAT_MARKDOWN` al posto di `TOOL_STATUS` e `REASONING_FORMAT`
_2026-11-18_

- [x] La riga `Uso "<titolo del tool>"...` compare sempre, prima di ogni tool
- [x] Nuova variabile booleana `SEND_CHAT_MARKDOWN` (default `false`): con `true` invia ragionamenti come `<think>` e, dopo ogni tool, il box `<details type="tool_calls">`; con `false` solo testo e `reasoning_content`
- [x] Rimosse `TOOL_STATUS` e `REASONING_FORMAT`; se sono ancora nel `.env`, il log segnala che non sono più usate
- [x] Con il box attivo, mentre il tool lavora resta visibile la riga `Uso "…"...`

### M14 · Recupero delle tool call scritte come testo
_2026-11-18_

- [x] Analizzata la risposta interrotta: il modello aveva scritto la tool call (formato XML Qwen) dentro il ragionamento, llama.cpp non l'aveva riconosciuta e il turno era finito senza tool call né testo
- [x] Quando un giro finisce senza tool call, il proxy cerca chiamate scritte come testo nel ragionamento e nella risposta (XML `<function=…><parameter=…>` e JSON stile Hermes) e le esegue
- [x] Solo tool esistenti: i nomi citati come esempio non vengono eseguiti; i parametri non stringa sono convertiti secondo lo schema del tool
- [x] Ogni recupero è segnalato nel log come `WARNING`
- [x] Le tool call scritte come testo vengono tolte dalla cronologia, perché il modello non le prenda a esempio

### M15 · Solo il system prompt del proxy
_2026-11-18_

- [x] Rimossi tutti i messaggi `system` e `developer` del client, in qualunque posizione; resta solo il system prompt del proxy
- [x] README: sezione «System prompt», con l'effetto su Open WebUI (prompt del modello, memorie, contesto RAG inserito come system)

### M16 · Limite dei giri di tool senza azioni annunciate e mai eseguite
_2026-11-18_

- [x] Analizzate le risposte che finivano con «Procedo con l'invio!» senza eseguire nulla: all'ultimo giro consentito il proxy impostava `tool_choice: none` senza dirlo al modello
- [x] All'ultimo giro il modello riceve una nota: non può usare altri tool, deve riassumere cosa ha fatto e cosa resta da fare e chiedere se continuare
- [x] Avviso in chat `Limite di N giri di strumenti raggiunto` (tolto dalla cronologia) e `WARNING` nel log
- [x] `MAX_TOOL_ROUNDS` di default portato da 8 a 20

### M17 · Diagnostica di fine risposta
_2026-11-18_

- [x] Riga `risposta conclusa` nel log per ogni risposta: giri, tool eseguiti, `finish_reason` dell'ultimo giro, lunghezza di testo e ragionamento finali, eventuale limite dei giri raggiunto
- [x] `WARNING` quando llama.cpp chiude con `finish_reason=tool_calls` senza tool call utilizzabili (tool call malformata o non riconosciuta), con la parte finale di ragionamento e testo
- [x] `WARNING` quando la risposta è troncata dal limite di token (`finish_reason=length`)

### M18 · Verifica di fine turno
_2026-11-18_

> Rimossa in M19: era un rimedio a valle; la causa (cronologia incompleta inviata al modello) è corretta in M19.

- [x] Dal log (`giri=2, tool eseguiti=1, finish_reason=stop`): il modello annunciava l'azione successiva e chiudeva il turno di sua scelta, dentro la stessa risposta
- [x] `AUTO_CONTINUE` (default attivo): se dopo aver usato strumenti il turno finisce con un testo breve (`AUTO_CONTINUE_MAX_CHARS`, default 400) e senza tool call, giro in più con una nota per il modello
- [x] Se il modello ha un'azione in sospeso la esegue; se risponde `FATTO`, il giro di verifica non viene mostrato (in streaming il suo output è trattenuto finché non si sa)
- [x] Giro di verifica senza ragionamento (`enable_thinking: false`), al massimo una volta per risposta, segnalato nel log
- [x] La verifica scatta anche senza tool già usati nella risposta, quando il testo breve finisce con i due punti (es. «Scarico l'immagine… e la invio subito:», `giri=1, tool eseguiti=0`)

### M19 · Database history e ragionamento tra i giri di tool
_2026-11-19_

- [x] Causa delle azioni annunciate e mai eseguite: il problema non si presenta usando llama.cpp e MCP senza proxy. Open WebUI rimanda al modello la conversazione completa (tool call, risultati, ragionamenti), il proxy solo il testo visibile
- [x] Il ragionamento fatto prima di una tool call torna al modello nel giro successivo (`reasoning_content` nel messaggio assistant con le tool call), come fa Open WebUI con llama.cpp
- [x] Nuovo modulo `history.py`: database SQLite `history` con tabelle `conversations` (UUID per conversazione, chat id del client, ramo di origine), `messages` (intero scambio: utente, risposte, ragionamenti, tool call, risultati, riassunti) e `turns` (impronta del testo visibile per ogni turno)
- [x] Ripresa della conversazione: system prompt, poi messaggi letti dal database, poi il nuovo messaggio; riconoscimento tramite `X-OpenWebUI-Chat-Id` (se inviato) o tramite il testo visibile dei messaggi precedenti
- [x] Compattazione salvata come messaggio `summary`: alla ripresa va in coda al system prompt (llama.cpp converte `developer` in `system`, e i template Qwen rifiutano un secondo messaggio di sistema) al posto dei messaggi che riassume; i messaggi recenti restano intatti
- [x] Rigenerazione o modifica di un messaggio: messaggi fino a quel punto copiati su una nuova conversazione (nuovo UUID, con riferimento al ramo di origine)
- [x] Chat mai viste dal proxy registrate con i messaggi del client; turni interrotti salvati; turno salvato prima della fine dello stream
- [x] Richieste con tool definiti dal client senza history
- [x] Servizio systemd: `StateDirectory=llm-proxy` e `HISTORY_DB=/var/lib/llm-proxy/history.db` (la cartella del proxy non è scrivibile dall'utente del servizio); se il database non si apre, il proxy continua senza history
- [x] Nuove variabili: `ENABLE_HISTORY`, `HISTORY_DB`, `HISTORY_RETENTION_DAYS`, `HISTORY_CHAT_ID_HEADER`
- [x] Rimossa la verifica di fine turno (M18)
- [x] Verifica con finto llama.cpp e client che simula Open WebUI: struttura delle richieste uguale a Open WebUI senza proxy, ripresa con e senza chat id, ramo, compattazione salvata e riusata, riavvio del proxy, chat mai viste, disconnessione, tool del client, entrambi i valori di `SEND_CHAT_MARKDOWN`

### M20 · Data di inizio e ID della conversazione nel system prompt
_2026-11-19_

- [x] In coda al system prompt: data e ora di inizio della conversazione, in italiano e con il fuso (`TIMEZONE`, default `Europe/Rome`), con l'invito a verificare data e ora correnti tramite lo strumento apposito
- [x] La data è salvata nel database history (`started_at`) e resta la stessa per tutta la conversazione: il system prompt non cambia e la cache di llama.cpp regge
- [x] I rami mantengono la data di inizio della conversazione da cui nascono
- [x] I database delle versioni precedenti vengono aggiornati automaticamente
- [x] Nel system prompt anche l'ID della conversazione (UUID del database history), per i tool del server MCP che lo richiedono; un ramo ha il suo nuovo ID

### M21 · Variabili fisse caricate dal servizio
_2026-11-19_

- [x] Il servizio systemd della repo carica `/etc/llm-proxy/llm-proxy.env` (`EnvironmentFile=-…`, facoltativo): non serve più il drop-in
- [x] README, `docs/PROXMOX.md` e `.env.example` aggiornati; istruzioni per rimuovere il drop-in creato in precedenza

### M22 · Giri di tool illimitati con protezione dai loop
_2026-11-19_

- [x] `MAX_TOOL_ROUNDS=0` (nuovo default): nessun limite ai giri di tool; un valore positivo resta un tetto, come rete di sicurezza
- [x] `TOOL_REPEAT_LIMIT` (default 3, 0 = disattivata): stessa chiamata (tool e argomenti) con lo stesso risultato per N giri di seguito = loop; il polling con risultati che cambiano non viene interrotto
- [x] Dopo un loop, giro senza tool con una nota per il modello; avviso in chat `Interrotta la ripetizione di "<tool>"` (tolto dalla cronologia) e `WARNING` nel log
- [x] Nel giro senza tool le eventuali tool call del modello (anche scritte come testo) vengono ignorate, così la risposta finisce sempre

### M23 · Variabile per attivare o disattivare il ragionamento
_2026-11-20_

- [x] `ENABLE_THINKING`: vuoto = nessun intervento (default); `false` = ragionamento disattivato in ogni richiesta (`chat_template_kwargs.enable_thinking=false` e `reasoning_effort: "none"`); `true` = attivato (`enable_thinking=true`)
- [x] Con `true`/`false` l'impostazione del proxy prevale su quella del client

### M24 · Annunci naturali dei tool
_2026-11-20_

- [x] Al posto di `Uso "<titolo>"...` frasi naturali pensate per le conversazioni vocali («Cerco tra i contatti...»)
- [x] Nuovo modulo `announcements.py`: frasi generate dal modello (5 per tool) da titolo e descrizione, in background, solo per i tool che non le hanno ancora
- [x] File `tool_announcements.json` accanto al database history (`ANNOUNCEMENTS_FILE`), con le frasi raggruppate per URL del server MCP; modificabile a mano, una voce cancellata viene rigenerata
- [x] Frasi generiche finché quelle del tool non sono pronte o se la generazione non riesce; frasi fisse per il tool di compattazione
- [x] Annuncio solo quando cambia il tool all'interno di una risposta, con frase scelta a caso senza ripetere l'ultima
- [x] Le frasi note vengono tolte dalla cronologia; riconosciuti anche i formati precedenti (`Uso "…"...`)

### M25 · Annunci di qualità migliore
_2026-11-20_

- [x] Analizzati gli annunci generati: verbi coniugati male («Selezione…»), costruzioni pesanti («Vado a…»), riempitivi («…ora») e dettagli troppo specifici
- [x] Generazione con il ragionamento attivo (a prescindere da `ENABLE_THINKING`) e `max_tokens` 2048
- [x] Nuovo prompt: regole su grammatica, prima persona, lunghezza, scopo per l'utente, frasi valide per ogni uso del tool, riempitivi e costruzioni da evitare; esempi buoni e da evitare; rilettura prima di rispondere
- [x] Campo `versione` nelle voci: quelle generate con un prompt precedente vengono rigenerate; `"manuale": true` protegge le voci corrette a mano

### M26 · Generazione degli annunci più rapida e diagnosticabile
_2026-11-20_

- [x] Dal log: circa 2 minuti per tool con il ragionamento attivo e alcune risposte non valide (probabile esaurimento dei 2048 token)
- [x] 5 tool per richiesta, con risposta JSON `{nome tecnico: [frasi]}`: il ragionamento si fa una volta per gruppo
- [x] `max_tokens` 8192; nessuna temperatura forzata, valgono i parametri di campionamento di `llama-server`
- [x] Log con durata, `finish_reason`, lunghezza di testo e ragionamento e, per i tool senza frasi valide, la parte finale del testo

### M27 · Annunci senza ragionamento di default
_2026-11-20_

- [x] Dal log: 922 secondi per un gruppo di 5 tool con il ragionamento (18.824 caratteri di ragionamento), circa 2 ore per 40 tool con llama.cpp occupato
- [x] `ANNOUNCEMENTS_THINKING` (default `false`): generazione senza ragionamento, riattivabile
- [x] Prompt: regola sulle sole azioni che il tool fa davvero e nuovi esempi da evitare («Leggo i dettagli della settimana...», «Confermo il risultato esatto...», «Ottengo il valore corretto...»); versione del prompt invariata, le frasi già generate restano

### M28 · Annunci: revisione e controllo automatico
_2026-11-20_

- [x] Dal confronto: senza ragionamento 51 secondi per gruppo invece di 922, ma alcune frasi violano le regole («Vado a…», «Risolvere…», «Ricerca… in corso»)
- [x] Regole condivise (`RULES`) tra il prompt di generazione e il nuovo prompt di revisione (`REVIEW_PROMPT`), che rilegge e corregge le frasi proposte
- [x] Controllo automatico delle regole verificabili: verbo alla prima persona all'inizio, costruzioni vietate, riempitivi finali, lunghezza; frasi scartate nel log con il motivo
- [x] Nuovo tentativo per i tool con meno di 3 frasi valide; frasi generiche sotto le 2; se la revisione fallisce si usano le frasi generate
- [x] Versione del prompt invariata: le frasi già generate restano

### M29 · Annunci: validazione frase per frase
_2026-11-20_

- [x] Dal log: dopo la revisione restavano frasi con il verbo al passato («Indagavo…»), azioni sbagliate («Cerco uno screenshot…») o vaghe («Applico i numeri…»)
- [x] Terzo passaggio (`VALIDATION_PROMPT`): verdetto motivato su ogni singola frase rispondendo a 5 domande; le frasi non valide o dubbie sono sostituite dalla correzione, le correzioni compaiono nel log
- [x] Se la validazione non risponde per un tool, si usano le frasi della revisione; il controllo automatico resta l'ultimo passaggio

### M30 · Validazione più rapida e meno invasiva
_2026-11-20_

- [x] Dal log: validazione di 274 secondi per gruppo (motivazioni lunghe), correzioni per gusto, frasi rese tutte uguali, errori introdotti («Resolvo»), falso positivo del controllo automatico su «data e ora»
- [x] La validazione elenca solo le frasi da correggere, con un motivo di al massimo 8 parole
- [x] Solo violazioni chiare delle regole, non preferenze di stile; correzioni in italiano corretto e non identiche alle altre frasi del tool (frasi simili vanno bene)
- [x] «ora» tolto dai riempitivi finali del controllo automatico

### M31 · Inoltro puro e log degli scambi
_2026-11-20_

- [x] `ENABLE_PROXY_FEATURES` (default `true`): con `false` `/v1/chat/completions` viene inoltrato a llama.cpp senza modifiche e la risposta torna al client così com'è, anche in streaming; il proxy non si collega al server MCP
- [x] `LOG_EXCHANGES` / `EXCHANGE_LOG`: log JSONL degli scambi (`client_request`, `llama_request`, `llama_response` ricomposta anche in streaming, `client_response`) con `request_id` comune e immagini base64 abbreviate
- [x] Obiettivo: confrontare cosa vede il modello con e senza le funzioni del proxy
- [x] Un file di log per modalità (`exchanges-avanzata.jsonl`, `exchanges-inoltro.jsonl`), per non mescolare i due test

### M32 · Conversazioni senza perdita di messaggi
_2026-11-20_

- [x] Dai log dei due test: anche in modalità avanzata Open WebUI invia 26 tool integrati; il proxy trattava la richiesta come «tool del client» e disattivava la history. Il modello perdeva tool call e risultati dei turni precedenti (codici di conferma, file scaricati, link) e l'ID della conversazione
- [x] Con le funzioni attive i tool inviati dal client vengono sempre scartati (anche un `tool_choice` riferito a uno di essi): il modello vede solo i tool MCP
- [x] Riconoscimento della conversazione riscritto: dai messaggi dell'utente (impronta della sequenza, `user_chain`) invece che dal testo visibile delle risposte; le risposte si prendono sempre dal database
- [x] Con l'id della chat, ripiego sul numero di messaggi utente se il client li ha modificati (es. contesto RAG aggiunto solo nella richiesta corrente), con `WARNING` nel log
- [x] Unica sessione senza memoria: database non disponibile o ultimo messaggio non dell'utente
- [x] Turni interrotti: salvato anche il testo parziale della risposta
- [x] Immagini restituite dai tool passate al modello in un messaggio dopo i risultati (`TOOL_IMAGES`, default `true`)
- [x] Aggiornamento automatico dei database delle versioni precedenti (impronta e numero dei messaggi utente calcolati dai messaggi salvati)

---

## Prossimi passi

- [ ] Test con llama.cpp reale e un modello con tool calling (Qwen 3 / Llama 3.1+)
- [ ] Prima installazione su Proxmox seguendo `docs/PROXMOX.md` e verifica della guida
- [ ] Test automatici nella repo (finto llama-server + server MCP di prova)
- [ ] Supporto a più server MCP contemporaneamente
- [ ] Valutare gli altri endpoint di llama.cpp (`/v1/responses`, `/v1/messages`)
