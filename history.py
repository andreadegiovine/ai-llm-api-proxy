"""
history.py - database "history" delle conversazioni del proxy (SQLite).

Il client (es. Open WebUI) conserva solo il testo visibile delle risposte: le tool call, i loro risultati
e i ragionamenti restano al proxy. Questo modulo li salva, così quando il client riprende una
conversazione il modello ritrova l'intero scambio, come se usasse llama.cpp direttamente.

- Ogni nuova conversazione riceve un UUID.
- `messages`: l'intero scambio in ordine (utente, risposte, ragionamenti, tool call, risultati dei tool,
  immagini dei tool) e i riassunti della compattazione (role = "summary", con `covers_until`: riassume i
  messaggi con seq < covers_until).
- `turns`: per ogni turno, l'impronta della sequenza dei messaggi dell'utente fino a quel turno
  (`user_chain`). La conversazione si riconosce dai messaggi dell'utente, che il proxy non modifica mai e
  che il client rimanda identici; le risposte dell'assistente si prendono sempre dal database, mai dal
  client. Con l'id della chat inviato dal client (es. X-OpenWebUI-Chat-Id) il riconoscimento è esatto.
- Rigenerazione o modifica di un messaggio: i messaggi fino al punto in cui la conversazione diverge
  vengono copiati su una nuova conversazione (nuovo UUID), così il ramo originale resta intatto.
- Ogni conversazione ha una data di inizio (`started_at`), scritta nel system prompt; un ramo eredita
  quella della conversazione da cui nasce.
"""

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from typing import Callable

log = logging.getLogger("history")

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    client_chat_id TEXT,
    parent_id TEXT,
    forked_at_turn INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL
);
CREATE INDEX IF NOT EXISTS conversations_client_chat ON conversations(client_chat_id);

CREATE TABLE IF NOT EXISTS messages (
    conversation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    turn INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    reasoning_content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    covers_until INTEGER,
    created_at REAL NOT NULL,
    PRIMARY KEY (conversation_id, seq)
);

CREATE TABLE IF NOT EXISTS turns (
    conversation_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    visible_hash TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    user_chain TEXT,
    user_count INTEGER,
    PRIMARY KEY (conversation_id, turn)
);
"""


def user_chain(keys: list[str]) -> str:
    """Impronta della sequenza dei messaggi dell'utente (già normalizzati) fino a un turno."""
    return hashlib.sha256(json.dumps(keys, ensure_ascii=False).encode()).hexdigest()


def _row_to_message(r: sqlite3.Row) -> dict:
    msg = {"role": r["role"], "content": json.loads(r["content"]) if r["content"] is not None else None}
    if r["reasoning_content"]:
        msg["reasoning_content"] = r["reasoning_content"]
    if r["tool_calls"]:
        msg["tool_calls"] = json.loads(r["tool_calls"])
    if r["tool_call_id"]:
        msg["tool_call_id"] = r["tool_call_id"]
    return msg


class Session:
    """Stato della conversazione durante una risposta: riassunto in vigore, messaggi successivi al
    riassunto (da inviare al modello) e righe nuove da salvare a fine risposta.
    Con db=None è una sessione senza memoria."""

    def __init__(self, db: "HistoryDB | None", conv_id: str | None, turn: int, rows: list[sqlite3.Row] | None = None,
                 messages: list[dict] | None = None, mode: str = "senza history", client_chat_id: str | None = None,
                 fork_from: tuple[str, int] | None = None, seed: list[dict] | None = None,
                 seed_chain: str | None = None, user_keys: list[str] | None = None,
                 started_at: float | None = None):
        self.db, self.conv_id, self.turn, self.mode = db, conv_id, turn, mode
        self.started_at = started_at or time.time()  # data di inizio della conversazione
        self.client_chat_id, self.fork_from = client_chat_id, fork_from
        self.seed, self.seed_chain = seed or [], seed_chain
        self.user_keys = user_keys or []  # messaggi dell'utente fino a questo turno compreso
        self.summary: str | None = None
        self.conv: list[dict] = []
        self.transient: list[dict] = []  # messaggi solo per il prossimo giro, mai salvati
        self.pending: list[dict] = []
        self.draft = ""  # testo della risposta in corso, salvato se il client si disconnette
        self.originals: dict[int, object] = {}  # id del messaggio -> contenuto originale (file sostituiti dal percorso)
        self._seq: dict[int, int] = {}
        self.next_seq = 0
        self.saved = False

        if rows:
            summaries = [r for r in rows if r["role"] == "summary"]
            covers = 0
            if summaries:
                self.summary, covers = summaries[-1]["content"], summaries[-1]["covers_until"]
            for r in rows:
                if r["role"] != "summary" and r["seq"] >= covers:
                    self._track(_row_to_message(r), r["seq"])
            self.next_seq = rows[-1]["seq"] + 1
        for i, m in enumerate(self.seed):  # conversazione mai vista: messaggi del client come punto di partenza
            self._track(m, i)
            self.next_seq = i + 1
        for m in messages or []:  # sessione senza history
            self._track(m, self.next_seq)
            self.next_seq += 1

    def _track(self, msg: dict, seq: int):
        self._seq[id(msg)] = seq
        self.conv.append(msg)

    def add(self, msg: dict):
        """Aggiunge un messaggio della risposta in corso (utente, assistant, tool, immagini dei tool)."""
        seq = self.next_seq
        self.next_seq += 1
        self._track(msg, seq)
        self.pending.append({"seq": seq, "msg": msg})
        if msg.get("role") == "assistant":
            self.draft = ""

    def original(self, msg: dict) -> dict:
        """Il messaggio con il contenuto originale, da salvare nel database."""
        return {**msg, "content": self.originals[id(msg)]} if id(msg) in self.originals else msg

    def apply_compaction(self, info: dict | None, working: list[dict]):
        """Dopo compactor.prepare: se c'è un nuovo riassunto, i messaggi che copre escono dalla sessione
        e il riassunto viene salvato come messaggio."""
        new = (info or {}).get("summary")
        if not new or new == self.summary:
            return
        boundary = next((self._seq[id(m)] for m in working if id(m) in self._seq), self.next_seq)
        self.conv = [m for m in self.conv if self._seq[id(m)] >= boundary]
        self.summary = new
        seq = self.next_seq
        self.next_seq += 1
        self.pending.append({"seq": seq, "summary": new, "covers_until": boundary})

    def save(self):
        if self.db is None or self.saved:
            return
        if self.draft.strip():  # risposta interrotta: il testo già inviato al client fa parte della conversazione
            self.add({"role": "assistant", "content": self.draft})
        self.db.save(self)
        self.saved = True


class HistoryDB:
    def __init__(self, path: str, retention_days: int = 0, user_key: Callable[[dict], str] | None = None):
        """user_key: normalizza un messaggio dell'utente come fa il proxy (serve ad aggiornare i database
        delle versioni precedenti)."""
        self.path = path
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate(user_key)
        self.db.execute("CREATE INDEX IF NOT EXISTS turns_user_chain ON turns(user_chain)")
        if retention_days > 0:
            self.purge(retention_days)
        n = self.db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        log.info("database history: %s (%d conversazioni)", path, n)

    def _migrate(self, user_key):
        """Aggiorna i database delle versioni precedenti: colonne nuove e impronta dei messaggi utente."""
        conv_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(conversations)")}
        if "started_at" not in conv_cols:
            self.db.execute("ALTER TABLE conversations ADD COLUMN started_at REAL")
        turn_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(turns)")}
        if "user_chain" not in turn_cols:
            self.db.execute("ALTER TABLE turns ADD COLUMN user_chain TEXT")
        if "user_count" not in turn_cols:
            self.db.execute("ALTER TABLE turns ADD COLUMN user_count INTEGER")
        missing = [r[0] for r in self.db.execute(
            "SELECT DISTINCT conversation_id FROM turns WHERE user_chain IS NULL OR user_count IS NULL")]
        if not missing or user_key is None:
            return
        for cid in missing:
            keys, chains, last_turn = [], {}, None
            for r in self.db.execute("SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq", (cid,)):
                if r["role"] == "user" and (r["turn"] == 0 or r["turn"] != last_turn):
                    keys.append(user_key(_row_to_message(r)))  # turno 0: tutti; poi il primo messaggio del turno
                    last_turn = r["turn"]
                chains[r["turn"]] = (user_chain(keys), len(keys))
            for turn, (chain, count) in chains.items():
                self.db.execute("UPDATE turns SET user_chain = ?, user_count = ? WHERE conversation_id = ? AND turn = ?",
                                (chain, count, cid, turn))
        log.info("database history: aggiornate %d conversazioni al nuovo riconoscimento", len(missing))

    def purge(self, days: int):
        limit = time.time() - days * 86400
        with self.lock:
            old = [r[0] for r in self.db.execute("SELECT id FROM conversations WHERE updated_at < ?", (limit,))]
            for cid in old:
                self.db.execute("DELETE FROM messages WHERE conversation_id = ?", (cid,))
                self.db.execute("DELETE FROM turns WHERE conversation_id = ?", (cid,))
                self.db.execute("DELETE FROM conversations WHERE id = ?", (cid,))
        if old:
            log.info("database history: eliminate %d conversazioni più vecchie di %d giorni", len(old), days)

    def _find(self, chain: str, client_chat_id: str | None):
        query = ("SELECT t.conversation_id AS cid, t.turn AS turn, c.updated_at AS updated, "
                 "COALESCE(c.started_at, c.created_at) AS started_at, "
                 "(SELECT MAX(turn) FROM turns x WHERE x.conversation_id = t.conversation_id) AS last_turn "
                 "FROM turns t JOIN conversations c ON c.id = t.conversation_id WHERE t.user_chain = ?")
        args: tuple = (chain,)
        if client_chat_id:
            query += " AND c.client_chat_id = ?"
            args += (client_chat_id,)
        rows = self.db.execute(query, args).fetchall()
        if not rows:
            return None
        # preferisci la conversazione in cui il turno trovato è l'ultimo (nessun ramo da creare), poi la più recente
        rows.sort(key=lambda r: (r["turn"] == r["last_turn"], r["updated"]), reverse=True)
        return rows[0]

    def _find_by_chat(self, client_chat_id: str, user_count: int):
        """Ripiego con l'id della chat: il turno con lo stesso numero di messaggi utente, anche se il testo
        non coincide (es. il client ha arricchito l'ultimo messaggio solo in quella richiesta)."""
        rows = self.db.execute(
            "SELECT t.conversation_id AS cid, t.turn AS turn, c.updated_at AS updated, "
            "COALESCE(c.started_at, c.created_at) AS started_at, "
            "(SELECT MAX(turn) FROM turns x WHERE x.conversation_id = t.conversation_id) AS last_turn "
            "FROM turns t JOIN conversations c ON c.id = t.conversation_id "
            "WHERE c.client_chat_id = ? AND t.user_count = ?", (client_chat_id, user_count)).fetchall()
        if not rows:
            return None
        rows.sort(key=lambda r: (r["turn"] == r["last_turn"], r["updated"]), reverse=True)
        return rows[0]

    def open(self, user_keys: list[str], prefix_messages: list[dict], user_msg: dict,
             client_chat_id: str | None) -> Session:
        """Trova (o crea) la conversazione a cui appartiene la richiesta e aggiunge il nuovo messaggio utente.
        user_keys: i messaggi dell'utente del client, normalizzati, compreso quello nuovo."""
        prefix = user_keys[:-1]
        chain = user_chain(prefix)
        with self.lock:
            found = self._find(chain, client_chat_id) if prefix else None
            if not found and prefix and client_chat_id:
                found = self._find_by_chat(client_chat_id, len(prefix))
                if found:
                    log.warning("conversazione %s riconosciuta solo dall'id della chat %s: i messaggi utente inviati "
                                "dal client non coincidono con quelli salvati", found["cid"], client_chat_id)
            if found:
                rows = self.db.execute("SELECT * FROM messages WHERE conversation_id = ? AND turn <= ? ORDER BY seq",
                                       (found["cid"], found["turn"])).fetchall()
        if found and found["turn"] == found["last_turn"]:
            s = Session(self, found["cid"], found["turn"] + 1, rows=rows, mode="ripresa",
                        client_chat_id=client_chat_id, user_keys=user_keys, started_at=found["started_at"])
        elif found:  # rigenerazione o modifica: ramo con i messaggi fino a quel punto, su un nuovo UUID
            s = Session(self, str(uuid.uuid4()), found["turn"] + 1, rows=rows,
                        mode=f"ramo di {found['cid']} dal turno {found['turn']}", client_chat_id=client_chat_id,
                        fork_from=(found["cid"], found["turn"]), user_keys=user_keys,
                        started_at=found["started_at"])  # il ramo mantiene la data di inizio dell'originale
        else:  # nuova conversazione; se il client ha già dei messaggi, sono il punto di partenza
            s = Session(self, str(uuid.uuid4()), 1, mode="nuova" if not prefix else "nuova (da cronologia del client)",
                        client_chat_id=client_chat_id, seed=[dict(m) for m in prefix_messages],
                        seed_chain=chain if prefix else None, user_keys=user_keys)
        s.add(user_msg)
        log.info("conversazione %s: %s, turno %d", s.conv_id, s.mode, s.turn)
        return s

    def save(self, s: Session):
        now = time.time()
        with self.lock:
            self.db.execute("BEGIN")
            try:
                exists = self.db.execute("SELECT 1 FROM conversations WHERE id = ?", (s.conv_id,)).fetchone()
                if not exists:
                    parent, fork_turn = s.fork_from or (None, None)
                    self.db.execute("INSERT INTO conversations (id, client_chat_id, parent_id, forked_at_turn, created_at, "
                                    "updated_at, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (s.conv_id, s.client_chat_id, parent, fork_turn, now, now, s.started_at))
                    if s.fork_from:
                        self.db.execute(
                            "INSERT INTO messages SELECT ?, seq, turn, role, content, reasoning_content, tool_calls, "
                            "tool_call_id, covers_until, created_at FROM messages WHERE conversation_id = ? AND turn <= ?",
                            (s.conv_id, parent, fork_turn))
                        self.db.execute(
                            "INSERT INTO turns (conversation_id, turn, visible_hash, created_at, user_chain, user_count) "
                            "SELECT ?, turn, visible_hash, created_at, user_chain, user_count FROM turns "
                            "WHERE conversation_id = ? AND turn <= ?", (s.conv_id, parent, fork_turn))
                    for i, m in enumerate(s.seed):
                        self._insert_message(s.conv_id, i, 0, s.original(m), now)
                    if s.seed_chain:
                        self.db.execute("INSERT INTO turns (conversation_id, turn, visible_hash, created_at, user_chain, "
                                        "user_count) VALUES (?, 0, '', ?, ?, ?)",
                                        (s.conv_id, now, s.seed_chain, len(s.user_keys) - 1))
                else:
                    self.db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, s.conv_id))
                for p in s.pending:
                    if "summary" in p:
                        self.db.execute("INSERT INTO messages (conversation_id, seq, turn, role, content, covers_until, "
                                        "created_at) VALUES (?, ?, ?, 'summary', ?, ?, ?)",
                                        (s.conv_id, p["seq"], s.turn, p["summary"], p["covers_until"], now))
                    else:
                        self._insert_message(s.conv_id, p["seq"], s.turn, s.original(p["msg"]), now)
                self.db.execute("INSERT OR REPLACE INTO turns (conversation_id, turn, visible_hash, created_at, user_chain, "
                                "user_count) VALUES (?, ?, '', ?, ?, ?)",
                                (s.conv_id, s.turn, now, user_chain(s.user_keys), len(s.user_keys)))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def _insert_message(self, conv_id: str, seq: int, turn: int, m: dict, now: float):
        self.db.execute(
            "INSERT INTO messages (conversation_id, seq, turn, role, content, reasoning_content, tool_calls, "
            "tool_call_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (conv_id, seq, turn, m.get("role"), json.dumps(m.get("content"), ensure_ascii=False),
             m.get("reasoning_content") or None,
             json.dumps(m["tool_calls"], ensure_ascii=False) if m.get("tool_calls") else None,
             m.get("tool_call_id"), now))
