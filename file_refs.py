"""
file_refs.py - contenuti dei file aperti dai tool sostituiti dal loro percorso.

Un tool "apre un file" quando il server MCP lo marca in sola lettura e ha un argomento con il percorso
(percorso, path, file, sorgente, url...), oppure quando il suo risultato contiene immagini di cui si
conosce il percorso o risorse con un indirizzo. Il contenuto resta intero per gli ultimi FILE_KEEP_LAST
file aperti; i precedenti, le copie di una stessa chiamata ripetuta e, durante la compattazione, tutti i
file aperti diventano il loro percorso: il modello li riapre se gli servono di nuovo.
I risultati dell'ultimo giro restano sempre interi (il modello non li ha ancora letti).

Le sostituzioni modificano solo ciò che va al modello: il contenuto originale viene registrato in
`originals` (id del messaggio -> contenuto) e il database history salva quello. Vengono ricalcolate allo
stesso modo a ogni richiesta, così il prompt resta stabile tra un turno e l'altro.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field

log = logging.getLogger("file_refs")

KEEP_LAST = int(os.getenv("FILE_KEEP_LAST", "3"))  # -1 = funzione disattivata
MIN_CHARS = 900  # circa 300 token: i risultati più brevi restano interi
MEDIA_MESSAGE_PREFIX = "[Messaggio automatico del proxy]"

# Argomenti che contengono il percorso, in ordine di preferenza
PATH_ARGS = (re.compile(r"percorso|path", re.I),
             re.compile(r"^(file|filename|file_name|nome_file|sorgente|source)$", re.I),
             re.compile(r"^(url|uri|link)$", re.I))
MARKER_RE = re.compile(r"\[(immagine \d+|audio \d+)(?:: ([^\]\n]+))?\]")
LABEL_RE = re.compile(r"^\[(immagine \d+|audio \d+)(?:: (.+))?\]$")
RESOURCE_RE = re.compile(r"^\[risorsa (\S+)", re.M)


def path_args(properties) -> list[str]:
    """Argomenti di un tool che contengono il percorso del file, in ordine di preferenza."""
    names = list(properties or [])
    return [n for rx in PATH_ARGS for n in names if rx.search(n)]


def _is_media_message(m: dict) -> bool:
    c = m.get("content")
    return (m.get("role") == "user" and isinstance(c, list) and bool(c)
            and str(c[0].get("text", "")).startswith(MEDIA_MESSAGE_PREFIX))


@dataclass
class Opening:
    msg: dict  # messaggio del tool
    name: str
    key: str  # tool + argomenti: due aperture con la stessa chiave sono la stessa chiamata
    location: str
    round: int
    labels: dict = field(default_factory=dict)  # "immagine 1" -> percorso (o None)
    media: list = field(default_factory=list)  # messaggi con le immagini e gli audio del giro


def find_openings(conv: list[dict], file_tools: dict[str, list[str]]) -> tuple[list[Opening], int]:
    """File aperti nella conversazione, in ordine, e numero dell'ultimo giro di tool."""
    calls, openings, rnd = {}, [], 0
    for m in conv:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            rnd += 1
            for c in m["tool_calls"]:
                f = c.get("function") or {}
                try:
                    args = json.loads(f.get("arguments") or "{}") if isinstance(f.get("arguments"), str) \
                        else dict(f.get("arguments") or {})
                except (ValueError, TypeError):
                    args = {}
                calls[c.get("id")] = (f.get("name"), args if isinstance(args, dict) else {}, rnd)
        elif role == "tool":
            name, args, r = calls.get(m.get("tool_call_id"), (None, {}, rnd))
            text = m.get("content") if isinstance(m.get("content"), str) else ""
            if not name or text.startswith("ERRORE"):
                continue
            labels = dict(MARKER_RE.findall(text))
            loc = next((args[a].strip() for a in file_tools.get(name, ())
                        if isinstance(args.get(a), str) and args[a].strip()), None)
            loc = loc or next((w for w in labels.values() if w), None)
            if loc is None and (res := RESOURCE_RE.search(text)):
                loc = res.group(1)
            if loc:
                key = json.dumps([name, args], sort_keys=True, ensure_ascii=False)
                openings.append(Opening(m, name, key, loc, r, {k: v or None for k, v in labels.items()}))
        elif _is_media_message(m):
            for o in openings:
                if o.round == rnd and o.labels:
                    o.media.append(m)
    return openings, rnd


def _set(msg: dict, content, originals: dict):
    originals.setdefault(id(msg), msg.get("content"))
    msg["content"] = content


def detach(o: Opening, originals: dict) -> bool:
    """Sostituisce il contenuto di un file aperto con il suo percorso. True se qualcosa è cambiato."""
    changed = False
    text = o.msg.get("content") or ""
    if len(text) >= MIN_CHARS:
        refs = "".join(f"\n[{k}: {v or o.location}]" for k, v in o.labels.items())
        _set(o.msg, f"[Contenuto di {o.location} tolto per risparmiare contesto: se ti serve di nuovo, "
                    f"riaprilo ripetendo la chiamata {o.name}.]{refs}", originals)
        changed = True
    for mm in o.media:
        content = mm["content"]
        if not isinstance(content, list):
            continue
        kept, i = [], 0
        while i < len(content):
            p = content[i]
            label = LABEL_RE.match(p.get("text", "")) if p.get("type") == "text" else None
            nxt = content[i + 1] if i + 1 < len(content) else {}
            if label and label.group(1) in o.labels and nxt.get("type") in ("image_url", "input_audio"):
                where = label.group(2) or o.labels[label.group(1)] or o.location
                done = "tolto per risparmiare contesto: se ti serve di nuovo, riaprilo" \
                    if label.group(1).startswith("audio") else \
                    "tolta per risparmiare contesto: se ti serve di nuovo, riaprila"
                kept.append({"type": "text", "text": f"[{label.group(1)}: {where}, {done} dal percorso]"})
                i += 2  # etichetta e immagine/audio
                continue
            kept.append(p)
            i += 1
        if kept != content:
            new = "\n".join(p["text"] for p in kept) if all(p.get("type") == "text" for p in kept) else kept
            _set(mm, new, originals)
            changed = True
    return changed


def detach_files(conv: list[dict], file_tools: dict[str, list[str]], originals: dict,
                 everything: bool = False, keep_last: int = KEEP_LAST) -> int:
    """Applica le regole a `conv` (modificando i messaggi). everything=True: tutti i file aperti tranne
    quelli dell'ultimo giro (compattazione). Restituisce il numero di file sostituiti."""
    if keep_last < 0:
        return 0
    openings, last_round = find_openings(conv, file_tools)
    latest = {o.key: o for o in openings}
    distinct = [o for o in openings if latest[o.key] is o]
    victims = [o for o in openings if latest[o.key] is not o]  # stessa chiamata ripetuta: vale l'ultima
    victims += distinct if everything else distinct[:max(len(distinct) - keep_last, 0)]
    n = sum(detach(o, originals) for o in victims if o.round != last_round)
    if n:
        log.info("%d file aperti sostituiti dal loro percorso%s", n, " (compattazione)" if everything else "")
    return n
