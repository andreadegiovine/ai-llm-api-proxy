"""
proxy.py - proxy OpenAI-compatibile davanti a llama.cpp.

- sostituisce i system prompt del client con il proprio;
- aggiunge i tool del server MCP (e compact_memory) ed esegue le chiamate lato proxy;
- streaming reale token per token, anche tra un giro di tool e l'altro;
- prima di eseguire un tool scrive in chat 'Uso "<titolo del tool>"...';
- compatta la memoria quando il prompt si avvicina al contesto massimo (vedi compact_tool.py);
- se PROXY_API_KEYS è impostata, accetta solo richieste con una API key valida.

La configurazione si legge da .env nella cartella del proxy (vedi .env.example).
Avvio: python proxy.py
"""

import asyncio
import contextlib
import hashlib
import hmac
import html
import itertools
import json
import logging
import os
import re
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

APP_DIR = Path(__file__).resolve().parent

try:  # .env va caricato prima di importare compact_tool, che legge le sue variabili all'import
    from dotenv import load_dotenv

    load_dotenv(APP_DIR / ".env")  # le variabili già presenti nell'ambiente hanno la precedenza
except ImportError:
    pass

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from mcp import ClientSession
from mcp.client.sse import sse_client
from starlette.background import BackgroundTask

try:  # mcp >= 2
    from mcp.client.streamable_http import streamable_http_client as http_transport
except ImportError:  # mcp 1.x
    from mcp.client.streamable_http import streamablehttp_client as http_transport

import compact_tool
from compact_tool import Compactor
from history import HistoryDB, Session, visible_hash

LLAMA_URL = os.getenv("LLAMA_URL", "http://192.168.1.10:8080")
LLAMA_API_KEY = os.getenv("LLAMA_API_KEY", "")
MCP_URL = os.getenv("MCP_URL", "http://192.168.1.11:8000/mcp")  # vuoto = nessun server MCP
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "http")               # "http" (streamable) oppure "sse"
SYSTEM_PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE") or str(APP_DIR / "system_prompt.txt")
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "0"))      # 0 = nessun limite ai giri di tool
TOOL_REPEAT_LIMIT = int(os.getenv("TOOL_REPEAT_LIMIT", "3"))  # stessa chiamata e stesso risultato N volte = loop
# true: ragionamenti come <think>...</think> e box <details type="tool_calls"> nel testo (es. Open WebUI)
# false: solo testo, ragionamenti nel campo standard reasoning_content
SEND_CHAT_MARKDOWN = os.getenv("SEND_CHAT_MARKDOWN", "false").strip().lower() in ("1", "true", "yes", "on")
THINK = SEND_CHAT_MARKDOWN
AUTO_COMPACT = os.getenv("AUTO_COMPACT", "1") == "1"
ENABLE_COMPACT_TOOL = os.getenv("ENABLE_COMPACT_TOOL", "1") == "1"
PROXY_HOST = os.getenv("PROXY_HOST", "0.0.0.0")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8081"))
PROXY_API_KEYS = [k.strip() for k in os.getenv("PROXY_API_KEYS", "").split(",") if k.strip()]
FORWARDED_ALLOW_IPS = os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1")  # IP di nginx, per loggare l'IP reale
# Fuso orario della data e ora di inizio conversazione, scritte nel system prompt
TIMEZONE = os.getenv("TIMEZONE", "Europe/Rome")
# Database "history": tool call, risultati e ragionamenti di ogni conversazione, per ricostruirla per intero
ENABLE_HISTORY = os.getenv("ENABLE_HISTORY", "1") == "1"
HISTORY_DB = os.getenv("HISTORY_DB") or str(APP_DIR / "history.db")
HISTORY_RETENTION_DAYS = int(os.getenv("HISTORY_RETENTION_DAYS", "90"))
HISTORY_CHAT_ID_HEADER = os.getenv("HISTORY_CHAT_ID_HEADER", "X-OpenWebUI-Chat-Id")
KEEPALIVE_SECONDS = 15

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("proxy")
for _noisy in ("httpx", "httpx2", "mcp"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Righe di stato scritte dal proxy: vengono rimosse dalla cronologia prima di inviarla al modello.
COMPACT_NOTICE_RE = re.compile(r"^Memoria compattata \(\d+ → \d+ token su \d+\)$")
LIMIT_NOTICE_RE = re.compile(r"^(?:Limite di \d+ giri di strumenti raggiunto|Interrotta la ripetizione di \".*\")$")
LEGACY_STATUS_RE = re.compile(r"^(?:🔧 Uso .*|🗜️ Memoria compattata.*)$")  # formato delle versioni precedenti
# Ragionamenti e box dei tool rimandati dal client dentro il testo: non devono tornare al modello
# né finire nei riassunti.
DETAILS_BLOCK_RE = re.compile(
    r"<think>.*?(?:</think>\s*|\Z)|<details\s+type=\"(?:reasoning|tool_calls)\".*?(?:</details>\s*|\Z)", re.S)


def tool_title(t) -> str:
    """Nome leggibile di un tool MCP: `title`, poi `annotations.title`, infine il nome tecnico."""
    return getattr(t, "title", None) or getattr(getattr(t, "annotations", None), "title", None) or t.name


def load_system_prompt() -> str:
    for path in (Path(SYSTEM_PROMPT_FILE), APP_DIR / "system_prompt.example.txt"):
        if path.is_file():
            if path.name == "system_prompt.example.txt":
                log.warning("%s non trovato: uso il prompt di esempio %s", SYSTEM_PROMPT_FILE, path)
            return path.read_text(encoding="utf-8").strip()
    log.warning("system prompt non trovato in %s: nessun system prompt", SYSTEM_PROMPT_FILE)
    return ""


# ====================================================================== MCP
class McpBridge:
    """Connessione persistente al server MCP, con riconnessione automatica in background."""

    def __init__(self, url: str, transport: str):
        self.url, self.transport = url, transport
        self.session: ClientSession | None = None
        self.tools: list[dict] = []
        self.names: set[str] = set()
        self.titles: dict[str, str] = {}
        # tutti i nomi e i titoli visti: servono a riconoscere le righe "Uso ..." nella cronologia
        self.labels: set[str] = {compact_tool.TOOL_NAME, compact_tool.TOOL_TITLE}
        self._ready = asyncio.Event()
        self._reset = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self):
        if self.url:
            self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def _run(self):
        while True:
            try:
                async with AsyncExitStack() as stack:
                    if self.transport == "sse":
                        streams = await stack.enter_async_context(sse_client(self.url))
                    else:
                        streams = await stack.enter_async_context(http_transport(self.url))
                    session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    self.tools = [{
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description or "",
                            "parameters": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
                                          or {"type": "object", "properties": {}},
                        },
                    } for t in tools]
                    self.names = {t.name for t in tools}
                    self.titles = {t.name: tool_title(t) for t in tools}
                    self.labels |= self.names | set(self.titles.values())
                    self.session = session
                    self._reset.clear()
                    self._ready.set()
                    log.info("MCP connesso, tool: %s", sorted(self.names))
                    await self._reset.wait()
            except asyncio.CancelledError:
                raise
            except BaseException as e:  # noqa: BLE001  (include gli ExceptionGroup di anyio)
                self.session = None
                self._ready.clear()
                log.warning("MCP non raggiungibile (%s): riprovo tra 5s", e)
                await asyncio.sleep(5)
            self.session = None
            self._ready.clear()

    async def _wait_ready(self, timeout: float = 15) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
            return self.session is not None
        except asyncio.TimeoutError:
            return False

    async def call(self, name: str, args: dict) -> str:
        if self.session is not None and name not in self.names:
            return f"ERRORE: il tool '{name}' non esiste. Tool disponibili: {', '.join(sorted(self.names))}"
        for attempt in (1, 2):  # al primo errore si riconnette e riprova una volta
            if not await self._wait_ready():
                return "ERRORE: server MCP non disponibile."
            session = self.session
            try:
                res = await session.call_tool(name, args)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    return f"ERRORE durante l'esecuzione di {name}: {e}"
                log.warning("chiamata MCP fallita (%s): mi riconnetto e riprovo", e)
                if self.session is session:
                    self.session = None
                    self._ready.clear()
                    self._reset.set()
        parts = []
        for c in getattr(res, "content", None) or []:
            parts.append(c.text if getattr(c, "type", "") == "text" else f"[contenuto '{c.type}' omesso]")
        structured = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
        if not parts and structured:
            parts.append(json.dumps(structured, ensure_ascii=False))
        is_error = getattr(res, "is_error", None) or getattr(res, "isError", False)
        return ("ERRORE: " if is_error else "") + ("\n".join(parts) or "(nessun output)")


# ====================================================================== app
state: dict = {}


@asynccontextmanager
async def lifespan(app):
    headers = {"Authorization": f"Bearer {LLAMA_API_KEY}"} if LLAMA_API_KEY else {}
    async with httpx.AsyncClient(base_url=LLAMA_URL, timeout=httpx.Timeout(None, connect=10), headers=headers) as http:
        mcp = McpBridge(MCP_URL, MCP_TRANSPORT)
        mcp.start()
        history = None
        if ENABLE_HISTORY:
            try:
                history = HistoryDB(HISTORY_DB, HISTORY_RETENTION_DAYS)
            except Exception as e:  # noqa: BLE001  senza database il proxy funziona comunque
                log.error("database history non disponibile (%s: %s): continuo senza history", HISTORY_DB, e)
        state.update(http=http, mcp=mcp, compactor=Compactor(http), system_prompt=load_system_prompt(),
                     history=history)
        yield
        await mcp.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def check_api_key(request: Request, call_next):
    """Con PROXY_API_KEYS impostata, accetta solo `Authorization: Bearer <key>` (o `x-api-key`)."""
    if PROXY_API_KEYS and request.url.path != "/health":
        auth = request.headers.get("authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
        if not any(hmac.compare_digest(key.encode(), k.encode()) for k in PROXY_API_KEYS):
            return JSONResponse({"error": {"message": "API key mancante o non valida",
                                           "type": "invalid_request_error", "code": 401}}, status_code=401)
    return await call_next(request)


def sse(obj) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


DONE = "data: [DONE]\n\n"


def is_status_line(line: str) -> bool:
    line = line.strip()
    if COMPACT_NOTICE_RE.match(line) or LIMIT_NOTICE_RE.match(line) or LEGACY_STATUS_RE.match(line):
        return True
    if not (line.startswith("Uso ") and line.endswith("...")):
        return False
    label = line[4:-3]
    if len(label) >= 2 and label[0] == label[-1] == '"':
        label = label[1:-1]  # formato attuale: Uso "titolo"...  (senza virgolette: versioni precedenti)
    return label in state["mcp"].labels


def strip_status(text: str) -> str:
    lines = text.split("\n")
    kept = [ln for ln in lines if not is_status_line(ln)]
    if len(kept) == len(lines):
        return text
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip("\n")


def clean_history(messages: list[dict]) -> list[dict]:
    """Toglie le righe di stato del proxy dai messaggi assistant che il client reinvia."""
    out = []
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("content"), str):
            text = TEXT_TOOL_CALL_RE.sub("", DETAILS_BLOCK_RE.sub("", m["content"]))  # niente esempi sbagliati
            m = {**m, "content": strip_status(text)}
        out.append(m)
    return out


def prepare_body(raw: dict):
    body = dict(raw)
    stream = bool(body.pop("stream", False))
    include_usage = bool((body.pop("stream_options", None) or {}).get("include_usage"))

    client_tools = body.get("tools") or []
    client_names = {(t.get("function") or {}).get("name") for t in client_tools}
    server_tools = list(state["mcp"].tools)
    if ENABLE_COMPACT_TOOL:
        server_tools.append(compact_tool.TOOL_SCHEMA)
    extra = [t for t in server_tools if t["function"]["name"] not in client_names]
    if extra:
        body["tools"] = client_tools + extra
    return body, stream, include_usage, client_names


def visible_text(msg: dict) -> str:
    """Testo che l'utente vede di un messaggio già ripulito, con gli spazi normalizzati."""
    return re.sub(r"\s+", " ", compact_tool.msg_text(msg)).strip()


def open_session(raw_messages: list[dict], client_names: set, chat_id: str | None) -> Session:
    """Sessione della richiesta: dal database history se la conversazione è gestita dal proxy, altrimenti
    senza memoria (richieste con tool del client, che gestisce da sé chiamate e risultati)."""
    cleaned = [m for m in clean_history(raw_messages or []) if m.get("role") not in ("system", "developer")]
    history: HistoryDB | None = state["history"]
    stateless = (history is None or client_names or not cleaned or cleaned[-1].get("role") != "user"
                 or any(m.get("role") == "tool" or m.get("tool_calls") for m in cleaned))
    if stateless:
        return Session(None, None, 0, messages=cleaned)
    items = [(m["role"], visible_text(m)) for m in cleaned if m.get("role") in ("user", "assistant")]
    session = history.open(items[:-1], cleaned[:-1], cleaned[-1], chat_id)
    session.user_item = items[-1]
    return session


def save_session(session: Session, sent_text: str):
    """Salva il turno, con l'impronta del testo che il client vedrà (e rimanderà) per questa risposta."""
    if session.db is None or session.saved:
        return
    assistant = clean_history([{"role": "assistant", "content": sent_text}])[0]
    after = visible_hash(session.prefix_items + [session.user_item, ("assistant", visible_text(assistant))])
    try:
        session.save(after)
    except Exception:  # noqa: BLE001  il salvataggio non deve far fallire la risposta
        log.exception("salvataggio della conversazione %s non riuscito", session.conv_id)


def split_calls(calls, client_names):
    """Divide le tool call in (lato proxy, lato client). I nomi sconosciuti vanno al proxy,
    che risponde al modello con un errore e gli permette di correggersi."""
    server = [c for c in calls if c["function"]["name"] not in client_names]
    client = [c for c in calls if c["function"]["name"] in client_names]
    return server, client


def status_line(text: str) -> str:
    return f"{text}\n\n"


WEEKDAYS = ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica")
MONTHS = ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre",
          "ottobre", "novembre", "dicembre")


def start_datetime_line(ts: float) -> str:
    """Data e ora di inizio della conversazione. Restano le stesse per tutta la conversazione (e nei suoi
    rami): il system prompt non cambia e llama.cpp può riusare la parte di prompt già elaborata."""
    try:
        tz = ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    dt = datetime.fromtimestamp(ts, tz)
    offset = dt.strftime("%z")
    return (f"Data e ora di inizio della conversazione: {WEEKDAYS[dt.weekday()]} {dt.day} {MONTHS[dt.month - 1]} "
            f"{dt.year}, {dt:%H:%M} ({TIMEZONE}, UTC{offset[:3]}:{offset[3:]}). Per la data e l'ora correnti usa "
            "sempre lo strumento apposito.")


def conversation_id_line(session: Session) -> str:
    """ID della conversazione nel database history, per i tool del server MCP che lo richiedono."""
    if session.db is None or not session.conv_id:
        return ""
    return f"ID della conversazione: {session.conv_id}. Usalo quando uno strumento richiede l'ID della conversazione."


def system_messages(session: Session) -> list[dict]:
    parts = (state["system_prompt"], start_datetime_line(session.started_at), conversation_id_line(session))
    return [{"role": "system", "content": "\n\n".join(x for x in parts if x)}]


async def prepare_round(body: dict, session: Session) -> dict | None:
    """Messaggi per il prossimo giro: system prompt (con l'eventuale riassunto in coda), messaggi della
    sessione, eventuale nota temporanea. Compatta se il contesto si avvicina al massimo."""
    compactor: Compactor = state["compactor"]
    info = None
    if AUTO_COMPACT:
        working, info = await compactor.prepare(
            system_messages(session) + session.conv, body.get("tools"), model=body.get("model"),
            reserve=body.get("max_tokens") or body.get("max_completion_tokens"),
            summary=session.summary, use_cache=session.db is None and session.summary is None)
        session.apply_compaction(info, working)
    else:
        working = compactor.build(system_messages(session), session.summary, session.conv)
    body["messages"] = working + session.transient
    return info


def compact_notice(info) -> str | None:
    if info and info.get("compacted"):
        return f"Memoria compattata ({info['before']} → {info['after']} token su {info['n_ctx']})"
    return None


async def execute_server_tools(body: dict, session: Session, assistant_msg: dict, calls: list[dict]) -> list[str]:
    """Esegue i tool lato proxy, aggiunge alla sessione la chiamata e i risultati
    e restituisce i risultati, nello stesso ordine delle chiamate."""
    compactor: Compactor = state["compactor"]
    model = body.get("model")
    results = []
    for c in calls:
        name = c["function"]["name"]
        raw_args = c["function"].get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except (json.JSONDecodeError, TypeError, ValueError):
            results.append(f"ERRORE: argomenti non in JSON valido: {raw_args}")
            continue
        if name == compact_tool.TOOL_NAME:
            # compatta la conversazione *prima* della chiamata corrente
            working, info = await compactor.prepare(
                system_messages(session) + session.conv, body.get("tools"), model=model, force=True,
                focus=args.get("focus"), summary=session.summary, use_cache=False)
            session.apply_compaction(info, working)
            results.append(compactor.describe(info))
        else:
            log.info("tool %s %s", name, raw_args)
            results.append(await compactor.cap_tool_result(await state["mcp"].call(name, args), model))
    session.add(assistant_msg)
    for c, r in zip(calls, results):
        session.add({"role": "tool", "tool_call_id": c["id"], "content": r})
    return results


# Tool call scritte come testo (spesso dentro il ragionamento) invece che come tool call vere.
# Formati: XML dei modelli Qwen recenti e JSON stile Hermes.
#   <tool_call><function=nome><parameter=p>valore</parameter></function></tool_call>
#   <tool_call>{"name": "nome", "arguments": {...}}</tool_call>
TEXT_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|\Z)", re.S)
XML_FUNCTION_RE = re.compile(r"<function=([^>\s]+)>(.*?)(?:</function>|\Z)", re.S)
XML_PARAMETER_RE = re.compile(r"<parameter=([^>\s]+)>(.*?)</parameter>", re.S)


def extract_text_tool_calls(text: str, tools: list[dict]) -> list[dict]:
    """Ricava le tool call scritte come testo. Considera solo i tool esistenti, per non scambiare
    per una chiamata un esempio citato dal modello."""
    schemas = {(t.get("function") or {}).get("name"): ((t.get("function") or {}).get("parameters") or {})
               for t in tools or []}
    calls = []
    for block in TEXT_TOOL_CALL_RE.findall(text or ""):
        name, args = None, None
        if block.startswith("{"):
            try:
                obj = json.loads(block)
                name, args = obj.get("name"), obj.get("arguments", obj.get("parameters", {}))
                if isinstance(args, str):
                    args = json.loads(args)
            except (json.JSONDecodeError, AttributeError):
                continue
        elif m := XML_FUNCTION_RE.search(block):
            name, args = m.group(1), {}
            props = (schemas.get(name) or {}).get("properties") or {}
            for pname, pvalue in XML_PARAMETER_RE.findall(m.group(2)):
                value = pvalue.strip()
                if (props.get(pname) or {}).get("type", "string") != "string":
                    try:  # numeri, booleani, oggetti e liste
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        pass
                args[pname] = value
        if name in schemas and isinstance(args, dict):
            calls.append({"id": f"call_{uuid.uuid4().hex[:24]}", "type": "function",
                          "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}})
    return calls


def recover_text_tool_calls(reasoning: str, content: str, tools: list[dict]) -> list[dict]:
    calls = extract_text_tool_calls(f"{reasoning}\n{content}", tools)
    if calls:
        log.warning("il modello ha scritto %d tool call come testo (nel ragionamento o nella risposta) "
                    "invece che come tool call: le eseguo comunque (%s)", len(calls),
                    ", ".join(c["function"]["name"] for c in calls))
    return calls


def tool_box(call: dict, result: str) -> str:
    """Box nativo di Open WebUI per un tool eseguito (argomenti in ingresso, risultato in uscita)."""
    raw_args = call["function"].get("arguments") or "{}"
    try:  # una sola riga: Open WebUI legge gli attributi riga per riga
        args = json.dumps(json.loads(raw_args) if isinstance(raw_args, str) else raw_args, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError, ValueError):
        args = json.dumps(raw_args, ensure_ascii=False)
    attrs = {
        "type": "tool_calls",
        "done": "true",
        "id": call["id"],
        "name": tool_display_name(call["function"]["name"]),
        "arguments": args,
        "result": json.dumps(result, ensure_ascii=False),
    }
    if result.startswith("ERRORE"):
        attrs["status"] = "failed"
    attr_text = " ".join(f'{k}="{html.escape(str(v), quote=True)}"' for k, v in attrs.items())
    return f"<details {attr_text}>\n<summary>Tool Executed</summary>\n</details>"


def tool_rounds():
    """Giri di tool di una risposta: illimitati con MAX_TOOL_ROUNDS=0."""
    return itertools.count() if MAX_TOOL_ROUNDS <= 0 else range(MAX_TOOL_ROUNDS)


def force_final_round(body: dict, session: Session, repeated: str | None = None) -> str:
    """Ultimo giro: niente più tool, e il modello sa perché. Senza questa nota il modello tende ad
    annunciare un'azione ("Procedo con l'invio!") che non può più eseguire.
    `repeated`: titolo del tool chiamato in loop; None = limite dei giri raggiunto."""
    body["tool_choice"] = "none"
    if repeated:
        reason = (f"Hai chiamato {TOOL_REPEAT_LIMIT} volte di seguito lo strumento \"{repeated}\" con gli stessi "
                  "argomenti, ottenendo sempre lo stesso risultato")
        ask = "Spiega all'utente cosa è successo, cosa hai ottenuto e cosa resta da fare, poi chiedi come procedere."
        log.warning("il modello ha ripetuto %d volte la stessa chiamata a %s con lo stesso risultato: "
                    "ultimo giro senza tool", TOOL_REPEAT_LIMIT, repeated)
        notice = f'Interrotta la ripetizione di "{repeated}"'
    else:
        rounds = MAX_TOOL_ROUNDS - 1
        reason = f"Hai raggiunto il limite di {rounds} giri di strumenti per questa risposta"
        ask = "Riassumi cosa hai fatto, i risultati ottenuti e cosa resta da fare, poi chiedi all'utente se vuole che tu continui."
        log.warning("raggiunto il limite di %d giri di strumenti (MAX_TOOL_ROUNDS=%d): ultimo giro senza tool",
                    rounds, MAX_TOOL_ROUNDS)
        notice = f"Limite di {rounds} giri di strumenti raggiunto"
    session.transient = [{"role": "user", "content": (
        f"[Messaggio automatico del proxy] {reason}: in questo turno non puoi usare altri strumenti. "
        f"Non annunciare azioni che non puoi eseguire. {ask}")}]
    return notice


class RepeatDetector:
    """Riconosce un loop: lo stesso giro di tool (stessi tool, argomenti e risultati) ripetuto
    TOOL_REPEAT_LIMIT volte di seguito. Il risultato conta: controllare più volte lo stato di un comando
    in background, con un risultato che cambia, non è un loop."""

    def __init__(self):
        self.last, self.count = None, 0

    def add(self, calls: list[dict], results: list[str]) -> str | None:
        """Restituisce il titolo del tool ripetuto se il loop è stato riconosciuto."""
        if TOOL_REPEAT_LIMIT <= 0:
            return None
        items = []
        for c, r in zip(calls, results):
            raw = c["function"].get("arguments") or "{}"
            try:
                args = json.dumps(json.loads(raw) if isinstance(raw, str) else raw, sort_keys=True, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError, ValueError):
                args = str(raw)
            items.append([c["function"]["name"], args, hashlib.sha256(r.encode()).hexdigest()])
        signature = json.dumps(sorted(items), ensure_ascii=False)
        self.count = self.count + 1 if signature == self.last else 1
        self.last = signature
        return tool_display_name(calls[0]["function"]["name"]) if self.count >= TOOL_REPEAT_LIMIT else None


def assistant_tool_message(content: str, calls: list[dict], reasoning: str) -> dict:
    """Messaggio assistant con le tool call, da rimandare a llama.cpp nel giro successivo. Include il
    ragionamento fatto prima della chiamata, come fa Open WebUI con llama.cpp: i modelli "thinking"
    recenti contano di ritrovarlo tra un giro di tool e l'altro."""
    msg = {"role": "assistant", "content": content, "tool_calls": calls}
    if reasoning:
        msg["reasoning_content"] = reasoning
    return msg


def final_assistant_message(content: str, reasoning: str) -> dict:
    msg = {"role": "assistant", "content": content}
    if reasoning:
        msg["reasoning_content"] = reasoning
    return msg


def log_response_end(rounds: int, tools_done: int, finish: str | None, content: str, reasoning: str,
                     limit_hit: bool):
    """Riepilogo di ogni risposta, per capire perché un turno si è chiuso."""
    log.info("risposta conclusa: giri=%d, tool eseguiti=%d, finish_reason=%s, testo finale=%d caratteri, "
             "ragionamento finale=%d caratteri%s", rounds, tools_done, finish, len(content), len(reasoning),
             ", ultimo giro senza tool (limite dei giri o ripetizione)" if limit_hit else "")
    if finish == "tool_calls":
        log.warning("llama.cpp ha chiuso il giro con finish_reason=tool_calls ma senza tool call utilizzabili "
                    "(probabile tool call malformata o non riconosciuta dal parser). Fine del ragionamento: %r. "
                    "Fine del testo: %r", reasoning[-400:], content[-400:])
    elif finish == "length":
        log.warning("risposta troncata dal limite di token (finish_reason=length): aumenta max_tokens nel client "
                    "o -n/--n-predict in llama-server")
    log.debug("fine del testo finale: %r", content[-300:])


def tool_display_name(name: str) -> str:
    if name == compact_tool.TOOL_NAME:
        return compact_tool.TOOL_TITLE
    return state["mcp"].titles.get(name) or name


def tool_status_text(calls) -> str:
    return "\n".join(f'Uso "{tool_display_name(c["function"]["name"])}"...' for c in calls)


# ====================================================================== statistiche (token, velocità)
STANDARD_CHUNK_KEYS = {"id", "object", "created", "model", "choices", "usage", "timings"}


def merge_timings(a: dict | None, b: dict | None) -> dict | None:
    """Somma i `timings` di llama.cpp di due giri e ricalcola le velocità.
    `cache_n` viene ricavato in modo che `cache_n + prompt_n + predicted_n` resti il contesto
    reale dell'ultimo giro, che i client usano per mostrare l'occupazione del contesto."""
    if not a or not b:
        return dict(a or b) if (a or b) else None
    out = dict(b)
    for k in set(a) | set(b):
        summable = (k.endswith("_n") and k != "cache_n") or (k.endswith("_ms") and not k.endswith("per_token_ms"))
        if summable and isinstance(a.get(k, 0), (int, float)) and isinstance(b.get(k, 0), (int, float)):
            out[k] = a.get(k, 0) + b.get(k, 0)
    ctx = b.get("cache_n", 0) + b.get("prompt_n", 0) + b.get("predicted_n", 0)
    out["cache_n"] = max(0, ctx - out.get("prompt_n", 0) - out.get("predicted_n", 0))
    for p in ("prompt", "predicted"):
        n, ms = out.get(f"{p}_n", 0), out.get(f"{p}_ms", 0)
        out[f"{p}_per_token_ms"] = ms / n if n else 0
        out[f"{p}_per_second"] = 1000 * n / ms if ms else 0
    return out


class RunStats:
    """Raccoglie `usage` e `timings` di tutti i giri (tool compresi) di una risposta."""

    def __init__(self):
        self.prev_timings: dict | None = None
        self.prev_completion = 0
        self.round_timings: dict | None = None
        self.round_usage: dict | None = None

    def see(self, data: dict):
        if data.get("timings"):
            self.round_timings = data["timings"]
        if data.get("usage"):
            self.round_usage = data["usage"]

    def end_round(self):
        self.prev_timings = merge_timings(self.prev_timings, self.round_timings)
        self.prev_completion += (self.round_usage or {}).get("completion_tokens", 0)
        self.round_timings = self.round_usage = None

    def timings(self) -> dict | None:
        return merge_timings(self.prev_timings, self.round_timings)

    def usage(self) -> dict | None:
        """prompt_tokens = contesto dell'ultimo giro; completion_tokens = somma di tutti i giri."""
        if not self.round_usage and not self.prev_completion:
            return None
        u = dict(self.round_usage or {})
        u["completion_tokens"] = u.get("completion_tokens", 0) + self.prev_completion
        u["total_tokens"] = u.get("prompt_tokens", 0) + u["completion_tokens"]
        return u

    def fields(self) -> dict:
        return {k: v for k, v in (("usage", self.usage()), ("timings", self.timings())) if v}


# ====================================================================== /v1/chat/completions
@app.post("/v1/chat/completions")
async def chat(req: Request):
    raw = await req.json()
    body, stream, include_usage, client_names = prepare_body(raw)
    session = open_session(raw.get("messages") or [], client_names, req.headers.get(HISTORY_CHAT_ID_HEADER))
    if stream:
        return StreamingResponse(chat_stream(body, session, client_names, include_usage),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return await chat_once(body, session, client_names)


async def chat_once(body, session: Session, client_names):
    """Versione non-streaming: il ciclo dei tool avviene tutto prima di rispondere."""
    body["stream"] = False
    data = None
    stats = RunStats()
    tools_done, limit_hit, rnd, thought = 0, False, 0, ""
    repeats, repeated = RepeatDetector(), None
    segments: list[str] = []    # testo visibile, nell'ordine in cui è avvenuto
    reasonings: list[str] = []  # ragionamenti, se SEND_CHAT_MARKDOWN=false

    def add_reasoning(text):
        if text:
            if THINK:
                segments.append(f"<think>\n{text}\n</think>")
            else:
                reasonings.append(text)

    def add_status(text):
        segments.append(text)

    for rnd in tool_rounds():
        if (repeated or MAX_TOOL_ROUNDS > 0 and rnd == MAX_TOOL_ROUNDS - 1) and body.get("tools") and not limit_hit:
            add_status(force_final_round(body, session, repeated))  # ultimo giro: obbliga una risposta testuale
            limit_hit = True
        if notice := compact_notice(await prepare_round(body, session)):
            add_status(notice)
        r = await state["http"].post("/v1/chat/completions", json=body)
        if r.status_code != 200:
            if tools_done:  # i tool eseguiti hanno avuto effetti: la conversazione li deve ricordare
                save_session(session, "\n\n".join(segments))
            return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))
        data = r.json()
        stats.see(data)
        msg = data["choices"][0]["message"]
        thought = msg.pop("reasoning_content", None) or msg.pop("reasoning", None) or ""
        if body.get("tool_choice") == "none":  # giro senza tool: eventuali chiamate vengono ignorate
            msg.pop("tool_calls", None)
        elif not msg.get("tool_calls") and body.get("tools"):
            if recovered := recover_text_tool_calls(thought, msg.get("content") or "", body["tools"]):
                msg["tool_calls"] = recovered
                msg["content"] = TEXT_TOOL_CALL_RE.sub("", msg.get("content") or "").strip()
        server, client = split_calls(msg.get("tool_calls") or [], client_names)
        add_reasoning(thought)
        if msg.get("content"):
            segments.append(msg["content"])
        if client or not server:
            if client:
                msg["tool_calls"] = client
            break
        stats.end_round()
        for c in server:
            c["id"] = c.get("id") or f"call_{uuid.uuid4().hex[:24]}"
        add_status(tool_status_text(server))
        results = await execute_server_tools(
            body, session, assistant_tool_message(msg.get("content") or "", server, thought), server)
        tools_done += len(server)
        repeated = repeats.add(server, results)
        if SEND_CHAT_MARKDOWN:
            segments.append("\n".join(tool_box(c, r) for c, r in zip(server, results)))

    msg = data["choices"][0]["message"]
    log_response_end(rnd + 1, tools_done, data["choices"][0].get("finish_reason"), msg.get("content") or "",
                     thought, limit_hit)
    session.add(final_assistant_message(msg.get("content") or "", thought))
    if segments:
        msg["content"] = "\n\n".join(segments)
    if reasonings:
        msg["reasoning_content"] = "\n\n".join(reasonings)
    data.update(stats.fields())
    save_session(session, msg.get("content") or "")
    return JSONResponse(data)


async def chat_stream(body, session: Session, client_names, include_usage):
    """Streaming reale: inoltra i token man mano, trattiene le tool call, esegue i tool MCP
    e continua lo stesso flusso SSE con il giro successivo. A fine risposta (o se il client si
    disconnette dopo aver ricevuto qualcosa) il turno viene salvato nel database history."""
    sent: list[str] = []  # testo inviato al client, per riconoscere la conversazione al turno successivo
    try:
        async for out in _chat_stream(body, session, client_names, include_usage, sent):
            yield out
    finally:
        if sent or session.pending[1:]:
            save_session(session, "".join(sent))


async def _chat_stream(body, session: Session, client_names, include_usage, sent: list[str]):
    cid = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    model = body.get("model") or "llama.cpp"
    role_sent = False
    stats = RunStats()
    need_sep = False  # separa con una riga vuota il testo di giri diversi
    in_think = False  # SEND_CHAT_MARKDOWN=true: blocco <think> aperto
    reasoning_sent = False  # SEND_CHAT_MARKDOWN=false: separa i ragionamenti di giri diversi

    def open_think() -> str:
        nonlocal in_think, need_sep
        if in_think:
            return ""
        prefix = "\n\n" if need_sep else ""
        in_think, need_sep = True, False
        return prefix + "<think>\n"

    def close_think() -> str:
        nonlocal in_think, need_sep
        if not in_think:
            return ""
        in_think, need_sep = False, False
        return "\n</think>\n\n"

    def chunk(delta, finish=None, extra=None):
        nonlocal role_sent
        if delta.get("content"):
            sent.append(delta["content"])
        if not role_sent:
            delta = {"role": "assistant", **delta}
            role_sent = True
        return sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **(extra or {})})

    def status_chunk(text):
        nonlocal need_sep
        prefix = "\n\n" if need_sep else ""
        need_sep = False
        return chunk({"content": prefix + status_line(text)})

    def error(msg, code=500):
        return sse({"error": {"message": msg, "code": code}}) + DONE

    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    tools_done, limit_hit = 0, False
    repeats, repeated = RepeatDetector(), None

    for rnd in tool_rounds():
        if (repeated or MAX_TOOL_ROUNDS > 0 and rnd == MAX_TOOL_ROUNDS - 1) and body.get("tools") and not limit_hit:
            yield status_chunk(force_final_round(body, session, repeated))  # ultimo giro: obbliga una risposta testuale
            limit_hit = True

        if notice := compact_notice(await prepare_round(body, session)):
            yield status_chunk(notice)

        text_parts: list[str] = []
        thought_parts: list[str] = []
        calls: dict[int, dict] = {}
        finish = None
        first_content = True
        first_thought = True
        try:
            async with state["http"].stream("POST", "/v1/chat/completions", json=body) as r:
                if r.status_code != 200:
                    yield error((await r.aread()).decode(errors="replace"), r.status_code)
                    return
                async for line in r.aiter_lines():
                    if line.startswith("error:"):
                        yield error(line[6:].strip())
                        return
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    data = json.loads(payload)
                    if "error" in data:
                        yield sse(data) + DONE
                        return
                    stats.see(data)
                    model = data.get("model") or model
                    # campi extra di llama.cpp (es. prompt_progress); timings aggiornati se presenti
                    extra = {k: v for k, v in data.items() if k not in STANDARD_CHUNK_KEYS}
                    if data.get("timings"):
                        extra["timings"] = stats.timings()
                    if "prompt_progress" in data and not data.get("choices"):
                        yield chunk({}, extra=extra)
                    for ch in data.get("choices") or []:
                        delta = ch.get("delta") or {}
                        finish = ch.get("finish_reason") or finish
                        for tc in delta.get("tool_calls") or []:
                            slot = calls.setdefault(tc.get("index", 0), {
                                "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            slot["function"]["name"] += fn.get("name") or ""
                            slot["function"]["arguments"] += fn.get("arguments") or ""
                        fwd = {k: v for k, v in delta.items() if k not in ("tool_calls", "role") and v not in (None, "")}
                        thought_parts.append(fwd.get("reasoning_content") or fwd.get("reasoning") or "")
                        out = ""
                        if THINK and (thought := fwd.pop("reasoning_content", None) or fwd.pop("reasoning", None)):
                            out += open_think() + thought  # ragionamento come <think> nel punto in cui avviene
                        elif fwd.get("reasoning_content"):
                            if first_thought and reasoning_sent:
                                fwd["reasoning_content"] = "\n\n" + fwd["reasoning_content"]
                            first_thought, reasoning_sent = False, True
                        if fwd.get("content"):
                            if in_think:
                                out += close_think()
                            elif first_content and need_sep:
                                out += "\n\n"
                            first_content = False
                            out += fwd["content"]
                            need_sep = True
                            text_parts.append(delta["content"])
                        if out:
                            fwd["content"] = out
                        if fwd or "prompt_progress" in data:
                            yield chunk(fwd, extra=extra)
        except httpx.HTTPError as e:
            yield error(f"llama.cpp non raggiungibile: {e}", 502)
            return

        if closing := close_think():
            yield chunk({"content": closing})

        tool_calls = [calls[i] for i in sorted(calls)]
        if body.get("tool_choice") == "none":  # giro senza tool: eventuali chiamate vengono ignorate
            tool_calls = []
        elif not tool_calls and body.get("tools"):
            # il modello ha scritto la chiamata come testo (tipicamente nel ragionamento)
            tool_calls = recover_text_tool_calls("".join(thought_parts), "".join(text_parts), body["tools"])
            if tool_calls:
                text_parts = [TEXT_TOOL_CALL_RE.sub("", "".join(text_parts)).strip()]
        for c in tool_calls:
            c["id"] = c["id"] or f"call_{uuid.uuid4().hex[:24]}"
        server, client = split_calls(tool_calls, client_names)

        if client or not server:  # risposta finale, oppure tool che deve eseguire il client
            log_response_end(rnd + 1, tools_done, finish, "".join(text_parts), "".join(thought_parts), limit_hit)
            if not client:
                session.add(final_assistant_message("".join(text_parts), "".join(thought_parts)))
            if client:
                yield chunk({"tool_calls": [{**c, "index": i} for i, c in enumerate(client)]})
                finish = "tool_calls"
            elif finish == "tool_calls":
                finish = "stop"
            save_session(session, "".join(sent))  # prima della fine dello stream: il client può subito riprendere
            yield chunk({}, finish or "stop", extra=stats.fields())
            if include_usage and stats.usage():
                yield sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                           "choices": [], **stats.fields()})
            yield DONE
            return

        stats.end_round()

        # tool lato proxy: avvisa in chat, esegui, poi nuovo giro nello stesso stream
        yield status_chunk(tool_status_text(server))
        tools_done += len(server)
        task = asyncio.create_task(execute_server_tools(
            body, session, assistant_tool_message("".join(text_parts), server, "".join(thought_parts)), server))
        while True:  # keepalive SSE mentre il tool lavora (evita timeout lato client/reverse proxy)
            done, _ = await asyncio.wait({task}, timeout=KEEPALIVE_SECONDS)
            if done:
                break
            yield ": keepalive\n\n"
        if task.exception():
            log.exception("errore nell'esecuzione dei tool", exc_info=task.exception())
            yield error(f"errore nell'esecuzione dei tool: {task.exception()}")
            return
        repeated = repeats.add(server, task.result())
        if SEND_CHAT_MARKDOWN:
            boxes = "\n".join(tool_box(c, r) for c, r in zip(server, task.result()))
            yield chunk({"content": ("\n\n" if need_sep else "") + boxes + "\n\n"})
            need_sep = False


# ====================================================================== tutto il resto
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def passthrough(path: str, req: Request):
    http: httpx.AsyncClient = state["http"]
    upstream = http.build_request(req.method, "/" + path, params=req.query_params, content=await req.body(),
                                  headers={"content-type": req.headers.get("content-type", "application/json")})
    r = await http.send(upstream, stream=True)
    return StreamingResponse(r.aiter_raw(), status_code=r.status_code, media_type=r.headers.get("content-type"),
                             background=BackgroundTask(r.aclose))


if __name__ == "__main__":
    import uvicorn

    for old in ("TOOL_STATUS", "REASONING_FORMAT"):
        if os.getenv(old):
            log.warning("%s non è più usata: ora l'avviso 'Uso \"...\"' è sempre attivo e "
                        "<think>/box dei tool si attivano con SEND_CHAT_MARKDOWN=true", old)
    if not PROXY_API_KEYS:
        log.warning("PROXY_API_KEYS non impostata: il proxy accetta richieste da chiunque lo raggiunga")
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT, proxy_headers=True, forwarded_allow_ips=FORWARDED_ALLOW_IPS)
