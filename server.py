"""
server.py - Backend dell'app (FastAPI) + interfaccia web in /static.

Avvio:  doppio clic su avvia.bat   (oppure: .venv\\Scripts\\python.exe server.py)
"""
import csv
import hashlib
import hmac
import io
import socket
import threading
import webbrowser
from datetime import date
from pathlib import Path

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import database as db
import settings
from ai_extractor import ErroreAI, estrai_fattura, prepara_immagine, prova_connessione

PORTA = 8000
STATIC = Path(__file__).parent / "static"
MAX_PAGINE = 6

db.init_db()
app = FastAPI(title="Inventario Negozio", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------- #
# Rete locale
# --------------------------------------------------------------------------- #
def ip_locale() -> str:
    """IP del PC sulla rete del negozio (nessun pacchetto viene davvero inviato)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        s.close()


def url_telefono() -> str:
    return f"http://{ip_locale()}:{PORTA}"


# --------------------------------------------------------------------------- #
# Accesso con PIN (facoltativo)
# --------------------------------------------------------------------------- #
def _token(cfg: dict) -> str:
    return hmac.new(cfg["secret"].encode(), cfg["pin"].encode(), hashlib.sha256).hexdigest()


def _imposta_cookie(resp: Response, cfg: dict) -> None:
    resp.set_cookie("auth", _token(cfg), max_age=60 * 60 * 24 * 365, httponly=True, samesite="strict")


def autenticato(request: Request) -> bool:
    cfg = settings.carica()
    return not cfg["pin"] or hmac.compare_digest(request.cookies.get("auth", ""), _token(cfg))


def richiedi_auth(request: Request):
    if not autenticato(request):
        raise HTTPException(401, "PIN richiesto")


class Login(BaseModel):
    pin: str


@app.get("/api/stato")
def stato(request: Request):
    cfg = settings.carica()
    return {"pin_richiesto": bool(cfg["pin"]), "autenticato": autenticato(request),
            "nome_negozio": cfg["nome_negozio"]}


@app.post("/api/login")
def login(dati: Login, response: Response):
    cfg = settings.carica()
    if not cfg["pin"] or hmac.compare_digest(dati.pin, cfg["pin"]):
        _imposta_cookie(response, cfg)
        return {"ok": True}
    raise HTTPException(401, "PIN errato")


api = APIRouter(prefix="/api", dependencies=[Depends(richiedi_auth)])


# --------------------------------------------------------------------------- #
# Prodotti e cartellini
# --------------------------------------------------------------------------- #
@api.get("/prodotti")
def prodotti():
    cfg = settings.carica()
    return {"prodotti": db.lista_prodotti(), "ricarico": cfg["ricarico"], "arrotonda": cfg["arrotonda"],
            "ai_pronta": cfg["ai_provider"] == "ollama" or bool(cfg["gemini_api_key"])}


@api.get("/prodotti/{prodotto_id}/storico")
def storico(prodotto_id: int):
    return db.storico_prodotto(prodotto_id)


class ModificaProdotto(BaseModel):
    nome: str | None = Field(default=None, max_length=80)
    unita: str | None = None
    categoria: str | None = None


@api.patch("/prodotti/{prodotto_id}")
def modifica(prodotto_id: int, dati: ModificaProdotto):
    try:
        return db.modifica_prodotto(prodotto_id, dati.nome, dati.unita, dati.categoria)
    except ValueError as e:
        raise HTTPException(404, str(e))


@api.delete("/prodotti/{prodotto_id}")
def elimina(prodotto_id: int):
    db.elimina_prodotto(prodotto_id)
    return {"ok": True}


class Cartellini(BaseModel):
    ids: list[int] | None = None      # None = tutti


@api.post("/cartellini/fatti")
def cartellini_fatti(dati: Cartellini):
    db.cartellini_fatti(dati.ids)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Fatture
# --------------------------------------------------------------------------- #
@api.post("/fattura/analizza")
def analizza(foto: list[UploadFile] = File(...)):
    cfg = settings.carica()
    if len(foto) > MAX_PAGINE:
        raise HTTPException(400, f"Massimo {MAX_PAGINE} pagine per fattura.")
    esistenti = {p["nome"].lower(): p for p in db.lista_prodotti()}
    try:
        immagini = [prepara_immagine(f.file.read())[0] for f in foto]
        risultato = estrai_fattura(immagini, cfg, [p["nome"] for p in esistenti.values()])
    except ErroreAI as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # rete, risposta inattesa
        raise HTTPException(502, f"Lettura non riuscita: {e}")
    for p in risultato["prodotti"]:
        if p["categoria"] not in db.CATEGORIE:
            p["categoria"] = "altro"
        gia = esistenti.get(p["nome_pulito"].lower())
        if gia:    # prodotto già noto: tieni la sua categoria
            p["categoria"] = gia["categoria"]
    return risultato


class Riga(BaseModel):
    nome: str = Field(min_length=1, max_length=80)
    unita: str = "pz"
    categoria: str | None = None
    prezzo: float = Field(gt=0, lt=100000)
    descrizione: str | None = None


class Documento(BaseModel):
    fornitore: str | None = None
    numero_documento: str | None = None
    data: str | None = None
    righe: list[Riga] = Field(min_length=1)


@api.post("/acquisti")
def salva_acquisti(doc: Documento):
    cfg = settings.carica()
    try:
        data_doc = date.fromisoformat(doc.data).isoformat() if doc.data else None
    except ValueError:
        data_doc = None
    return db.registra_documento([r.model_dump() for r in doc.righe],
                                 fornitore=(doc.fornitore or "").strip() or None,
                                 numero=(doc.numero_documento or "").strip() or None,
                                 data_doc=data_doc, ricarico_pct=cfg["ricarico"], passo=cfg["arrotonda"])


@api.get("/documenti")
def documenti():
    return db.lista_documenti()


@api.get("/documenti/{documento_id}")
def documento(documento_id: int):
    return db.righe_documento(documento_id)


@api.delete("/documenti/{documento_id}")
def elimina_documento(documento_id: int):
    db.elimina_documento(documento_id)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Esportazioni
# --------------------------------------------------------------------------- #
@api.get("/export/listino.csv")
def export_listino():
    """Listino per Excel italiano (separatore ; e virgola decimale)."""
    it = lambda v: "" if v is None else f"{v:.2f}".replace(".", ",")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Prodotto", "Categoria", "Unità", "Acquisto ivato", "Prezzo al pubblico",
                "Variazione ultima", "Aggiornato il"])
    for p in sorted(db.lista_prodotti(), key=lambda x: x["nome"].lower()):
        w.writerow([p["nome"], p["categoria"], p["unita"], it(p["prezzo_acquisto_ivato"]),
                    it(p["prezzo_vendita"]), it(p["variazione_ultima"]), p["aggiornato_il"]])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="listino.csv"'})


@api.get("/export/backup.db")
def export_backup():
    return FileResponse(db.DB_PATH, media_type="application/octet-stream",
                        filename=f"inventario-{date.today().isoformat()}.db")


# --------------------------------------------------------------------------- #
# Impostazioni
# --------------------------------------------------------------------------- #
def _pubbliche(cfg: dict) -> dict:
    chiave = cfg["gemini_api_key"]
    return {
        "nome_negozio": cfg["nome_negozio"], "ai_provider": cfg["ai_provider"],
        "gemini_model": cfg["gemini_model"], "ollama_url": cfg["ollama_url"],
        "ollama_model": cfg["ollama_model"], "ricarico": cfg["ricarico"],
        "arrotonda": cfg["arrotonda"], "pin_impostato": bool(cfg["pin"]),
        "chiave_impostata": bool(chiave), "chiave_finale": chiave[-4:] if chiave else "",
        "url_telefono": url_telefono(),
    }


class Impostazioni(BaseModel):
    nome_negozio: str | None = Field(default=None, max_length=60)
    ai_provider: str | None = None
    gemini_api_key: str | None = None      # vuoto = lascia invariata
    gemini_model: str | None = None
    ollama_url: str | None = None
    ollama_model: str | None = None
    ricarico: float | None = Field(default=None, ge=0, le=1000)
    arrotonda: str | None = None
    pin: str | None = None                 # None = invariato, "" = rimuovi PIN


@api.get("/impostazioni")
def leggi_impostazioni():
    return _pubbliche(settings.carica())


@api.post("/impostazioni")
def salva_impostazioni(nuove: Impostazioni, response: Response):
    vecchie = settings.carica()
    modifiche = {k: (v.strip() if isinstance(v, str) else v)
                 for k, v in nuove.model_dump().items() if v is not None}
    if not modifiche.get("gemini_api_key"):
        modifiche.pop("gemini_api_key", None)
    if "gemini_model" in modifiche and not modifiche["gemini_model"]:
        modifiche["gemini_model"] = "auto"
    cfg = settings.salva(modifiche)
    if (cfg["ricarico"], cfg["arrotonda"]) != (vecchie["ricarico"], vecchie["arrotonda"]):
        db.ricalcola_vendite(cfg["ricarico"], cfg["arrotonda"])
    if cfg["pin"]:
        _imposta_cookie(response, cfg)  # chi cambia il PIN resta collegato
    return _pubbliche(cfg)


@api.post("/impostazioni/prova-ai")
def prova_ai():
    try:
        return {"messaggio": prova_connessione(settings.carica())}
    except ErroreAI as e:
        raise HTTPException(400, str(e))


@app.get("/api/qr.svg")
def qr():
    """QR code dell'indirizzo per il telefono (mostrato sul PC)."""
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(url_telefono(), image_factory=qrcode.image.svg.SvgPathImage, box_size=12)
    return Response(img.to_string(), media_type="image/svg+xml")


app.include_router(api)


# --------------------------------------------------------------------------- #
# Interfaccia
# --------------------------------------------------------------------------- #
@app.middleware("http")
async def niente_cache(request: Request, call_next):
    """Così dopo un aggiornamento dell'app il telefono carica subito la versione nuova."""
    risposta = await call_next(request)
    if request.url.path == "/" or (request.url.path.startswith("/static/")
                                   and not request.url.path.startswith("/static/fonts/")):
        risposta.headers["Cache-Control"] = "no-cache"
    return risposta


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    indirizzo = url_telefono()
    print("\n" + "=" * 60)
    print("  INVENTARIO NEGOZIO avviato")
    print(f"  Su questo PC:         http://localhost:{PORTA}")
    print(f"  Dal telefono (Wi-Fi): {indirizzo}")
    print("  Per chiudere: chiudi questa finestra")
    print("=" * 60 + "\n")
    threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{PORTA}")).start()
    uvicorn.run(app, host="0.0.0.0", port=PORTA, log_level="warning")
