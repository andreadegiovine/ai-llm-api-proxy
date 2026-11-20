"""
progress.py - frasi di attesa mentre la risposta non mostra testo.

Quando passano PROGRESS_FIRST_SECONDS secondi senza testo visibile (testo del modello, annunci dei tool),
il proxy scrive in chat una frase come "Sto ragionando..." o "Ancora qualche secondo...", poi una ogni
PROGRESS_EVERY_SECONDS finché il silenzio continua. I ragionamenti non contano come testo visibile:
non vengono letti ad alta voce dagli assistenti vocali.

Le frasi sono raggruppate per fase (attesa, risultati, ragionamento, preparazione, strumento) più un
gruppo di frasi generiche, nel file progress_phrases.json (se manca, progress_phrases.example.json).
Non vengono salvate nella risposta del modello: il modello non le vede nei turni successivi.
"""

import json
import logging
import os
import random
import time
from pathlib import Path

log = logging.getLogger("progress")

FIRST_SECONDS = float(os.getenv("PROGRESS_FIRST_SECONDS", "10"))  # 0 = disattivate
EVERY_SECONDS = float(os.getenv("PROGRESS_EVERY_SECONDS", "20"))
HERE = Path(__file__).resolve().parent
PHASES = ("attesa", "risultati", "ragionamento", "preparazione", "strumento", "generiche")


def load_phrases(path: str | None = None) -> dict[str, list[str]]:
    """Frasi per fase. path vuoto: progress_phrases.json nella cartella del proxy, altrimenti l'esempio."""
    if path:
        file = Path(path)
    else:
        file = HERE / "progress_phrases.json"
        if not file.exists():
            file = HERE / "progress_phrases.example.json"
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.error("frasi di attesa non leggibili da %s: %s", file, e)
        return {}
    phrases = {k: [str(p).strip() for p in v if str(p).strip()] for k, v in data.items()
               if k in PHASES and isinstance(v, list)}
    log.info("frasi di attesa: %s (%d frasi)", file, sum(len(v) for v in phrases.values()))
    return phrases


class Progress:
    """Timer del silenzio di una risposta e scelta delle frasi (mai la stessa due volte di seguito,
    preferendo quelle non ancora usate nella risposta)."""

    def __init__(self, phrases: dict[str, list[str]], first: float = FIRST_SECONDS, every: float = EVERY_SECONDS):
        self.phrases, self.first, self.every = phrases, first, max(every, 1.0)
        self.enabled = first > 0 and any(phrases.values())
        self.last = time.monotonic()
        self.count = 0  # frasi dall'ultimo testo visibile
        self.used: set[str] = set()
        self.previous: str | None = None

    def visible(self):
        """È stato inviato testo visibile: il silenzio riparte da zero."""
        self.last, self.count = time.monotonic(), 0

    def wait(self) -> float:
        """Secondi che mancano alla prossima frase."""
        if not self.enabled:
            return float("inf")
        return max(self.last + (self.first if self.count == 0 else self.every) - time.monotonic(), 0.0)

    def due(self, phase: str) -> str | None:
        """La frase da scrivere ora, oppure None se non è ancora il momento."""
        if not self.enabled or self.wait() > 0.05:
            return None
        own, generic = self.phrases.get(phase, []), self.phrases.get("generiche", [])
        pool = list(dict.fromkeys(own + generic))
        # prima le frasi della fase non ancora usate, poi le generiche, poi qualunque purché diversa dall'ultima
        choices = ([p for p in own if p not in self.used and p != self.previous]
                   or [p for p in generic if p not in self.used and p != self.previous]
                   or [p for p in pool if p != self.previous] or pool)
        if not choices:  # nessuna frase per questa fase: si riprova al prossimo intervallo
            self.last = time.monotonic()
            return None
        phrase = random.choice(choices)
        self.used.add(phrase)
        self.previous = phrase
        self.last, self.count = time.monotonic(), self.count + 1
        return phrase
