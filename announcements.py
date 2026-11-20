"""
announcements.py - frasi naturali con cui il proxy annuncia in chat l'uso di un tool.

Pensato per le conversazioni vocali: invece di 'Uso "Cerca contatti"...' il proxy dice per esempio
"Cerco tra i contatti...".

- Le frasi di ogni tool vengono generate una sola volta dallo stesso modello (llama.cpp), partendo dal
  titolo e dalla descrizione del tool, e salvate nel file degli annunci (tool_announcements.json).
  Al riavvio, o quando il proxy si ricollega al server MCP, vengono generate solo per i tool che non
  le hanno ancora.
- Nel file le frasi sono raggruppate per URL del server MCP: tool con lo stesso nome su server
  diversi non si sovrascrivono.
- Il file si può modificare a mano (vale dal riavvio successivo); per rigenerare le frasi di un tool
  basta cancellarne la voce. Quando il prompt cambia (PROMPT_VERSION), le voci generate con il prompt
  precedente vengono rigenerate, tranne quelle con "manuale": true.
- Finché le frasi di un tool non sono pronte, o se la generazione non riesce, si usano frasi generiche.
"""

import asyncio
import json
import logging
import os
import random
import re
import time
from pathlib import Path

import httpx

log = logging.getLogger("announcements")

GENERIC_PHRASES = ["Un attimo...", "Ci lavoro...", "Verifico subito...", "Controllo...", "Un momento..."]
COMPACT_PHRASES = ["Riordino la memoria della conversazione...", "Faccio un po' di spazio in memoria...",
                   "Riassumo quello che ci siamo detti..."]

# Versione del prompt: le voci generate con una versione precedente vengono rigenerate
# (tranne quelle con "manuale": true, corrette a mano).
PROMPT_VERSION = 2

GENERATION_PROMPT = """Sei l'autore delle frasi di un assistente vocale italiano. Quando l'assistente usa uno \
strumento, mentre aspetta il risultato pronuncia una frase breve per far sapere all'utente cosa sta facendo.
Scrivi 5 frasi per lo strumento descritto sotto.

Come devono essere:
- italiano corretto e naturale, come lo direbbe una persona che ti sta aiutando;
- iniziano con un verbo alla prima persona singolare del presente indicativo ("Cerco", "Controllo", \
"Scarico", "Preparo"): controlla la coniugazione ("Seleziono", non "Selezione");
- da 2 a 6 parole, terminate da "...";
- descrivono lo scopo dell'azione per l'utente, non i passaggi tecnici;
- vanno bene per qualsiasi uso dello strumento: nessun dettaglio che lo strumento non fa sempre \
(non "il nome" se può cercare anche per email o telefono, non "i dati" se scarica qualsiasi file);
- sono diverse tra loro, con verbi o costruzioni diverse.

Da evitare:
- riempitivi come "ora", "adesso", "subito", "un attimo che";
- costruzioni come "Vado a...", "Sto per...", "Procedo a...";
- nomi tecnici, il nome dello strumento, virgolette, emoji.

Esempi buoni:
- strumento che cerca immagini sul web: "Cerco qualche immagine...", "Guardo cosa trovo online..."
- strumento che legge il calendario: "Controllo il calendario...", "Guardo i tuoi impegni..."
Esempi da evitare:
- "Selezione le migliori anteprime..." (verbo coniugato male, troppo tecnico)
- "Vado a cercare il nome..." (costruzione pesante, troppo specifico)
- "Scarico i dati ora..." (riempitivo, troppo specifico)

Prima di rispondere ragiona su cosa fa lo strumento dal punto di vista dell'utente, scrivi le frasi e \
rileggile controllando ogni regola. Rispondi solo con un array JSON di 5 stringhe."""


def _clean_phrase(text) -> str | None:
    if not isinstance(text, str):
        return None
    text = text.strip().strip('"').strip()
    text = re.sub(r"[.!?…:;,\s]+$", "", text)
    if not text or len(text) > 80 or "\n" in text:
        return None
    return text[0].upper() + text[1:] + "..."


def parse_phrases(output: str) -> list[str]:
    """Estrae le frasi dall'output del modello (array JSON, eventualmente con testo intorno)."""
    output = re.sub(r"<think>.*?</think>", "", output or "", flags=re.S)
    m = re.search(r"\[.*\]", output, re.S)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    phrases = []
    for item in items if isinstance(items, list) else []:
        p = _clean_phrase(item)
        if p and p not in phrases:
            phrases.append(p)
    return phrases[:6]


class Announcer:
    def __init__(self, path: str, http: httpx.AsyncClient):
        self.path = Path(path)
        self.http = http
        self.data: dict[str, dict[str, dict]] = {}  # url del server MCP -> nome del tool -> voce
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            n = sum(len(v) for v in self.data.values())
            log.info("annunci dei tool: %s (%d tool)", self.path, n)
        except FileNotFoundError:
            log.info("annunci dei tool: %s (nuovo file)", self.path)
        except (OSError, ValueError) as e:
            log.error("file degli annunci %s non leggibile (%s): uso le frasi generiche", self.path, e)

    # ------------------------------------------------------------------ frasi
    def phrases(self, server_url: str, tool_name: str) -> list[str]:
        entry = self.data.get(server_url, {}).get(tool_name) or {}
        return entry.get("frasi") or GENERIC_PHRASES

    def known_phrases(self) -> set[str]:
        """Tutte le frasi che il proxy può aver scritto in chat, per toglierle dalla cronologia."""
        known = set(GENERIC_PHRASES) | set(COMPACT_PHRASES)
        for tools in self.data.values():
            for entry in tools.values():
                known.update(entry.get("frasi") or [])
        return known

    # ------------------------------------------------------------------ generazione
    def schedule(self, server_url: str, tools: list[dict]):
        """Avvia in background la generazione per i tool che non hanno ancora frasi.
        tools: [{"name", "title", "description"}]"""
        missing = [t for t in tools if self._needs_generation(self.data.get(server_url, {}).get(t["name"]))]
        if not missing:
            return
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._generate_all(server_url, missing))

    @staticmethod
    def _needs_generation(entry: dict | None) -> bool:
        """Da generare: voce assente, senza frasi o creata con un prompt precedente (se non è manuale)."""
        if not entry or not entry.get("frasi"):
            return True
        return not entry.get("manuale") and entry.get("versione", 1) < PROMPT_VERSION

    async def _generate_all(self, server_url: str, tools: list[dict]):
        log.info("annunci: genero le frasi per %d tool di %s", len(tools), server_url)
        for t in tools:
            try:
                phrases = await self._generate(t)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001  si riproverà alla prossima connessione al server MCP
                log.warning("annunci: generazione non riuscita per %s (%s)", t["name"], e)
                continue
            if len(phrases) < 2:
                log.warning("annunci: risposta del modello non valida per %s, uso le frasi generiche", t["name"])
                continue
            async with self._lock:
                self.data.setdefault(server_url, {})[t["name"]] = {
                    "titolo": t.get("title") or t["name"], "frasi": phrases, "versione": PROMPT_VERSION,
                    "generato": time.strftime("%Y-%m-%d %H:%M:%S")}
                self._save()
            log.info("annunci: %s -> %s", t["name"], phrases)

    async def _generate(self, tool: dict) -> list[str]:
        user = (f"Strumento: {tool.get('title') or tool['name']}\nNome tecnico: {tool['name']}\n"
                f"Descrizione: {(tool.get('description') or '')[:1500]}")
        payload = {
            "messages": [{"role": "system", "content": GENERATION_PROMPT}, {"role": "user", "content": user}],
            # ragionamento attivo (a prescindere da ENABLE_THINKING): frasi più curate; serve più spazio per i token
            "max_tokens": 2048, "temperature": 0.7, "stream": False,
            "chat_template_kwargs": {"enable_thinking": True},
        }
        r = await self.http.post("/v1/chat/completions", json=payload)
        r.raise_for_status()
        return parse_phrases(r.json()["choices"][0]["message"].get("content") or "")

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.error("annunci: impossibile salvare %s (%s): le frasi restano solo in memoria", self.path, e)


class AnnounceTracker:
    """Stato degli annunci in una risposta: si annuncia solo quando cambia il tool, e la frase scelta
    evita di ripetere l'ultima usata per lo stesso tool."""

    def __init__(self, announcer: Announcer, server_url: str, compact_tool_name: str):
        self.announcer, self.server_url, self.compact_tool_name = announcer, server_url, compact_tool_name
        self.last_tool: str | None = None
        self.last_phrase: dict[str, str] = {}

    def text(self, calls: list[dict]) -> str | None:
        lines = []
        for name in dict.fromkeys(c["function"]["name"] for c in calls):  # tool distinti, in ordine
            if name == self.last_tool:
                continue
            options = (COMPACT_PHRASES if name == self.compact_tool_name
                       else self.announcer.phrases(self.server_url, name))
            choices = [p for p in options if p != self.last_phrase.get(name)] or options
            phrase = random.choice(choices)
            self.last_phrase[name] = phrase
            self.last_tool = name
            lines.append(phrase)
        return "\n".join(lines) or None
