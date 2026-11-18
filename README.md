# ai-llm-api-proxy

Proxy OpenAI-compatibile da mettere davanti a **llama.cpp** (`llama-server`). Qualunque client
lo chiami (Open WebUI, app, script), il proxy:

- **aggiunge il system prompt** a ogni chat (se il client ne ha già uno, li unisce);
- **espone i tool di un server MCP** e li **esegue lato proxy**, ripetendo i giri di tool finché
  il modello dà la risposta finale;
- fa **streaming reale** token per token, anche tra un giro di tool e l'altro;
- inoltra le **statistiche di llama.cpp** (`usage`, `timings`: token, tempi, velocità,
  `prompt_progress`), sommate su tutti i giri di tool della risposta;
- mostra l'uso dei tool in chat: una riga **«Uso "Data e ora corrente"...»** (titolo del tool MCP)
  oppure, per Open WebUI, il **box nativo dei tool** con argomenti in ingresso e risultato in uscita;
- **compatta la memoria** quando il prompt si avvicina al contesto massimo, facendo riassumere
  i messaggi più vecchi allo stesso modello (`compact_tool.py`). Espone anche il tool
  `compact_memory`, che il modello può chiamare di sua iniziativa;
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

Se `git pull` si rifiuta di procedere perché ci sono modifiche locali a file della repo, guarda
quali sono con `git status`. Per scartarle e tornare alla versione della repo usa
`git checkout -- <file>`. La configurazione va solo in `.env` e `system_prompt.txt`.

## Configurazione

Tutta la configurazione è nel file `.env` della cartella del proxy. Ogni variabile è descritta in
[`.env.example`](.env.example). `.env` e `system_prompt.txt` sono esclusi da git, quindi un `git pull`
non sovrascrive la configurazione locale.

| File | Ruolo |
|---|---|
| `proxy.py` | proxy, streaming, tool MCP, API key |
| `compact_tool.py` | compattazione della memoria e tool `compact_memory` |
| `.env.example` | modello della configurazione, da copiare in `.env` |
| `system_prompt.example.txt` | modello del system prompt, da copiare in `system_prompt.txt` |
| `deploy/llm-proxy.service` | servizio systemd |
| `deploy/nginx-llm-proxy.conf` | configurazione nginx per l'esposizione su internet |

## Come funziona la compattazione

- `n_ctx` viene letto da llama.cpp (`GET /props`, fallback `GET /slots`) e aggiornato ogni
  `N_CTX_TTL` secondi: se cambi modello o contesto non serve riavviare il proxy.
- I token vengono contati con `/v1/chat/completions/input_tokens`; sulle versioni che non
  lo hanno si usa `/apply-template` + `/tokenize`, e in ultima istanza una stima.
- I messaggi recenti restano intatti, quelli vecchi diventano un riassunto aggiunto al system
  prompt. Le conversazioni molto lunghe vengono riassunte a blocchi.
- I ragionamenti non entrano nei riassunti: si riassumono solo il testo dei messaggi e le chiamate
  ai tool, e i blocchi di ragionamento rimandati dai client vengono tolti prima.
- I riassunti sono tenuti in cache (in memoria) per prefisso di conversazione: il client
  rimanda tutta la cronologia a ogni turno, ma il riassunto viene calcolato una volta sola.

## Configurazione consigliata per Open WebUI

```ini
TOOL_STATUS=openwebui
REASONING_FORMAT=think
```

- `TOOL_STATUS=openwebui`: dopo ogni tool il proxy inserisce nel testo un blocco
  `<details type="tool_calls">`, che Open WebUI mostra con il suo box nativo dei tool: nome
  (il `title` del tool MCP), argomenti in ingresso e risultato in uscita. I tool che falliscono
  sono segnati come errore. Il box compare quando il tool ha finito: mentre lavora non si vede nulla.
- `REASONING_FORMAT=think`: ogni ragionamento compare come blocco separato, nel punto in cui
  avviene (vedi sotto).

Il risultato è: ragionamento → box del tool → ragionamento → risposta. I box e i ragionamenti
non tornano al modello nei turni successivi: Open WebUI non li rimanda e, se un client lo fa,
il proxy li toglie dalla cronologia.

Valori di `TOOL_STATUS`: `content` (riga `Uso "…"...` nel testo, default), `reasoning` (la stessa
riga nel ragionamento), `openwebui` (box nativo), `off`.

## Ragionamenti (modelli "thinking")

Con più giri di tool il modello ragiona più volte nella stessa risposta. `REASONING_FORMAT` decide
come i ragionamenti arrivano al client:

- `native` (default): campo `reasoning_content`, come fa llama.cpp. È lo standard, ma alcuni client
  (es. **Open WebUI**) uniscono tutti i ragionamenti nel primo blocco in cima alla risposta;
- `think`: ogni ragionamento diventa un blocco `<think>...</think>` dentro il testo, nel punto in cui
  avviene. **Consigliato per Open WebUI**, che mostra un blocco di ragionamento sotto ogni
  `Uso "<tool>"...`. I client che non riconoscono i tag mostrerebbero `<think>` come testo.

In entrambi i casi i ragionamenti non tornano al modello come testo: se un client li rimanda nella
cronologia dentro il contenuto (`<think>…</think>` o `<details type="reasoning">`), il proxy li rimuove.

## Statistiche (token e velocità)

Il proxy restituisce i campi `usage` e `timings` di llama.cpp. In streaming si trovano nell'ultimo chunk, insieme a
`finish_reason`, più il chunk standard con `choices: []` se il client chiede
`stream_options.include_usage`. Quando una risposta richiede più giri di tool:

- `completion_tokens`, `prompt_n`, `predicted_n` e i tempi sono la **somma** di tutti i giri;
  le velocità (`*_per_second`, `*_per_token_ms`) sono ricalcolate sui totali;
- `prompt_tokens` è il contesto dell'ultima richiesta a llama.cpp, e `cache_n` è calcolato in modo
  che `cache_n + prompt_n + predicted_n` resti il contesto reale occupato;
- il tempo di esecuzione dei tool MCP non è incluso.

## Limiti

- Viene gestito solo `/v1/chat/completions`. Gli altri endpoint passano invariati, senza
  system prompt né tool.
- Il client vede solo il testo finale, non le chiamate ai tool: al turno successivo il modello
  non ha più l'output grezzo dei tool usati in precedenza.
- La cache dei riassunti si svuota al riavvio del proxy.
