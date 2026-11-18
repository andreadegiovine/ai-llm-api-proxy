"""
compact_tool.py - compattazione della memoria per il proxy llama.cpp.

- Legge n_ctx da llama-server a runtime (GET /props, fallback GET /slots), con cache a tempo:
  se cambi modello o contesto non serve riavviare il proxy.
- Conta i token del prompt (POST /v1/chat/completions/input_tokens, fallback
  /apply-template + /tokenize, fallback stima sui caratteri).
- Quando il prompt si avvicina al contesto massimo, chiede allo stesso modello di
  riassumere i messaggi più vecchi e li sostituisce con il riassunto.
- Espone il tool "compact_memory", che il modello può chiamare di sua iniziativa.
- Tiene in cache i riassunti per prefisso di conversazione. I client reinviano tutta la
  cronologia a ogni turno: senza cache il riassunto verrebbe ricalcolato ogni volta.
"""

import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict

import httpx

log = logging.getLogger("compact")

THRESHOLD = float(os.getenv("COMPACT_THRESHOLD", "0.80"))        # compatta oltre l'80% di n_ctx
TARGET = float(os.getenv("COMPACT_TARGET", "0.50"))              # dopo la compattazione resta sotto il 50%
SUMMARY_TOKENS = int(os.getenv("COMPACT_SUMMARY_TOKENS", "1024"))  # lunghezza massima del riassunto
RESERVE_TOKENS = int(os.getenv("COMPACT_RESERVE_TOKENS", "2048"))  # spazio riservato alla risposta
TOOL_RESULT_RATIO = float(os.getenv("MAX_TOOL_RESULT_RATIO", "0.25"))  # risultato di un tool: max 25% di n_ctx
DEFAULT_N_CTX = int(os.getenv("DEFAULT_N_CTX", "8192"))          # usato solo se llama.cpp non risponde
N_CTX_TTL = int(os.getenv("N_CTX_TTL", "60"))                    # secondi di cache di n_ctx
CACHE_SIZE = int(os.getenv("COMPACT_CACHE_SIZE", "256"))

TOOL_NAME = "compact_memory"
TOOL_TITLE = "Compattazione della memoria"  # mostrato in chat: "Uso Compattazione della memoria..."

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "Compatta la memoria della conversazione: riassume i messaggi più vecchi per liberare "
            "spazio nel contesto. Usalo quando la conversazione è molto lunga, quando i risultati "
            "dei tool occupano molto spazio, o quando l'utente chiede di fare spazio/ripartire "
            "mantenendo il filo del discorso."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": "Informazioni da conservare con priorità nel riassunto (opzionale).",
                }
            },
        },
    },
}

SUMMARY_PROMPT = """Sei il modulo di compattazione della memoria di un assistente.
Ricevi un eventuale riassunto precedente e una porzione di conversazione.
Produci UN SOLO riassunto aggiornato che sostituirà quei messaggi.

Conserva:
- obiettivi, richieste e preferenze dell'utente;
- decisioni prese e motivazioni;
- fatti concreti: nomi, numeri, date, percorsi, comandi, configurazioni, codice rilevante;
- risultati importanti dei tool;
- questioni aperte e l'ULTIMA richiesta dell'utente ancora da soddisfare (indicala esplicitamente).

Elimina convenevoli e ripetizioni. Usa elenchi puntati e la lingua della conversazione.
Rispondi SOLO con il riassunto, senza introduzioni."""

SUMMARY_HEADER = "## Riassunto della conversazione precedente (memoria compattata)"
LABELS = {"user": "UTENTE", "assistant": "ASSISTENTE", "tool": "RISULTATO TOOL", "system": "SISTEMA"}


def msg_text(m: dict) -> str:
    """Testo di un messaggio OpenAI (gestisce content a parti e tool_calls)."""
    c = m.get("content")
    if isinstance(c, list):
        c = "\n".join(
            p.get("text", "") if p.get("type") == "text" else f"[{p.get('type', 'allegato')}]" for p in c
        )
    text = c or ""
    for tc in m.get("tool_calls") or []:
        f = tc.get("function") or {}
        text += f"\n[chiamata tool {f.get('name')}({f.get('arguments')})]"
    return text


def _prefix_hashes(conv: list[dict]) -> list[str]:
    """out[i] = hash dei messaggi conv[0..i]. Considera solo ruolo e testo (gli id cambiano)."""
    h, out = hashlib.sha256(), []
    for m in conv:
        h.update(json.dumps([m.get("role"), msg_text(m).strip()], ensure_ascii=False).encode())
        h.update(b"\x00")
        out.append(h.copy().hexdigest())
    return out


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return f"{text[:half]}\n[... {len(text) - max_chars} caratteri omessi ...]\n{text[-half:]}"


class Compactor:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self._nctx: dict[str, tuple[int, float]] = {}
        self._summaries: OrderedDict[str, str] = OrderedDict()
        self._toklen: OrderedDict[str, int] = OrderedDict()
        self._input_tokens_ok: bool | None = None
        self._tokenize_ok: bool | None = None

    # ------------------------------------------------------------------ info dal server
    async def n_ctx(self, model: str | None = None) -> int:
        key = model or ""
        hit = self._nctx.get(key)
        if hit and time.time() - hit[1] < N_CTX_TTL:
            return hit[0]
        params = {"model": model} if model else None
        val = None
        try:
            r = await self.http.get("/props", params=params)
            if r.status_code == 200:
                val = (r.json().get("default_generation_settings") or {}).get("n_ctx")
            if not val:
                r = await self.http.get("/slots", params=params)
                if r.status_code == 200 and isinstance(r.json(), list) and r.json():
                    val = r.json()[0].get("n_ctx")
        except (httpx.HTTPError, ValueError) as e:
            log.warning("impossibile leggere n_ctx: %s", e)
        if not val:
            val = hit[0] if hit else DEFAULT_N_CTX
        val = int(val)
        if not hit or hit[0] != val:
            log.info("n_ctx per modello '%s': %d", key or "default", val)
        self._nctx[key] = (val, time.time())
        return val

    async def tok_len(self, text: str, model: str | None = None) -> int:
        key = hashlib.sha1(text.encode()).hexdigest()
        if key in self._toklen:
            return self._toklen[key]
        n = None
        if self._tokenize_ok is not False:
            try:
                payload = {"content": text, **({"model": model} if model else {})}
                r = await self.http.post("/tokenize", json=payload)
                if r.status_code == 200:
                    n, self._tokenize_ok = len(r.json()["tokens"]), True
                elif r.status_code == 404:
                    self._tokenize_ok = False
            except (httpx.HTTPError, ValueError, KeyError):
                pass
        if n is None:
            n = len(text) // 3 + 1
        self._toklen[key] = n
        if len(self._toklen) > 4096:
            self._toklen.popitem(last=False)
        return n

    async def count_tokens(self, messages, tools=None, model=None) -> int:
        payload = {"messages": messages, **({"tools": tools} if tools else {}), **({"model": model} if model else {})}
        if self._input_tokens_ok is not False:
            try:
                r = await self.http.post("/v1/chat/completions/input_tokens", json=payload)
                if r.status_code == 200 and "input_tokens" in r.json():
                    self._input_tokens_ok = True
                    return int(r.json()["input_tokens"])
                if r.status_code in (404, 405):
                    self._input_tokens_ok = False
            except (httpx.HTTPError, ValueError):
                pass
        try:
            r = await self.http.post("/apply-template", json={k: v for k, v in payload.items() if k != "tools"})
            if r.status_code == 200:
                n = await self.tok_len(r.json()["prompt"], model)
                if tools:
                    n += await self.tok_len(json.dumps(tools, ensure_ascii=False), model)
                return n
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        return len(json.dumps(payload, ensure_ascii=False)) // 3

    # ------------------------------------------------------------------ API usata dal proxy
    async def cap_tool_result(self, text: str, model: str | None = None) -> str:
        """Tronca i risultati dei tool troppo grandi per il contesto."""
        max_chars = int(await self.n_ctx(model) * TOOL_RESULT_RATIO * 3)
        return _truncate(text, max_chars)

    async def prepare(self, messages, tools=None, model=None, reserve=None, force=False, focus=None):
        """
        Ritorna (messages, info).
        1) applica il riassunto in cache del prefisso più lungo già compattato;
        2) se il prompt supera la soglia (o force=True), riassume i messaggi vecchi.
        In caso di errore restituisce i messaggi senza compattarli: la chat non si blocca.
        """
        n_ctx = await self.n_ctx(model)
        reserve = min(int(reserve or RESERVE_TOKENS), n_ctx // 4)
        limit = int(n_ctx * THRESHOLD) - reserve

        n_head = 0
        while n_head < len(messages) and messages[n_head].get("role") == "system":
            n_head += 1
        head, conv = messages[:n_head], messages[n_head:]
        hashes = _prefix_hashes(conv)

        k, summary = 0, None
        for i in range(len(conv) - 1, -1, -1):
            if hashes[i] in self._summaries:
                k, summary = i + 1, self._summaries[hashes[i]]
                self._summaries.move_to_end(hashes[i])
                break

        working = self._build(head, summary, conv[k:])
        before = await self.count_tokens(working, tools, model)
        info = {"compacted": False, "n_ctx": n_ctx, "before": before, "after": before, "summarized": 0,
                "cached_prefix": k}
        if not force and before <= limit:
            return working, info

        head_tokens = await self.tok_len(msg_text(head[0]), model) if head else 0
        tail_budget = max(int(n_ctx * TARGET) - reserve - SUMMARY_TOKENS - head_tokens, 256)
        b = await self._choose_boundary(conv, k, tail_budget, model)
        if b <= k:
            log.warning("niente da compattare: i messaggi recenti da soli superano il budget (%d token)", before)
            return working, info

        try:
            t0 = time.time()
            summary = await self._summarize(summary, conv[k:b], n_ctx, model, focus)
        except Exception as e:  # noqa: BLE001
            log.error("compattazione fallita: %s", e)
            info["error"] = str(e)
            return working, info

        self._summaries[hashes[b - 1]] = summary
        while len(self._summaries) > CACHE_SIZE:
            self._summaries.popitem(last=False)

        working = self._build(head, summary, conv[b:])
        after = await self.count_tokens(working, tools, model)
        info.update(compacted=True, after=after, summarized=b - k)
        log.info("memoria compattata: %d messaggi riassunti, %d -> %d token (n_ctx %d) in %.1fs",
                 b - k, before, after, n_ctx, time.time() - t0)
        return working, info

    @staticmethod
    def describe(info: dict) -> str:
        if info.get("error"):
            return f"Compattazione non riuscita: {info['error']}"
        if not info.get("compacted"):
            return (f"Nessuna compattazione necessaria o possibile: contesto attuale {info['before']} "
                    f"token su {info['n_ctx']}.")
        return (f"Memoria compattata (messaggi riassunti: {info['summarized']}): contesto da {info['before']} "
                f"a {info['after']} token su {info['n_ctx']}. Il riassunto è nel messaggio di sistema.")

    # ------------------------------------------------------------------ interni
    def _build(self, head, summary, tail):
        if summary is None:
            return head + tail
        block = f"{SUMMARY_HEADER}\n{summary}"
        if head:
            first = {**head[0], "content": f"{msg_text(head[0])}\n\n{block}"}
            new_head = [first] + head[1:]
        else:
            new_head = [{"role": "system", "content": block}]
        # molti template vogliono che dopo il system arrivi un messaggio user
        if tail and tail[0].get("role") != "user":
            tail = [{"role": "user", "content": "(La parte precedente della conversazione è riassunta "
                                                "nel messaggio di sistema. Continua.)"}] + tail
        return new_head + tail

    async def _choose_boundary(self, conv, k, budget, model) -> int:
        """Indice b: conv[b:] resta intatto (messaggi recenti), conv[k:b] viene riassunto."""
        used, b = 0, len(conv)
        for i in range(len(conv) - 1, k - 1, -1):
            t = await self.tok_len(msg_text(conv[i]), model) + 8
            if used + t > budget and b < len(conv):  # l'ultimo messaggio resta sempre
                break
            used, b = used + t, i
        # non separare un risultato di tool dalla chiamata che lo ha prodotto
        while k < b < len(conv) and conv[b].get("role") == "tool":
            b -= 1
        return b

    async def _summarize(self, prev, msgs, n_ctx, model, focus) -> str:
        chunk_budget = max(int(n_ctx * 0.6) - SUMMARY_TOKENS - 600, 512)
        max_chars = chunk_budget * 3
        chunks, cur, cur_tok = [], [], 0
        for m in msgs:
            line = f"{LABELS.get(m.get('role'), str(m.get('role')).upper())}: {_truncate(msg_text(m), max_chars)}"
            t = await self.tok_len(line, model)
            if cur and cur_tok + t > chunk_budget:
                chunks.append(cur)
                cur, cur_tok = [], 0
            cur.append(line)
            cur_tok += t
        if cur:
            chunks.append(cur)

        summary = prev
        for chunk in chunks:  # riassunto "a cascata": ogni blocco aggiorna il precedente
            summary = await self._summarize_chunk(summary, "\n\n".join(chunk), model, focus)
        return summary

    async def _summarize_chunk(self, prev, text, model, focus) -> str:
        user = f"RIASSUNTO PRECEDENTE:\n{prev}\n\n" if prev else ""
        user += f"MESSAGGI DA INTEGRARE:\n{text}"
        if focus:
            user += f"\n\nPRIORITÀ: conserva in particolare: {focus}"
        payload = {
            "messages": [{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": user}],
            "max_tokens": SUMMARY_TOKENS,
            "temperature": 0.2,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},  # modelli "thinking" (es. Qwen3)
        }
        if model:
            payload["model"] = model
        r = await self.http.post("/v1/chat/completions", json=payload)
        r.raise_for_status()
        out = r.json()["choices"][0]["message"].get("content") or ""
        out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()
        if not out:
            raise RuntimeError("il modello ha restituito un riassunto vuoto")
        return out
