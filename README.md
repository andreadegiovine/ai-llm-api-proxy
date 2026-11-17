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
- I riassunti sono tenuti in cache (in memoria) per prefisso di conversazione: il client
  rimanda tutta la cronologia a ogni turno, ma il riassunto viene calcolato una volta sola.

## Limiti

- Viene gestito solo `/v1/chat/completions`. Gli altri endpoint passano invariati, senza
  system prompt né tool.
- Il client vede solo il testo finale, non le chiamate ai tool: al turno successivo il modello
  non ha più l'output grezzo dei tool usati in precedenza.
- La cache dei riassunti si svuota al riavvio del proxy.
