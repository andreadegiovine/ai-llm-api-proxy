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

BATCH_SIZE = 5  # tool per richiesta
MAX_TOKENS = 8192  # spazio per l'eventuale ragionamento e per la risposta
# Ragionamento durante la generazione: frasi un po' più curate, ma molto più lento (anche 15 minuti per
# gruppo di 5 tool, con llama.cpp occupato). Di default disattivato: il prompt contiene già regole ed esempi.
THINKING = os.getenv("ANNOUNCEMENTS_THINKING", "false").strip().lower() in ("1", "true", "yes", "on")

GENERIC_PHRASES = ["Un attimo...", "Ci lavoro...", "Verifico subito...", "Controllo...", "Un momento..."]
COMPACT_PHRASES = ["Riordino la memoria della conversazione...", "Faccio un po' di spazio in memoria...",
                   "Riassumo quello che ci siamo detti..."]

# Versione del prompt: le voci generate con una versione precedente vengono rigenerate
# (tranne quelle con "manuale": true, corrette a mano).
PROMPT_VERSION = 2

INTRO = """Quando l'assistente usa uno strumento, mentre aspetta il risultato pronuncia una frase breve \
per far sapere all'utente cosa sta facendo."""

RULES = """Come devono essere le frasi:
- italiano corretto e naturale, come lo direbbe una persona che ti sta aiutando;
- iniziano con un verbo alla prima persona singolare del presente indicativo ("Cerco", "Controllo", \
"Scarico", "Preparo"): controlla la coniugazione ("Seleziono", non "Selezione");
- da 2 a 6 parole, terminate da "...";
- descrivono lo scopo dell'azione per l'utente, non i passaggi tecnici;
- descrivono solo azioni che lo strumento fa davvero: niente verbi che non indicano un'azione concreta \
("Confermo", "Ottengo", "Aggiorno" se lo strumento non conferma, non ottiene o non aggiorna nulla);
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
- "Risolvere l'espressione..." (verbo all'infinito)
- "Ricerca internet in corso..." (non inizia con un verbo)
- "Leggo i dettagli della settimana..." per uno strumento che dice data e ora (dettaglio che non fa)
- "Confermo il risultato esatto..." per una calcolatrice (non descrive un'azione concreta)
- "Ottengo il valore corretto..." (vago, non descrive cosa sta facendo)"""

JSON_ANSWER = """Rispondi solo con un oggetto JSON: per ogni strumento il suo nome tecnico come chiave e un array \
di 5 stringhe come valore."""

GENERATION_PROMPT = f"""Sei l'autore delle frasi di un assistente vocale italiano. {INTRO}
Scrivi 5 frasi per ciascuno degli strumenti descritti sotto.

{RULES}

Per ogni strumento pensa a cosa fa dal punto di vista dell'utente, scrivi le frasi e rileggile \
controllando ogni regola. {JSON_ANSWER}"""

REVIEW_PROMPT = f"""Sei il revisore delle frasi di un assistente vocale italiano. {INTRO}
Ricevi la descrizione di alcuni strumenti e le frasi proposte per ciascuno. Rileggile una per una: correggi \
o sostituisci ogni frase che non rispetta le regole e lascia invariate quelle corrette. Per ogni strumento \
restituisci 5 frasi.

{RULES}

Controlla in particolare che ogni frase:
- inizi con un verbo alla prima persona singolare del presente ("Cerco", non "Cercare" né "Ricerca... in corso");
- non inizi con "Vado a", "Sto per", "Procedo a";
- non finisca con riempitivi come "ora", "adesso", "subito";
- descriva un'azione che lo strumento fa davvero e che vale per ogni suo uso;
- non sia vaga o innaturale.

{JSON_ANSWER}"""

VALIDATION_PROMPT = f"""Sei il controllore finale delle frasi di un assistente vocale italiano. {INTRO}
Ricevi la descrizione di alcuni strumenti e le frasi scelte per ciascuno. Valuta ogni frase singolarmente, \
rispondendo per ognuna a queste domande:
1. Inizia con un verbo alla prima persona singolare del presente indicativo? (non al passato o all'imperfetto \
come "Indagavo", non all'infinito come "Risolvere", non un nome come "Ricerca")
2. Descrive un'azione che lo strumento fa davvero, secondo la sua descrizione? (uno strumento che cattura uno \
screenshot non "cerca" e non "scarica" un'immagine)
3. Vale per qualsiasi uso dello strumento, senza dettagli che lo strumento non fa sempre?
4. È naturale e chiara, come la direbbe una persona? (non vaga come "Applico i numeri..." o "Ottengo il valore...")
5. È priva di riempitivi ("ora", "adesso", "subito") e di costruzioni come "Vado a...", "Sto per...", "Procedo a..."?
Una frase è valida solo se la risposta è sì a tutte le domande. Se non è valida, o se hai anche solo un dubbio, \
scrivi una correzione che rispetti tutte le regole.

{RULES}

Rispondi solo con un oggetto JSON: per ogni strumento il suo nome tecnico come chiave e come valore un array con \
un oggetto per ogni frase: {{"frase": "...", "valida": true o false, "motivo": "...", "correzione": "..."}} \
("correzione" vuota se la frase è valida e non hai dubbi)."""

# Controllo automatico delle regole verificabili nel codice: l'ultima rete dopo generazione, revisione e validazione
FORBIDDEN_STARTS = ("vado a ", "sto per ", "procedo a ", "procedo ad ", "procedo con ")
FILLER_ENDINGS = ("ora", "adesso", "subito", "un attimo", "un momento")
MAX_WORDS = 7
MIN_GOOD = 3  # sotto questa soglia il tool viene rigenerato una volta


def phrase_problem(phrase: str) -> str | None:
    """Motivo per cui la frase viola una regola verificabile, oppure None."""
    text = phrase.rstrip(".…").strip()
    low = text.lower()
    first = re.split(r"[\s'’]", low, maxsplit=1)[0]
    if not first.endswith("o"):
        return "non inizia con un verbo alla prima persona"
    if (low + " ").startswith(FORBIDDEN_STARTS):
        return "costruzione da evitare"
    if any(low.endswith(" " + f) for f in FILLER_ENDINGS):
        return "riempitivo finale"
    if len(text.split()) > MAX_WORDS:
        return "troppo lunga"
    return None


def valid_phrases(phrases: list[str]) -> tuple[list[str], list[str]]:
    """Divide le frasi in (valide, scartate con il motivo)."""
    good, bad = [], []
    for p in phrases:
        problem = phrase_problem(p)
        (bad if problem else good).append(f"{p} ({problem})" if problem else p)
    return good, bad


def _clean_phrase(text) -> str | None:
    if not isinstance(text, str):
        return None
    text = text.strip().strip('"').strip()
    text = re.sub(r"[.!?…:;,\s]+$", "", text)
    if not text or len(text) > 80 or "\n" in text:
        return None
    return text[0].upper() + text[1:] + "..."


def clean_phrases(items) -> list[str]:
    phrases = []
    for item in items if isinstance(items, list) else []:
        p = _clean_phrase(item)
        if p and p not in phrases:
            phrases.append(p)
    return phrases[:6]


def _json_object(output: str):
    output = re.sub(r"<think>.*?</think>", "", output or "", flags=re.S)
    m = re.search(r"\{.*\}", output, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def parse_validation(output: str) -> dict[str, list[dict]]:
    """Verdetti della validazione: {nome tecnico: [{"frase", "valida", "motivo", "correzione"}]}."""
    data = _json_object(output)
    if not isinstance(data, dict):
        return {}
    out = {}
    for name, items in data.items():
        if isinstance(items, list):
            out[str(name)] = [i for i in items if isinstance(i, dict) and isinstance(i.get("frase"), str)]
    return out


def apply_validation(items: list[dict]) -> tuple[list[str], list[str]]:
    """Frasi dopo la validazione: le valide restano, le altre sono sostituite dalla correzione.
    Restituisce (frasi, descrizione delle correzioni per il log)."""
    phrases, changes = [], []
    for item in items:
        valid = item.get("valida") is True or str(item.get("valida")).lower() == "true"
        correction = item.get("correzione") if isinstance(item.get("correzione"), str) else ""
        if valid and not correction.strip():
            phrases.append(item["frase"])
        elif correction.strip():
            phrases.append(correction)
            changes.append(f"{item['frase']!r} -> {correction!r} ({item.get('motivo') or 'dubbio'})")
        else:
            changes.append(f"{item['frase']!r} scartata ({item.get('motivo') or 'non valida'})")
    return clean_phrases(phrases), changes


def parse_batch(output: str) -> dict[str, list[str]]:
    """Estrae le frasi dall'output del modello: oggetto JSON {nome tecnico: [frasi]}, eventualmente con
    testo intorno."""
    output = re.sub(r"<think>.*?</think>", "", output or "", flags=re.S)
    m = re.search(r"\{.*\}", output, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return {str(k): clean_phrases(v) for k, v in data.items()} if isinstance(data, dict) else {}


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
        log.info("annunci: genero le frasi per %d tool di %s, %d per richiesta, ragionamento %s", len(tools),
                 server_url, BATCH_SIZE, "attivo" if THINKING else "disattivato")
        partial: dict[str, list[str]] = {}  # frasi valide del primo tentativo, per i tool da ritentare
        retry = await self._run_batches(server_url, tools, partial, final=False)
        if retry:
            log.info("annunci: nuovo tentativo per %d tool con meno di %d frasi valide: %s", len(retry), MIN_GOOD,
                     ", ".join(t["name"] for t in retry))
            await self._run_batches(server_url, retry, partial, final=True)

    async def _run_batches(self, server_url: str, tools: list[dict], partial: dict, final: bool) -> list[dict]:
        """Genera e rivede le frasi a gruppi. Restituisce i tool da ritentare (solo al primo tentativo)."""
        retry = []
        for start in range(0, len(tools), BATCH_SIZE):
            batch = tools[start:start + BATCH_SIZE]
            try:
                results = await self._generate_and_review(batch)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001  si riproverà alla prossima connessione al server MCP
                log.warning("annunci: generazione non riuscita per %s (%s)", ", ".join(t["name"] for t in batch), e)
                continue
            for t in batch:
                phrases = list(dict.fromkeys(partial.get(t["name"], []) + results.get(t["name"], [])))[:6]
                if len(phrases) >= MIN_GOOD or (final and len(phrases) >= 2):
                    async with self._lock:
                        self.data.setdefault(server_url, {})[t["name"]] = {
                            "titolo": t.get("title") or t["name"], "frasi": phrases, "versione": PROMPT_VERSION,
                            "generato": time.strftime("%Y-%m-%d %H:%M:%S")}
                        self._save()
                    log.info("annunci: %s -> %s", t["name"], phrases)
                elif not final:
                    partial[t["name"]] = phrases
                    retry.append(t)
                else:
                    log.warning("annunci: troppe poche frasi valide per %s (%s), uso le frasi generiche",
                                t["name"], phrases)
        return retry

    async def _generate_and_review(self, tools: list[dict]) -> dict[str, list[str]]:
        """Generazione, revisione, validazione frase per frase da parte del modello e controllo automatico
        delle regole verificabili."""
        names = ", ".join(t["name"] for t in tools)
        description = "\n\n".join(
            f"Strumento: {t.get('title') or t['name']}\nNome tecnico: {t['name']}\n"
            f"Descrizione: {(t.get('description') or '')[:1000]}" for t in tools)

        t0 = time.time()
        generated, diag = await self._ask(GENERATION_PROMPT, description)
        log.info("annunci: generazione per %s completata in %.0fs (%s)", names, time.time() - t0, diag_text(diag))
        proposals = {t["name"]: generated.get(t["name"], []) for t in tools}
        missing = [n for n, p in proposals.items() if not p]
        if missing:
            log.warning("annunci: nessuna frase generata per %s (%s, fine del testo: %r)", ", ".join(missing),
                        diag_text(diag), diag["_tail"])

        reviewed: dict[str, list[str]] = {}
        if any(proposals.values()):
            t0 = time.time()
            try:
                reviewed, rdiag = await self._ask(
                    REVIEW_PROMPT, f"{description}\n\nFrasi proposte:\n{json.dumps(proposals, ensure_ascii=False, indent=1)}")
                log.info("annunci: revisione per %s completata in %.0fs (%s)", names, time.time() - t0, diag_text(rdiag))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001  si tengono le frasi generate
                log.warning("annunci: revisione non riuscita per %s (%s): uso le frasi generate", names, e)

        chosen = {t["name"]: (reviewed.get(t["name"]) if len(reviewed.get(t["name"]) or []) >= 2
                              else proposals.get(t["name"], [])) for t in tools}

        validated: dict[str, list[dict]] = {}
        if any(chosen.values()):
            t0 = time.time()
            try:
                validated, vdiag = await self._ask(
                    VALIDATION_PROMPT,
                    f"{description}\n\nFrasi da controllare:\n{json.dumps(chosen, ensure_ascii=False, indent=1)}",
                    parse_validation)
                log.info("annunci: validazione per %s completata in %.0fs (%s)", names, time.time() - t0,
                         diag_text(vdiag))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001  si tengono le frasi della revisione
                log.warning("annunci: validazione non riuscita per %s (%s): uso le frasi della revisione", names, e)

        results = {}
        for t in tools:
            name = t["name"]
            source = chosen[name]
            if validated.get(name):
                checked, changes = apply_validation(validated[name])
                if changes:
                    log.info("annunci: validazione per %s: %s", name, "; ".join(changes))
                if len(checked) >= 2:
                    source = checked
            good, bad = valid_phrases(source)
            if bad:
                log.info("annunci: scartate per %s: %s", name, bad)
            if len(good) < MIN_GOOD and source is not proposals.get(name):  # integra con le frasi generate valide
                good = list(dict.fromkeys(good + valid_phrases(proposals.get(name, []))[0]))
            results[name] = good[:6]
        return results

    async def _ask(self, system: str, user: str, parser=None) -> tuple[dict, dict]:
        payload = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            # ragionamento secondo ANNOUNCEMENTS_THINKING (a prescindere da ENABLE_THINKING); i parametri di
            # campionamento (temperatura, top_p...) sono quelli impostati in llama-server
            "max_tokens": MAX_TOKENS, "stream": False,
            "chat_template_kwargs": {"enable_thinking": THINKING},
        }
        r = await self.http.post("/v1/chat/completions", json=payload)
        r.raise_for_status()
        choice = r.json()["choices"][0]
        content = choice["message"].get("content") or ""
        reasoning = choice["message"].get("reasoning_content") or ""
        diag = {"finish_reason": choice.get("finish_reason"), "testo": len(content), "ragionamento": len(reasoning),
                "_tail": content[-200:]}
        return (parser or parse_batch)(content), diag

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.error("annunci: impossibile salvare %s (%s): le frasi restano solo in memoria", self.path, e)


def diag_text(diag: dict) -> str:
    """finish_reason=length significa che il limite di token (MAX_TOKENS) è finito prima della risposta."""
    return (f"finish_reason={diag['finish_reason']}, testo {diag['testo']} caratteri, "
            f"ragionamento {diag['ragionamento']} caratteri")


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
