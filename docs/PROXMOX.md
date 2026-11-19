# Installazione su Proxmox

Il proxy gira in un container LXC dedicato, accanto a quelli di llama.cpp e del server MCP.

```
LAN
 ├─ CT llama.cpp      192.168.1.10:8080
 ├─ CT server MCP     192.168.1.11:8000
 └─ CT llm-proxy      192.168.1.12:8081   ← questo progetto
```

Gli IP sono esempi: usa quelli della tua rete, in tutta la guida e nel file `.env`.

## 1. Risorse da allocare

Il proxy non fa inferenza: inoltra le richieste a llama.cpp e chiama il server MCP. Il lavoro pesante
resta nel container di llama.cpp, quindi bastano poche risorse.

| Risorsa | Minimo | Consigliato | Note |
|---|---|---|---|
| CPU | 1 core | 1–2 core | il proxy usa pochissima CPU, anche con lo streaming |
| RAM | 512 MB | 1 GB | Python + dipendenze occupano circa 100–150 MB; 1 GB lascia spazio se aggiungi nginx nello stesso container |
| Swap | 512 MB | 512 MB | |
| Disco | 4 GB | 4–8 GB | Debian circa 1 GB, virtualenv circa 150 MB, il resto per log e aggiornamenti |
| Rete | IP statico | IP statico | i client e nginx devono trovarlo sempre allo stesso indirizzo |

Container **unprivileged**: non servono privilegi né accesso alla GPU.

## 2. Creare il container con un community script

Usa lo script **Debian LXC** dei [Proxmox VE Community Scripts](https://community-scripts.github.io/ProxmoxVE/).
Dalla shell del **nodo Proxmox** (non dentro un container), lancia:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/debian.sh)"
```

Le impostazioni predefinite dello script sono Debian 13, unprivileged, 1 core, 512 MB di RAM e 2 GB di disco.
Il disco è poco: scegli la modalità **Advanced** e imposta:

- **Hostname:** `llm-proxy`
- **Disk size:** `4` GB
- **CPU cores:** `1`
- **RAM:** `512` MB (oppure `1024` se metterai nginx nello stesso container)
- **IP:** statico, es. `192.168.1.12/24`, con il gateway della tua rete
- **Container type:** unprivileged

Come per ogni script scaricato da internet, se vuoi leggilo prima di eseguirlo.

<details>
<summary>In alternativa, senza community script</summary>

Dalla shell del nodo scarica il template Debian e crea il container (adatta ID, storage, bridge e IP):

```bash
pveam update && pveam available --section system | grep debian-13
pveam download local debian-13-standard_13.1-2_amd64.tar.zst
pct create 120 local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst \
  --hostname llm-proxy --unprivileged 1 --cores 1 --memory 512 --swap 512 \
  --rootfs local-lvm:4 --net0 name=eth0,bridge=vmbr0,ip=192.168.1.12/24,gw=192.168.1.1 \
  --features nesting=1 --onboot 1 --start 1
```

Il nome esatto del template cambia nel tempo: usa quello restituito da `pveam available`.
</details>

## 3. Installare il proxy

Entra nel container (`pct enter <ID>` dal nodo, oppure dalla console dell'interfaccia web) ed esegui:

```bash
apt update && apt install -y git python3 python3-venv
```

```bash
git clone https://github.com/andreadegiovine/ai-llm-api-proxy /opt/llm-proxy
```

```bash
python3 -m venv /opt/llm-proxy/venv && /opt/llm-proxy/venv/bin/pip install -r /opt/llm-proxy/requirements.txt
```

Crea un utente di sistema dedicato, senza shell né privilegi. Il servizio gira con questo utente,
mentre il codice resta di proprietà di root: il processo può leggerlo ma non modificarlo.

```bash
useradd --system --home-dir /opt/llm-proxy --shell /usr/sbin/nologin llmproxy
```

## 4. Configurare prima del primo avvio

Copia i file di esempio:

```bash
cd /opt/llm-proxy && cp .env.example .env && cp system_prompt.example.txt system_prompt.txt
```

Modifica `.env`: almeno `LLAMA_URL`, `MCP_URL` e `MCP_TRANSPORT`. Se esporrai il proxy su internet,
imposta anche `PROXY_API_KEYS` (vedi [NGINX.md](NGINX.md)). Tutte le variabili sono descritte nel file.

In alternativa, consigliata, metti le variabili personalizzate in `/etc/llm-proxy/llm-proxy.env`, che il
servizio carica e che gli aggiornamenti non toccano: vedi
[Variabili fisse nel servizio systemd](../README.md#variabili-fisse-nel-servizio-systemd) nel README.

```bash
nano /opt/llm-proxy/.env
```

Scrivi il tuo system prompt:

```bash
nano /opt/llm-proxy/system_prompt.txt
```

Il file `.env` contiene le chiavi: rendilo leggibile solo a root e all'utente del servizio.

```bash
chown root:llmproxy /opt/llm-proxy/.env && chmod 640 /opt/llm-proxy/.env
```

`.env` e `system_prompt.txt` sono esclusi da git: gli aggiornamenti non li toccano.

## 5. Avviare il servizio

```bash
cp /opt/llm-proxy/deploy/llm-proxy.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now llm-proxy
```

Nel log devono comparire la connessione al server MCP e l'elenco dei tool:

```bash
journalctl -u llm-proxy -f
```

Prova una richiesta da un'altra macchina della LAN (togli l'header `Authorization` se non hai impostato `PROXY_API_KEYS`):

```bash
curl http://192.168.1.12:8081/v1/chat/completions -H 'Content-Type: application/json' -H 'Authorization: Bearer LA_TUA_KEY' -d '{"messages":[{"role":"user","content":"Che tool hai a disposizione?"}]}'
```

Nei client usa `http://192.168.1.12:8081/v1` come endpoint OpenAI.

Il database delle conversazioni (`history.db`) viene creato da systemd in `/var/lib/llm-proxy`,
l'unica cartella scrivibile dall'utente del servizio. Il codice in `/opt/llm-proxy` resta di root.

## 6. Proteggere llama.cpp

I client devono passare dal proxy, non parlare direttamente con llama.cpp. Con il **firewall di Proxmox**,
sul container di llama.cpp (*Firewall → Add*, poi abilita il firewall del container nelle *Options*):

| Direzione | Azione | Sorgente | Porta | Commento |
|---|---|---|---|---|
| IN | ACCEPT | `192.168.1.12` | `8080` | solo il proxy |
| IN | DROP | | `8080` | tutti gli altri |

Lo stesso vale per la porta del server MCP, se non deve essere usata da altri client.

## Risoluzione problemi

Per vedere lo stato e le ultime righe del log:

```bash
systemctl status llm-proxy --no-pager && journalctl -u llm-proxy -n 50 --no-pager
```

| Errore nel log | Causa | Soluzione |
|---|---|---|
| `status=217/USER`, `Failed to determine user credentials` | manca l'utente `llmproxy` (passo 3 saltato o fallito) | crea l'utente e ripeti il `chown` di `.env` (passi 3 e 4), poi `systemctl reset-failed llm-proxy && systemctl restart llm-proxy` |
| `chown: invalid group: 'root:llmproxy'` | stesso motivo: l'utente non esiste ancora | crea prima l'utente (passo 3) |
| `PermissionError` su `.env` | `.env` non leggibile dal servizio | `chown root:llmproxy /opt/llm-proxy/.env && chmod 640 /opt/llm-proxy/.env` |
| `status=203/EXEC` | virtualenv mancante o creato in un'altra cartella | ripeti la creazione del virtualenv (passo 3) |
| `MCP non raggiungibile ... riprovo tra 5s` | `MCP_URL` o `MCP_TRANSPORT` errati, oppure server MCP spento | controlla `.env`; il proxy si riconnette da solo appena il server risponde |
| `llama.cpp non raggiungibile` nelle risposte | `LLAMA_URL` errato o firewall | prova `curl http://IP_LLAMA:8080/health` dal container del proxy |

Dopo aver corretto la causa, se systemd ha smesso di riprovare, azzera il contatore con `systemctl reset-failed llm-proxy`.

## Aggiornare

I passaggi completi, compresi il controllo delle nuove variabili e la reinstallazione del servizio,
sono nel [README](../README.md#aggiornamento). Per un aggiornamento di routine, da root nel container:

```bash
cd /opt/llm-proxy && git pull && venv/bin/pip install -r requirements.txt && systemctl restart llm-proxy
```

Dopo un aggiornamento, confronta `.env.example` con il tuo `.env` per vedere se sono state aggiunte nuove variabili:

```bash
diff <(grep -o '^[A-Z_]*=' /opt/llm-proxy/.env.example) <(grep -o '^[A-Z_]*=' /opt/llm-proxy/.env)
```
