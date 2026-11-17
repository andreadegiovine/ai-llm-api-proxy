# Esporre il proxy su internet

## Serve davvero nginx?

Tecnicamente il proxy può essere raggiungibile anche direttamente, ma su internet nginx (o un reverse
proxy equivalente) è di fatto necessario:

- **HTTPS.** Il proxy parla solo HTTP. Senza TLS l'API key e tutte le conversazioni viaggiano in chiaro.
  nginx gestisce il certificato Let's Encrypt e il suo rinnovo.
- **Superficie esposta.** nginx pubblica solo `/v1/`. Gli endpoint interni di llama.cpp che il proxy
  inoltra (`/props`, `/slots`, `/metrics`, `/tokenize`...) restano raggiungibili solo dalla LAN.
- **Limiti.** Limite di richieste per IP e dimensione massima delle richieste.

nginx da solo però **non basta**: senza autenticazione chiunque trovi l'indirizzo può usare il tuo
modello e i tuoi tool MCP. Imposta sempre `PROXY_API_KEYS` (vedi sotto).

> **Se lo usi solo tu**, valuta prima una VPN (Tailscale, WireGuard): i client raggiungono il proxy come
> se fossero in LAN e non apri nessuna porta sul router. È l'opzione più sicura.

## Architettura

```
Internet ──443──> router ──> CT nginx (192.168.1.13) ──8081──> CT llm-proxy (192.168.1.12) ──> llama.cpp
                                                                                          └──> server MCP
```

nginx può stare in un container dedicato (consigliato se pubblichi anche altri servizi) oppure nello
stesso container del proxy. In quel caso usa `127.0.0.1:8081` come upstream, `PROXY_HOST=127.0.0.1` e
porta la RAM del container a 1 GB.

## 1. Configurare il proxy

In `/opt/llm-proxy/.env`:

```ini
# una chiave per ogni client/dispositivo: per revocarne una basta toglierla dall'elenco
PROXY_API_KEYS=chiave-telefono,chiave-portatile
# IP di nginx: i log mostreranno l'IP reale dei client
FORWARDED_ALLOW_IPS=192.168.1.13
```

Genera ogni chiave con:

```bash
openssl rand -hex 32
```

Riavvia il proxy:

```bash
systemctl restart llm-proxy
```

I client inseriscono la chiave nel campo **API key** delle impostazioni OpenAI. Le richieste senza
chiave valida ricevono `401`. `/health` resta libero per il monitoraggio.

## 2. Prerequisiti di rete

- **Dominio**: un record DNS `A` (es. `llm.example.com`) che punta al tuo IP pubblico. Se l'IP è
  dinamico, usa un servizio DDNS (DuckDNS, il DDNS del router, Cloudflare...).
- **Router**: inoltra le porte TCP **80** e **443** all'IP del container nginx. La porta 80 serve per
  il rinnovo dei certificati e per il redirect a HTTPS.
- **Non inoltrare mai** le porte 8081 (proxy), 8080 (llama.cpp) o quella del server MCP.
- Se il tuo operatore usa il **CGNAT** (le porte non si possono aprire), usa una VPN oppure
  Cloudflare Tunnel (vedi in fondo).

## 3A. nginx + certbot (configurazione manuale)

Crea il container con il community script Debian, come in [PROXMOX.md](PROXMOX.md)
(1 core, 512 MB di RAM e 4 GB di disco bastano), con IP statico `192.168.1.13`. Poi, nel container:

```bash
apt update && apt install -y nginx certbot
```

Ottieni il certificato. Il sito predefinito di nginx serve già `/var/www/html` sulla porta 80:

```bash
certbot certonly --webroot -w /var/www/html -d llm.example.com --deploy-hook "systemctl reload nginx"
```

Copia la configurazione dalla repo (`deploy/nginx-llm-proxy.conf`) in
`/etc/nginx/sites-available/llm-proxy`. Sostituisci `llm.example.com` e l'IP del proxy, poi abilita il sito:

```bash
ln -s /etc/nginx/sites-available/llm-proxy /etc/nginx/sites-enabled/ && rm /etc/nginx/sites-enabled/default
```

```bash
nginx -t && systemctl reload nginx
```

Il rinnovo dei certificati è automatico (timer `certbot.timer`). Per verificarlo:

```bash
certbot renew --dry-run
```

### Le impostazioni che contano

Se scrivi una configurazione tua, queste sono indispensabili:

| Direttiva | Perché |
|---|---|
| `proxy_buffering off` | senza, nginx accumula la risposta e lo streaming arriva tutto alla fine |
| `proxy_read_timeout 3600s` | il default di 60 s interrompe generazioni lunghe e tool lenti |
| `proxy_http_version 1.1` + `Connection ""` | connessioni persistenti verso il proxy |
| `client_max_body_size 50m` | il default di 1 MB blocca conversazioni lunghe e immagini |
| `X-Forwarded-For` | permette al proxy di loggare l'IP reale (insieme a `FORWARDED_ALLOW_IPS`) |

## 3B. In alternativa: Nginx Proxy Manager

Se preferisci un'interfaccia web, esiste il community script **Nginx Proxy Manager**. Dalla shell del nodo Proxmox:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/nginxproxymanager.sh)"
```

Le impostazioni predefinite dello script sono 2 core, 2 GB di RAM e 8 GB di disco. Poi, nell'interfaccia (porta 81):

1. **Hosts → Proxy Hosts → Add Proxy Host**
   - Domain: `llm.example.com`
   - Scheme `http`, Forward Hostname/IP `192.168.1.12`, Port `8081`
   - Attiva **Block Common Exploits**
2. Scheda **SSL**: richiedi un nuovo certificato Let's Encrypt, attiva **Force SSL** e **HTTP/2**.
3. Scheda **Advanced**, campo *Custom Nginx Configuration*:

   ```nginx
   proxy_buffering off;
   proxy_cache off;
   proxy_request_buffering off;
   proxy_read_timeout 3600s;
   proxy_send_timeout 3600s;
   client_max_body_size 50m;
   ```

Con questa configurazione l'intero proxy viene pubblicato, non solo `/v1/`: le API key restano
obbligatorie.

## 4. Firewall

Con il firewall di Proxmox, sul container **llm-proxy**:

| Direzione | Azione | Sorgente | Porta | Commento |
|---|---|---|---|---|
| IN | ACCEPT | `192.168.1.13` | `8081` | nginx |
| IN | ACCEPT | `192.168.1.0/24` | `8081` | client in LAN (facoltativo) |
| IN | DROP | | `8081` | tutti gli altri |

Su llama.cpp resta valida la regola di [PROXMOX.md](PROXMOX.md#6-proteggere-llamacpp): accesso solo dal proxy.

## 5. Verifica

Da una rete esterna (es. il telefono senza Wi-Fi). Senza chiave deve rispondere `401`:

```bash
curl -i https://llm.example.com/v1/models
```

Con la chiave, in streaming: le parole devono arrivare una alla volta, non tutte insieme alla fine.

```bash
curl -N https://llm.example.com/v1/chat/completions -H 'Content-Type: application/json' -H 'Authorization: Bearer LA_TUA_KEY' -d '{"stream":true,"messages":[{"role":"user","content":"Raccontami una storia breve"}]}'
```

Gli endpoint interni non devono essere raggiungibili (risposta `404`):

```bash
curl -i https://llm.example.com/props
```

## Checklist di sicurezza

- [ ] `PROXY_API_KEYS` impostata, con chiavi lunghe e casuali, una per dispositivo
- [ ] `.env` con permessi `640` (`root:llmproxy`)
- [ ] Sul router inoltrate solo le porte 80 e 443, verso nginx
- [ ] llama.cpp raggiungibile solo dal proxy, proxy raggiungibile solo da nginx e LAN
- [ ] Certificato valido e rinnovo automatico verificato
- [ ] Facoltativo: fail2ban sui `401` nel log di nginx, per bloccare chi prova chiavi a caso
- [ ] Ricorda che i tool MCP agiscono davvero: chi ha una chiave può usarli tutti

## Altre opzioni senza aprire porte

- **Tailscale / WireGuard**: nessuna esposizione pubblica. È l'opzione consigliata per uso personale.
  Con Tailscale installato nel container del proxy, i client usano `http://llm-proxy:8081/v1` dalla tailnet.
- **Cloudflare Tunnel**: funziona anche con il CGNAT e fornisce l'HTTPS. Il traffico però passa da
  Cloudflare, e le richieste non in streaming molto lunghe (molti giri di tool) possono superare il suo
  timeout di circa 100 secondi. Usa lo streaming.
