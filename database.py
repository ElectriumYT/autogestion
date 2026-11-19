"""
database.py - Gestione del database SQLite (inventario.db)

Tabelle:
  prodotti   -> anagrafica (un record per prodotto "pulito", es. "Orata") con l'ultimo prezzo
  documenti  -> una riga per ogni fattura / bolla caricata
  acquisti   -> storico: una riga per ogni prodotto di ogni documento
"""
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from settings import DATA_DIR

DB_PATH = DATA_DIR / "inventario.db"
RICARICO_DEFAULT = 40.0  # +40%

CATEGORIE = ["pesce", "crostacei", "carne", "verdure", "pronti", "pizza",
             "gelati", "latticini", "dispensa", "altro"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS prodotti (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    nome                    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    unita                   TEXT NOT NULL DEFAULT 'pz',      -- 'pz' oppure 'kg'
    prezzo_acquisto_ivato   REAL,                            -- ultimo prezzo d'acquisto unitario ivato
    prezzo_vendita          REAL,                            -- prezzo al pubblico (acquisto + ricarico)
    variazione_ultima       REAL DEFAULT 0,                  -- differenza vs fornitura precedente
    aggiornato_il           TEXT
);

CREATE TABLE IF NOT EXISTS documenti (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fornitore   TEXT,
    numero      TEXT,
    data        TEXT NOT NULL,                               -- YYYY-MM-DD
    creato_il   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS acquisti (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    prodotto_id             INTEGER NOT NULL REFERENCES prodotti(id) ON DELETE CASCADE,
    data_acquisizione       TEXT NOT NULL,                   -- YYYY-MM-DD
    prezzo_acquisto_ivato   REAL NOT NULL,
    prezzo_vendita          REAL NOT NULL,
    prezzo_precedente       REAL,                            -- NULL alla prima fornitura
    variazione              REAL,                            -- nuovo - precedente
    fornitore               TEXT,
    numero_documento        TEXT,
    descrizione_originale   TEXT
);

CREATE INDEX IF NOT EXISTS idx_acquisti_prodotto ON acquisti(prodotto_id, data_acquisizione);
"""

# Colonne aggiunte nelle versioni successive (i database esistenti vengono aggiornati da soli)
MIGRAZIONI = [
    ("prodotti", "categoria", "TEXT NOT NULL DEFAULT 'altro'"),
    ("prodotti", "cartellino", "INTEGER NOT NULL DEFAULT 0"),   # 1 = cartellino da riscrivere
    ("prodotti", "vendita_esposta", "REAL"),                    # prezzo scritto sul cartellino attuale
    ("acquisti", "documento_id", "INTEGER REFERENCES documenti(id) ON DELETE CASCADE"),
]


def arrotonda(valore, passo="0.01") -> Decimal:
    """Arrotondamento matematico (half-up). passo 0.01 = al centesimo; 0.05 / 0.10 = ai 5 / 10 cent.
    Si usa Decimal per evitare gli errori dei float (es. 2.675 -> 2.68, non 2.67)."""
    passo = Decimal(str(passo))
    return (Decimal(str(valore)) / passo).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * passo


def calcola_prezzo_vendita(prezzo_acquisto_ivato, ricarico_pct=RICARICO_DEFAULT, passo="0.01") -> float:
    """Prezzo al pubblico = acquisto ivato + ricarico%, arrotondato. Con +40%: 5.00 -> 7.00"""
    acquisto = Decimal(str(prezzo_acquisto_ivato))
    fattore = 1 + Decimal(str(ricarico_pct)) / 100
    return float(arrotonda(acquisto * fattore, passo).quantize(Decimal("0.01")))


@contextmanager
def connessione():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Crea inventario.db e le tabelle se non esistono e aggiorna i database più vecchi."""
    with connessione() as conn:
        conn.executescript(SCHEMA)
        for tabella, colonna, tipo in MIGRAZIONI:
            esistenti = {r["name"] for r in conn.execute(f"PRAGMA table_info({tabella})")}
            if colonna not in esistenti:
                conn.execute(f"ALTER TABLE {tabella} ADD COLUMN {colonna} {tipo}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_acquisti_doc ON acquisti(documento_id)")
        conn.execute("UPDATE prodotti SET vendita_esposta = prezzo_vendita "
                     "WHERE vendita_esposta IS NULL AND cartellino = 0")


def _pulisci_nome(nome: str) -> str:
    nome = " ".join(str(nome or "").split())
    return nome[:1].upper() + nome[1:]


def _riallinea(conn, prodotto_id: int) -> None:
    """Ricostruisce variazioni e prezzo attuale di un prodotto dallo storico, in ordine di data.
    Così funziona anche se si carica una fattura vecchia o se ne elimina una."""
    righe = conn.execute(
        "SELECT id, data_acquisizione, prezzo_acquisto_ivato, prezzo_vendita FROM acquisti "
        "WHERE prodotto_id = ? ORDER BY data_acquisizione, id", (prodotto_id,)).fetchall()
    if not righe:
        conn.execute("DELETE FROM prodotti WHERE id = ?", (prodotto_id,))
        return
    precedente = None
    for r in righe:
        variazione = (float(arrotonda(r["prezzo_acquisto_ivato"] - precedente))
                      if precedente is not None else None)
        conn.execute("UPDATE acquisti SET prezzo_precedente = ?, variazione = ? WHERE id = ?",
                     (precedente, variazione, r["id"]))
        precedente = r["prezzo_acquisto_ivato"]
    ultima = righe[-1]
    p = conn.execute("SELECT vendita_esposta FROM prodotti WHERE id = ?", (prodotto_id,)).fetchone()
    esposta = p["vendita_esposta"]
    da_rifare = esposta is None or abs(esposta - ultima["prezzo_vendita"]) > 0.004
    conn.execute(
        """UPDATE prodotti SET prezzo_acquisto_ivato = ?, prezzo_vendita = ?, variazione_ultima = ?,
               aggiornato_il = ?, cartellino = ? WHERE id = ?""",
        (ultima["prezzo_acquisto_ivato"], ultima["prezzo_vendita"], variazione or 0,
         ultima["data_acquisizione"], 1 if da_rifare else 0, prodotto_id))


def registra_documento(righe: list[dict], fornitore: str | None = None, numero: str | None = None,
                       data_doc: str | None = None, ricarico_pct: float = RICARICO_DEFAULT,
                       passo: str = "0.01") -> dict:
    """Salva una fattura con le sue righe. Ogni riga: {nome, prezzo, unita, categoria, descrizione}.
    Ritorna il documento e l'esito per ogni prodotto (nuovo / variazione)."""
    data_doc = data_doc or date.today().isoformat()
    with connessione() as conn:
        doc_id = conn.execute(
            "INSERT INTO documenti (fornitore, numero, data, creato_il) VALUES (?, ?, ?, ?)",
            (fornitore, numero, data_doc, datetime.now().isoformat(timespec="seconds"))).lastrowid
        inseriti = []
        for r in righe:
            nome = _pulisci_nome(r["nome"])
            if not nome:
                continue
            unita = "kg" if r.get("unita") == "kg" else "pz"
            categoria = r.get("categoria") if r.get("categoria") in CATEGORIE else None
            acquisto = float(arrotonda(r["prezzo"]))
            vendita = calcola_prezzo_vendita(r["prezzo"], ricarico_pct, passo)
            p = conn.execute("SELECT id, categoria FROM prodotti WHERE nome = ?", (nome,)).fetchone()
            if p is None:
                pid = conn.execute("INSERT INTO prodotti (nome, unita, categoria) VALUES (?, ?, ?)",
                                   (nome, unita, categoria or "altro")).lastrowid
                nuovo = True
            else:
                pid, nuovo = p["id"], False
                conn.execute("UPDATE prodotti SET unita = ? WHERE id = ?", (unita, pid))
                if categoria and p["categoria"] == "altro":
                    conn.execute("UPDATE prodotti SET categoria = ? WHERE id = ?", (categoria, pid))
            aid = conn.execute(
                """INSERT INTO acquisti (prodotto_id, documento_id, data_acquisizione,
                       prezzo_acquisto_ivato, prezzo_vendita, fornitore, numero_documento,
                       descrizione_originale) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (pid, doc_id, data_doc, acquisto, vendita, fornitore, numero,
                 r.get("descrizione"))).lastrowid
            _riallinea(conn, pid)
            inseriti.append((aid, nuovo))

        esiti = []
        for aid, nuovo in inseriti:
            e = conn.execute(
                """SELECT p.id, p.nome, p.unita, p.categoria, a.prezzo_precedente,
                          a.prezzo_acquisto_ivato AS prezzo_acquisto, a.prezzo_vendita, a.variazione
                   FROM acquisti a JOIN prodotti p ON p.id = a.prodotto_id WHERE a.id = ?""",
                (aid,)).fetchone()
            esiti.append({**dict(e), "nuovo": nuovo})
    return {"documento_id": doc_id, "esiti": esiti}


def lista_prodotti() -> list[dict]:
    with connessione() as conn:
        prodotti = [dict(r) for r in conn.execute(
            "SELECT * FROM prodotti ORDER BY aggiornato_il DESC, id DESC")]
        storie: dict[int, list[float]] = {}
        for r in conn.execute("SELECT prodotto_id, prezzo_acquisto_ivato FROM acquisti "
                              "ORDER BY data_acquisizione, id"):
            storie.setdefault(r["prodotto_id"], []).append(r["prezzo_acquisto_ivato"])
    for p in prodotti:
        storia = storie.get(p["id"], [])
        p["n_acquisti"] = len(storia)
        p["storia"] = storia[-12:]          # per il mini-grafico nella lista
    return prodotti


def storico_prodotto(prodotto_id: int) -> list[dict]:
    with connessione() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT id, documento_id, data_acquisizione, prezzo_acquisto_ivato, prezzo_vendita,
                      variazione, fornitore, numero_documento, descrizione_originale
               FROM acquisti WHERE prodotto_id = ? ORDER BY data_acquisizione, id""",
            (prodotto_id,))]


def modifica_prodotto(prodotto_id: int, nome: str | None = None, unita: str | None = None,
                      categoria: str | None = None) -> dict:
    """Rinomina / cambia unità o categoria. Se il nuovo nome esiste già, i due prodotti
    vengono UNITI (utile quando l'AI ha chiamato lo stesso prodotto in due modi)."""
    with connessione() as conn:
        p = conn.execute("SELECT * FROM prodotti WHERE id = ?", (prodotto_id,)).fetchone()
        if p is None:
            raise ValueError("Prodotto non trovato")
        if unita in ("pz", "kg"):
            conn.execute("UPDATE prodotti SET unita = ? WHERE id = ?", (unita, prodotto_id))
        if categoria in CATEGORIE:
            conn.execute("UPDATE prodotti SET categoria = ? WHERE id = ?", (categoria, prodotto_id))
        unito = False
        nome = _pulisci_nome(nome) if nome is not None else ""
        if nome and nome != p["nome"]:
            altro = conn.execute("SELECT id FROM prodotti WHERE nome = ? AND id != ?",
                                 (nome, prodotto_id)).fetchone()
            if altro:
                conn.execute("UPDATE acquisti SET prodotto_id = ? WHERE prodotto_id = ?",
                             (altro["id"], prodotto_id))
                conn.execute("DELETE FROM prodotti WHERE id = ?", (prodotto_id,))
                _riallinea(conn, altro["id"])
                prodotto_id, unito = altro["id"], True
            else:
                conn.execute("UPDATE prodotti SET nome = ? WHERE id = ?", (nome, prodotto_id))
    return {"id": prodotto_id, "unito": unito}


def elimina_prodotto(prodotto_id: int) -> None:
    with connessione() as conn:
        conn.execute("DELETE FROM prodotti WHERE id = ?", (prodotto_id,))


def cartellini_fatti(ids: list[int] | None = None) -> None:
    """Segna i cartellini come riscritti (tutti se ids è None)."""
    with connessione() as conn:
        if ids is None:
            conn.execute("UPDATE prodotti SET cartellino = 0, vendita_esposta = prezzo_vendita")
        else:
            conn.executemany("UPDATE prodotti SET cartellino = 0, vendita_esposta = prezzo_vendita "
                             "WHERE id = ?", [(i,) for i in ids])


def lista_documenti() -> list[dict]:
    with connessione() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT d.*, COUNT(a.id) AS n_righe,
                      SUM(CASE WHEN a.variazione > 0.004 THEN 1 ELSE 0 END) AS rincari,
                      SUM(CASE WHEN a.variazione < -0.004 THEN 1 ELSE 0 END) AS ribassi
               FROM documenti d LEFT JOIN acquisti a ON a.documento_id = d.id
               GROUP BY d.id ORDER BY d.data DESC, d.id DESC""")]


def righe_documento(documento_id: int) -> list[dict]:
    with connessione() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT p.id AS prodotto_id, p.nome, p.unita, p.categoria, a.prezzo_acquisto_ivato,
                      a.prezzo_vendita, a.prezzo_precedente, a.variazione, a.descrizione_originale
               FROM acquisti a JOIN prodotti p ON p.id = a.prodotto_id
               WHERE a.documento_id = ? ORDER BY a.id""", (documento_id,))]


def elimina_documento(documento_id: int) -> None:
    """Annulla una fattura: toglie le sue righe e riporta i prodotti al prezzo precedente."""
    with connessione() as conn:
        ids = [r["prodotto_id"] for r in conn.execute(
            "SELECT DISTINCT prodotto_id FROM acquisti WHERE documento_id = ?", (documento_id,))]
        conn.execute("DELETE FROM acquisti WHERE documento_id = ?", (documento_id,))
        conn.execute("DELETE FROM documenti WHERE id = ?", (documento_id,))
        for pid in ids:
            _riallinea(conn, pid)


def ricalcola_vendite(ricarico_pct: float, passo: str = "0.01") -> None:
    """Dopo un cambio di ricarico o arrotondamento: aggiorna il prezzo al pubblico attuale di
    tutti i prodotti (lo storico passato resta com'era) e segnala i cartellini da rifare."""
    with connessione() as conn:
        for r in conn.execute("SELECT id, prezzo_acquisto_ivato, vendita_esposta FROM prodotti").fetchall():
            if r["prezzo_acquisto_ivato"] is None:
                continue
            vendita = calcola_prezzo_vendita(r["prezzo_acquisto_ivato"], ricarico_pct, passo)
            esposta = r["vendita_esposta"]
            conn.execute("UPDATE prodotti SET prezzo_vendita = ?, cartellino = ? WHERE id = ?",
                         (vendita, 1 if esposta is None or abs(esposta - vendita) > 0.004 else 0,
                          r["id"]))
            ultimo = conn.execute("SELECT id FROM acquisti WHERE prodotto_id = ? "
                                  "ORDER BY data_acquisizione DESC, id DESC LIMIT 1", (r["id"],)).fetchone()
            if ultimo:
                conn.execute("UPDATE acquisti SET prezzo_vendita = ? WHERE id = ?", (vendita, ultimo["id"]))


if __name__ == "__main__":
    init_db()
    print(f"Database pronto: {DB_PATH}")
