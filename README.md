# JARVIS

Ein persönlicher, lokal-zuerst arbeitender KI-Assistent mit Sprache, Langzeitgedächtnis,
Agenten und PC-Steuerung.

> **Status:** in aktiver Entwicklung. Diese README wird mit jeder Ausbaustufe erweitert.
> Was hier steht, ist implementiert und getestet — nichts davon ist eine Absichtserklärung.

## Schnellstart (Entwicklung)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env          # optional: OPENROUTER_API_KEY eintragen
.venv/bin/python app.py       # http://127.0.0.1:8765
```

Tests und Linter:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

## Aktueller Funktionsumfang

- FastAPI-Backend mit WebSocket-Eventstream (`/api/events`)
- SQLite mit vorwärtsgerichteten Migrationen — die Datenbank wird bei Schemaänderungen
  **nie** gelöscht
- Einstellungen als lesbares JSON, Secrets getrennt davon im Betriebssystem-Schlüsselspeicher
  (Fallback: Datei mit `0600`-Rechten)
- Automatische Secret-Maskierung in allen Logs, Events und Audit-Einträgen
- Health-Monitoring, das fehlende Subsysteme **benennt** statt Verfügbarkeit vorzutäuschen

## Plattformgrenzen

JARVIS behauptet nie, etwas zu können, was das laufende System nicht hergibt.
`GET /api/platform` meldet für jede Fähigkeit, ob sie verfügbar ist, und wenn nicht, warum
und wie man sie aktiviert. Vollständige PC-Steuerung und Sprachausgabe setzen den
Windows-Host mit den optionalen Abhängigkeiten voraus.

## Lizenz

Siehe `LICENSE`.
