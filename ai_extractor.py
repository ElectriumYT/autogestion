"""
ai_extractor.py - Lettura fatture tramite Vision AI.

Due provider intercambiabili, entrambi a costo zero:
  - "gemini" : Google Gemini (piano gratuito di Google AI Studio). Serve una API key gratuita.
  - "ollama" : modello vision locale (es. qwen2.5vl, llama3.2-vision). Niente internet.

La configurazione arriva da settings.py (modificabile dall'app, scheda Altro).
Con Gemini il modello viene scelto da solo tra quelli gratuiti che rispondono.
"""
import base64
import io
import json
import re

import requests
from PIL import Image, ImageOps

# --------------------------------------------------------------------------- #
# PROMPT: regole stringenti per l'AI
# --------------------------------------------------------------------------- #
PROMPT_FATTURA = """
Sei un assistente contabile per un negozio italiano di surgelati e alimentari che vende SFUSO
(al pezzo o al kg). Analizza le immagini della fattura / bolla di consegna (possono essere più
pagine dello stesso documento: trattale come un unico documento) e restituisci
ESCLUSIVAMENTE un oggetto JSON valido, senza testo prima o dopo, senza markdown.

REGOLE OBBLIGATORIE
1. NOME PULITO: trasforma la descrizione commerciale in un nome generico, breve, in italiano,
   con iniziale maiuscola. Rimuovi codici articolo, calibri, pezzature, marche, sigle di
   confezione, lotti e numeri.
   Esempi: "Orata 150 300 art XYZ" -> "Orata"; "GAMBERONI ARG. L1 10/20 CT 2KG" -> "Gamberoni argentini";
   "PISELLI FINI SURG. 1KG X10" -> "Piselli fini".
   Usa sempre lo stesso nome per lo stesso tipo di prodotto.
2. UNITA' DI VENDITA: decidi se il prodotto si vende a "kg" (pesce, carne, verdure sfuse, prodotti
   a peso) oppure a "pz" (confezioni, pezzi singoli). Usa solo "kg" o "pz".
   Pesce, crostacei, molluschi, carne e verdure sfuse si vendono SEMPRE a "kg": se il collo
   riporta un peso (es. "CT 2KG", "CASSA 5 KG"), la quantità totale è colli x kg per collo
   (es. 2 CT da 2KG = 4 kg) e il prezzo va calcolato al kg.
3. QUANTITA': calcola il numero TOTALE di unità di vendita della riga.
   Esempi: "2 cartoni da 6 pezzi" -> 12 pz; "1 collo da 5 kg" -> 5 kg; "3 casse x 2,5 kg" -> 7.5 kg.
4. PREZZO UNITARIO IVATO: calcola il costo di UN singolo pezzo o di UN singolo kg, IVA INCLUSA.
   - Se la riga riporta il prezzo totale del collo/scatolo, dividilo per la quantità.
     Esempio: 6 pezzi a 30,00 EUR totali -> 5.00 EUR al pezzo.
   - Se il prezzo è imponibile (senza IVA), applica l'aliquota IVA della riga
     (alimentari tipicamente 4% o 10%; se non indicata usa 10%).
   - Se uno sconto è indicato sulla riga, applicalo prima dell'IVA.
5. Riporta anche i dati grezzi letti, così il calcolo può essere verificato.
6. Usa il punto come separatore decimale. Nessun simbolo di valuta nei numeri.
7. Ignora righe che non sono prodotti (trasporto, cauzioni, totali, bolli).
8. Se un valore è illeggibile usa null; non inventare.
9. CATEGORIA: scegli una sola tra: "pesce", "crostacei" (anche molluschi), "carne", "verdure"
   (anche frutta), "pronti" (piatti pronti, fritti, pasta ripiena), "pizza" (pane, pizza, impasti),
   "gelati" (anche dolci), "latticini" (anche uova), "dispensa" (conserve, pasta secca, olio,
   bevande), "altro".
10. Se lo stesso prodotto compare su più righe, riportalo una volta sola con il prezzo più recente.

FORMATO DI USCITA (esattamente queste chiavi):
{
  "fornitore": "string o null",
  "numero_documento": "string o null",
  "data_documento": "YYYY-MM-DD o null",
  "prodotti": [
    {
      "descrizione_originale": "testo esatto della riga",
      "nome_pulito": "Orata",
      "categoria": "pesce",
      "unita": "kg",
      "quantita_totale": 6,
      "prezzo_totale_riga": 30.00,
      "prezzo_riga_ivato": false,
      "aliquota_iva": 10,
      "prezzo_unitario_ivato": 5.50
    }
  ]
}
""".strip()


class ErroreAI(Exception):
    pass


def prepara_immagine(file_bytes: bytes) -> tuple[bytes, str]:
    """Ruota secondo EXIF, ridimensiona (max 2000px) e converte in JPEG:
    upload più veloce e meno token consumati dall'AI."""
    try:
        img = Image.open(io.BytesIO(file_bytes))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as e:
        raise ErroreAI("Il file caricato non è un'immagine valida.") from e
    img.thumbnail((2000, 2000))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue(), "image/jpeg"


def _estrai_json(testo: str) -> dict:
    """Ripulisce eventuali ```json ... ``` e fa il parse."""
    testo = testo.strip()
    testo = re.sub(r"^```(?:json)?\s*|\s*```$", "", testo, flags=re.IGNORECASE)
    try:
        return json.loads(testo)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", testo, flags=re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise ErroreAI(f"Risposta AI non in formato JSON:\n{testo[:500]}")


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #
# Modelli gratuiti provati in ordine quando il modello è "auto" (o quando quello scelto non risponde).
# Google ritira spesso i modelli vecchi: se nessuno di questi esiste più, l'elenco viene letto
# direttamente dall'account (vedi _modelli_disponibili).
MODELLI_GEMINI = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
_ultimo_ok: str | None = None      # ultimo modello che ha risposto: viene provato per primo
ultimo_modello_usato: str | None = None


def _client_gemini(cfg: dict):
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise ErroreAI("Libreria mancante: pip install google-genai") from e
    if not cfg.get("gemini_api_key"):
        raise ErroreAI("Chiave Gemini non impostata: inseriscila nella scheda Altro.")
    # timeout breve e nessun tentativo ripetuto: se un modello è sovraccarico si passa al successivo
    return genai.Client(api_key=cfg["gemini_api_key"],
                        http_options=types.HttpOptions(
                            timeout=45000, retry_options=types.HttpRetryOptions(attempts=1)))


def _modelli_disponibili(client) -> list[str]:
    """Modelli 'flash' di testo/visione presenti sull'account, dal più recente."""
    nomi = []
    for m in client.models.list():
        n = m.name.removeprefix("models/")
        if ("generateContent" in (m.supported_actions or []) and "flash" in n
                and n.startswith("gemini-")
                and not any(x in n for x in ("image", "preview", "omni", "tts", "live", "audio"))):
            nomi.append(n)

    def versione(n: str) -> float:
        m = re.search(r"\d+(?:\.\d+)?", n)
        return float(m.group(0)) if m else 0.0

    return sorted(nomi, key=lambda n: (versione(n), "lite" not in n), reverse=True)


def _coda_modelli(cfg: dict) -> list[str]:
    scelto = (cfg.get("gemini_model") or "auto").strip()
    coda = (([] if scelto.lower() == "auto" else [scelto])
            + ([_ultimo_ok] if _ultimo_ok else []) + MODELLI_GEMINI)
    return list(dict.fromkeys(coda))


def _genera_gemini(cfg: dict, contenuti: list, json_mode: bool) -> str:
    """Prova i modelli in ordine finché uno risponde.
    Il "ragionamento" del modello è tenuto basso: per leggere una fattura non serve e la
    lettura passa da ~50 a ~12 secondi (i conti vengono comunque ricontrollati in Python)."""
    global _ultimo_ok, ultimo_modello_usato
    import time
    from google.genai import errors, types
    client = _client_gemini(cfg)
    mime = "application/json" if json_mode else None

    def chiama(modello: str, veloce: bool):
        config = types.GenerateContentConfig(
            temperature=0, response_mime_type=mime,
            thinking_config=types.ThinkingConfig(thinking_level="low") if veloce else None)
        return client.models.generate_content(model=modello, contents=contenuti, config=config)

    coda, provati, errori, esteso, occupati = _coda_modelli(cfg), set(), [], False, []
    while coda:
        modello = coda.pop(0)
        if modello in provati:
            continue
        provati.add(modello)
        try:
            try:
                risposta = chiama(modello, veloce=True)
            except errors.APIError as e:
                if getattr(e, "code", None) != 400 or "API key" in str(e):
                    raise
                risposta = chiama(modello, veloce=False)   # modello che non accetta l'opzione
            if risposta.text:
                _ultimo_ok = ultimo_modello_usato = modello
                return risposta.text
            errori.append(f"{modello}: risposta vuota")
        except errors.APIError as e:
            codice = getattr(e, "code", None)
            if codice in (400, 401, 403) and "API key" in str(e):
                raise ErroreAI("La chiave Gemini non è valida: controllala nella scheda Altro.") from e
            errori.append(f"{modello}: {codice or 'errore'}")
            if codice in (500, 503, 504):
                occupati.append(modello)
        except Exception as e:  # timeout, rete assente
            errori.append(f"{modello}: {type(e).__name__}")
        if not coda and not esteso:   # nessuno dei modelli noti ha risposto
            esteso = True
            try:      # chiedi a Google quali altri modelli ha l'account...
                coda = [m for m in _modelli_disponibili(client) if m not in provati][:3]
            except Exception:
                pass
            if occupati:   # ...e riprova una volta quelli che erano solo sovraccarichi
                time.sleep(3)
                provati -= set(occupati)
                coda += occupati
    if errori and all("429" in e for e in errori):
        raise ErroreAI("Limite gratuito di Gemini raggiunto per oggi o per questo minuto. "
                       "Riprova tra poco, oppure inserisci la fattura a mano.")
    if errori and all(any(c in e for c in ("500", "503", "504")) for e in errori):
        raise ErroreAI("I server gratuiti di Gemini sono sovraccarichi in questo momento. "
                       "Riprova tra un minuto, oppure inserisci la fattura a mano.")
    raise ErroreAI("Nessun modello Gemini ha risposto (" + "; ".join(errori) + "). "
                   "Controlla la connessione a Internet e riprova.")


def _gemini(immagini: list[bytes], prompt: str, cfg: dict) -> str:
    from google.genai import types
    parti = [types.Part.from_bytes(data=i, mime_type="image/jpeg") for i in immagini]
    return _genera_gemini(cfg, parti + [prompt], json_mode=True)


def _ollama(immagini: list[bytes], prompt: str, cfg: dict) -> str:
    url = (cfg.get("ollama_url") or "http://localhost:11434").rstrip("/")
    payload = {
        "model": cfg.get("ollama_model") or "qwen2.5vl:7b",
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(i).decode() for i in immagini]}],
    }
    try:
        r = requests.post(f"{url}/api/chat", json=payload, timeout=600)
        r.raise_for_status()
    except requests.RequestException as e:
        raise ErroreAI(f"Ollama non raggiungibile su {url}: {e}") from e
    return r.json()["message"]["content"]


def prova_connessione(cfg: dict) -> str:
    """Verifica rapida che il motore AI configurato risponda."""
    if cfg.get("ai_provider") == "ollama":
        url = (cfg.get("ollama_url") or "http://localhost:11434").rstrip("/")
        try:
            modelli = [m["name"] for m in requests.get(f"{url}/api/tags", timeout=5).json()["models"]]
        except Exception as e:
            raise ErroreAI(f"Ollama non raggiungibile su {url}") from e
        voluto = cfg.get("ollama_model", "")
        if not any(m == voluto or m.split(":")[0] == voluto for m in modelli):
            raise ErroreAI(f"Ollama risponde ma il modello '{voluto}' non è installato "
                           f"(esegui: ollama pull {voluto}).")
        return f"Ollama OK · modello {voluto}"
    _genera_gemini(cfg, ["Rispondi solo con: OK"], json_mode=False)
    return f"Gemini risponde con il modello {ultimo_modello_usato}"


# --------------------------------------------------------------------------- #
# Verifica calcoli (non ci fidiamo ciecamente dell'aritmetica dell'AI)
# --------------------------------------------------------------------------- #
def _num(v):
    try:
        return float(str(v).replace(",", ".")) if v is not None else None
    except ValueError:
        return None


def _ricalcola(p: dict) -> dict:
    """Se ci sono i dati grezzi, ricalcola il prezzo unitario ivato in Python.
    Se differisce da quello dell'AI di oltre 1 centesimo, usa il valore ricalcolato
    e segnala la riga come 'da_verificare'."""
    tot = _num(p.get("prezzo_totale_riga"))
    qta = _num(p.get("quantita_totale"))
    iva = _num(p.get("aliquota_iva"))
    ai_val = _num(p.get("prezzo_unitario_ivato"))
    p["da_verificare"] = False

    if tot and qta and qta > 0:
        unit = tot / qta
        gia_ivato = str(p.get("prezzo_riga_ivato")).strip().lower() in ("true", "1", "si", "sì")
        if not gia_ivato:
            unit *= 1 + (iva if iva is not None else 10) / 100
        unit = round(unit, 4)
        if ai_val is None or abs(unit - ai_val) > 0.01:
            p["da_verificare"] = ai_val is not None
            p["prezzo_unitario_ivato"] = unit
    elif ai_val is None:
        p["da_verificare"] = True
    else:
        p["prezzo_unitario_ivato"] = ai_val

    p["unita"] = "kg" if str(p.get("unita", "")).lower().startswith("k") else "pz"
    nome = " ".join((p.get("nome_pulito") or "").split())
    p["nome_pulito"] = nome[:1].upper() + nome[1:]
    p["categoria"] = str(p.get("categoria") or "altro").strip().lower()
    return p


def estrai_fattura(immagini: list[bytes], cfg: dict, nomi_esistenti: list[str] | None = None) -> dict:
    """Invia le pagine all'AI e ritorna il dizionario strutturato e verificato.
    nomi_esistenti: prodotti già in archivio, così l'AI riusa gli stessi nomi e lo storico resta unito."""
    prompt = PROMPT_FATTURA
    if nomi_esistenti:
        prompt += ("\n\nPRODOTTI GIA' IN ARCHIVIO: se una riga è lo stesso prodotto di uno di questi, "
                   "usa ESATTAMENTE questo nome come nome_pulito:\n" + "; ".join(nomi_esistenti[:400]))
    provider = (cfg.get("ai_provider") or "gemini").lower()
    if provider == "gemini":
        testo = _gemini(immagini, prompt, cfg)
    elif provider == "ollama":
        testo = _ollama(immagini, prompt, cfg)
    else:
        raise ErroreAI(f"Provider sconosciuto: {provider}")

    dati = _estrai_json(testo)
    if isinstance(dati, list):
        # A volte il modello avvolge la risposta in una lista (un oggetto per pagina o per documento)
        documenti = [d for d in dati if isinstance(d, dict) and isinstance(d.get("prodotti"), list)]
        if documenti:
            unito = dict(documenti[0])
            unito["prodotti"] = [p for d in documenti for p in d["prodotti"]]
            dati = unito
        else:
            dati = {"prodotti": dati}
    if not isinstance(dati, dict):
        raise ErroreAI("L'AI ha risposto in un formato inatteso. Riprova con un'altra foto.")
    prodotti = [_ricalcola(p) for p in dati.get("prodotti") or [] if isinstance(p, dict)]
    dati["prodotti"] = [p for p in prodotti if p["nome_pulito"]]
    dati["modello"] = ultimo_modello_usato if provider == "gemini" else cfg.get("ollama_model")
    return dati
