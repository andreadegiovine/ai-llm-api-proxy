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
- [x] System prompt da file aggiunto a ogni chat; se il client ne ha già uno, i due vengono uniti
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

---

## Prossimi passi

- [ ] Test con llama.cpp reale e un modello con tool calling (Qwen 3 / Llama 3.1+)
- [ ] Prima installazione su Proxmox seguendo `docs/PROXMOX.md` e verifica della guida
- [ ] Test automatici nella repo (finto llama-server + server MCP di prova)
- [ ] Cache dei riassunti persistente su disco, che sopravviva ai riavvii
- [ ] Supporto a più server MCP contemporaneamente
- [ ] Valutare gli altri endpoint di llama.cpp (`/v1/responses`, `/v1/messages`)
