"""
settings.py - Impostazioni dell'app salvate in impostazioni.json.
Si modificano dall'app stessa (scheda "Altro"), senza aprire file a mano.
"""
import json
import os
import secrets
import threading
from pathlib import Path

# Cartella dei dati (database + impostazioni). Di norma è la cartella dell'app.
DATA_DIR = Path(os.environ.get("INVENTARIO_DATA") or Path(__file__).parent)
FILE = DATA_DIR / "impostazioni.json"
_lock = threading.Lock()

DEFAULTS = {
    "nome_negozio": "Gestione Surgelati & Alimentari",
    "ai_provider": "gemini",                 # "gemini" | "ollama"
    "gemini_api_key": "",
    "gemini_model": "auto",                  # "auto" = sceglie da solo un modello gratuito che risponde
    "ollama_url": "http://localhost:11434",
    "ollama_model": "qwen2.5vl:7b",
    "ricarico": 40.0,                        # percentuale di ricarico sul prezzo d'acquisto ivato
    "arrotonda": "0.01",                     # passo del prezzo al pubblico: 0.01 | 0.05 | 0.10
    "pin": "",                               # vuoto = nessun PIN
    "secret": "",                            # generato automaticamente, firma il cookie di accesso
}


def _scrivi(cfg: dict) -> None:
    FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def carica() -> dict:
    with _lock:
        cfg = dict(DEFAULTS)
        if FILE.exists():
            try:
                cfg.update(json.loads(FILE.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
        if not cfg["secret"]:
            cfg["secret"] = secrets.token_hex(32)
            _scrivi(cfg)
    if not cfg["gemini_api_key"] and os.environ.get("GEMINI_API_KEY"):
        cfg["gemini_api_key"] = os.environ["GEMINI_API_KEY"]
    if cfg["arrotonda"] not in ("0.01", "0.05", "0.10"):
        cfg["arrotonda"] = "0.01"
    return cfg


def salva(modifiche: dict) -> dict:
    cfg = carica()
    for k, v in modifiche.items():
        if k in DEFAULTS and k != "secret":
            cfg[k] = v
    with _lock:
        _scrivi(cfg)
    return cfg
