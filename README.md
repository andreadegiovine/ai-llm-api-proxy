# ai-llm-api-proxy

Proxy OpenAI-compatibile da mettere davanti a **llama.cpp** (`llama-server`). Qualunque client
lo chiami (Open WebUI, app, script), il proxy:

- **aggiunge il system prompt** a ogni chat (se il client ne ha già uno, li unisce);
- **espone i tool di un server MCP** e li **esegue lato proxy**, ripetendo i giri di tool finché
  il modello dà la risposta finale;
- fa **streaming reale** token per token, anche tra un giro di tool e l'altro;
- scrive in chat **«🔧 Uso nome_strumento...»** prima di eseguire un tool;
- **compatta la memoria** quando il prompt si avvicina al contesto massimo, facendo riassumere
  i messaggi più vecchi allo stesso modello (`compact_tool.py`). Espone anche il tool
  `compact_memory`, che il modello può chiamare di sua iniziativa.

```
Client  →  proxy :8081  →  llama.cpp :8080
              │
              └──→  server MCP (streamable HTTP o SSE)
```

## Requisiti

- `llama-server` avviato con `--jinja` e un modello che supporti il tool calling
  (es. Qwen 2.5/3, Llama 3.1+, Mistral).
- Python 3.10+.
- Server MCP raggiungibile via HTTP (streamable HTTP o SSE). Per un server solo stdio,
  mettici davanti un bridge come `mcp-proxy`.

## Installazione (es. container LXC Debian su Proxmox)

```bash
apt install -y python3-venv git
git clone https://github.com/andreadegiovine/ai-llm-api-proxy /opt/llm-proxy
python3 -m venv /opt/llm-proxy/venv
/opt/llm-proxy/venv/bin/pip install -r /opt/llm-proxy/requirements.txt
```

Modifica `system_prompt.txt` e gli indirizzi in `llm-proxy.service`, poi:

```bash
cp /opt/llm-proxy/llm-proxy.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now llm-proxy
journalctl -u llm-proxy -f
```

Nei client usa `http://IP_PROXY:8081/v1` come endpoint OpenAI. Conviene limitare la porta
di llama.cpp al solo IP del proxy, così nessun client lo aggira.

## Configurazione (variabili d'ambiente)

| Variabile | Default | Descrizione |
|---|---|---|
| `LLAMA_URL` | `http://192.168.1.10:8080` | indirizzo di llama-server |
| `LLAMA_API_KEY` | | chiave se llama-server usa `--api-key` |
| `MCP_URL` | `http://192.168.1.11:8000/mcp` | endpoint MCP (vuoto = nessun MCP) |
| `MCP_TRANSPORT` | `http` | `http` (streamable) oppure `sse` |
| `SYSTEM_PROMPT_FILE` | `/opt/llm-proxy/system_prompt.txt` | file del system prompt |
| `MAX_TOOL_ROUNDS` | `8` | giri massimi di tool per risposta |
| `TOOL_STATUS` | `content` | messaggi «🔧 Uso …»: `content`, `reasoning` oppure `off` |
| `AUTO_COMPACT` | `1` | compattazione automatica della memoria |
| `ENABLE_COMPACT_TOOL` | `1` | espone il tool `compact_memory` al modello |
| `COMPACT_THRESHOLD` | `0.80` | compatta oltre questa frazione di `n_ctx` |
| `COMPACT_TARGET` | `0.50` | dopo la compattazione resta sotto questa frazione |
| `COMPACT_SUMMARY_TOKENS` | `1024` | lunghezza massima del riassunto |
| `COMPACT_RESERVE_TOKENS` | `2048` | token riservati alla risposta |
| `MAX_TOOL_RESULT_RATIO` | `0.25` | taglia i risultati dei tool oltre questa frazione di `n_ctx` |
| `DEFAULT_N_CTX` | `8192` | usato solo se llama.cpp non comunica `n_ctx` |
| `N_CTX_TTL` | `60` | secondi di cache di `n_ctx` |

## Come funziona la compattazione

- `n_ctx` viene letto da llama.cpp (`GET /props`, fallback `GET /slots`) e aggiornato ogni
  `N_CTX_TTL` secondi: se cambi modello o contesto non serve riavviare il proxy.
- I token vengono contati con `/v1/chat/completions/input_tokens`; sulle versioni che non
  lo hanno si usa `/apply-template` + `/tokenize`, e in ultima istanza una stima.
- I messaggi recenti restano intatti, quelli vecchi diventano un riassunto aggiunto al system
  prompt. Le conversazioni molto lunghe vengono riassunte a blocchi.
- I riassunti sono tenuti in cache (in memoria) per prefisso di conversazione: il client
  rimanda tutta la cronologia a ogni turno, ma il riassunto viene calcolato una volta sola.

## Limiti

- Viene gestito solo `/v1/chat/completions`. Gli altri endpoint passano invariati, senza
  system prompt né tool.
- Il client vede solo il testo finale, non le chiamate ai tool: al turno successivo il modello
  non ha più l'output grezzo dei tool usati in precedenza.
- La cache dei riassunti si svuota al riavvio del proxy.
