# 🧊 Inventario Negozio – Web App locale

App privata per leggere le fatture con l'AI, tenere lo storico dei prezzi d'acquisto e calcolare
il prezzo al pubblico (+40%). Gira sul PC del negozio e si usa dal telefono via Wi‑Fi. Costo: zero.

## Avvio (sempre così)

**Doppio clic su `avvia.bat`.**

- La prima volta prepara tutto da solo (ambiente Python + componenti, qualche minuto).
- Non serve più "activate" né PowerShell: il file usa direttamente il Python dentro `.venv`.
- Si apre il browser del PC; la finestra nera deve restare aperta (chiuderla = spegnere l'app).
- Se Windows chiede di consentire Python nel firewall: spunta **Reti private** → *Consenti*.

Requisito unico: Python 3.11+ da <https://www.python.org/downloads/> con la casella
**"Add python.exe to PATH"** spuntata durante l'installazione.

## Sul telefono

1. Telefono collegato **allo stesso Wi‑Fi** del PC.
2. Inquadra il **QR code** mostrato sull'app del PC (oppure scrivi l'indirizzo tipo `http://192.168.0.119:8000`).
3. Menu del browser → **Aggiungi a schermata Home**: avrai l'icona come un'app vera.

Consiglio: sul router imposta un IP fisso (prenotazione DHCP) per il PC, così l'indirizzo non cambia.

Se il telefono non si collega: la rete Wi‑Fi del PC deve essere impostata come **Privata**
(Impostazioni → Rete e Internet → proprietà della rete) e Python consentito nel firewall.

## Chiave AI (gratis, una volta sola)

1. Vai su <https://aistudio.google.com/apikey>, accedi con il tuo account Google, **Create API key**.
2. Nell'app: scheda **Altro** → *Lettura delle fatture* → incolla la chiave → **Salva e prova**.

Il modello Gemini viene scelto **da solo**: l'app prova in ordine i modelli gratuiti e usa il primo
che risponde (Google ritira spesso i modelli vecchi e quelli nuovi a volte sono sovraccarichi).
L'elenco è in `ai_extractor.py` → `MODELLI_GEMINI`.

Alternativa 100% offline: installa Ollama (<https://ollama.com/download>), esegui
`ollama pull qwen2.5vl:7b`, e in *Altro* scegli **Ollama**. Serve un PC con almeno 8 GB di RAM.
Nota: sul piano gratuito Gemini, Google può usare le immagini inviate per migliorare i modelli;
con Ollama le fatture non escono mai dal PC.

## Uso

- **Pulsante fotocamera** (al centro in basso) → scatta una o più pagine → *Leggi la fattura* →
  controlla le righe (costo precedente, variazione e nuovo prezzo al pubblico sono già calcolati) → **Salva**.
- **Oggi**: i **cartellini da riscrivere**, cioè i prodotti il cui prezzo al pubblico è cambiato.
  Tocca un cartellino quando l'hai riscritto sullo scaffale.
- **Prodotti**: ricerca, filtri per categoria, rincari e ribassi, mini-grafico dell'andamento.
  Tocca un prodotto per grafico, storico e guadagno; la matita lo rinomina, cambia categoria o lo
  **unisce** a un altro (se l'AI ha chiamato lo stesso prodotto in due modi).
- **Fatture**: archivio delle fatture salvate. Una fattura caricata per errore si può **annullare**:
  i prodotti tornano al prezzo precedente.
- **Altro**: ricarico e arrotondamento (centesimo, 5 o 10 cent), chiave AI, PIN, stampa del listino,
  listino per Excel, copia di sicurezza del database, QR per il telefono.

## Logica prezzi

- Prezzo unitario ivato = totale riga ÷ quantità (pezzi o kg) × (1 + IVA) se la riga è imponibile.
  L'AI lo calcola e il server **lo ricontrolla in Python**: le righe incoerenti vengono segnate ⚠.
- Prezzo al pubblico = acquisto ivato × (1 + ricarico), arrotondamento matematico a 2 decimali
  con `Decimal` (5,00 € → 7,00 €). Dalle impostazioni si può arrotondare ai 5 o 10 centesimi.

## File

| File | Ruolo |
|---|---|
| `avvia.bat` | Installa (se serve) e avvia l'app |
| `server.py` | Backend FastAPI: API, PIN, QR code |
| `database.py` | SQLite: storico acquisti, confronto prezzi, ricarico |
| `ai_extractor.py` | Prompt per l'AI, Gemini/Ollama, verifica dei calcoli |
| `settings.py` | Impostazioni salvate in `impostazioni.json` |
| `static/fonts/` | Caratteri Barlow (licenza OFL), inclusi per funzionare senza Internet |
| `static/` | Interfaccia mobile (HTML/CSS/JS, nessuna dipendenza esterna) |
| `inventario.db` | **I tuoi dati** – creato al primo avvio. Per il backup copia questo file. |
| `impostazioni.json` | Chiave AI e PIN – non condividerlo |
