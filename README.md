# 🕷️ Crawl4AI-Web — lokale Crawl-Station als Proxmox LXC (Community-Script-Stil)

Webseiten eintragen → crawlen → **LLM-ready Markdown** ansehen / laden.
Läuft **vollständig lokal** im LXC, keine Cloud nötig. Upstream: https://github.com/unclecode/crawl4AI

**Visuelle Oberfläche:** URL-Feld + Modus (Markdown / Fit / HTML) → Crawlen → Ergebnis + Verlauf (SQLite).
**Login:** optional — beim Installieren Passwort setzen oder leer lassen (offen im Heimnetz).

## 🎯 Use Cases — wofür ist das gut?

Crawl4AI macht aus jeder Webseite **sauberes, LLM-ready Markdown**. Die Web-UI legt noch Bulk-Crawl,
Verlauf mit Suche und Statistiken drauf. Typische Anwendungen:

| Use Case | Wie |
|---|---|
| **🤖 RAG / Wissensdatenbank füttern** | Dokus, Handbücher, Wikis per Bulk crawlen → Markdown laden → in NotebookLM, AnythingLLM, Open WebUI oder eigene Vektor-DB importieren |
| **📚 Recherche & Lesearchiv** | Artikel/Quellen sammeln, im Verlauf suchen, als `.md` ablegen — lesbar ohne Cookie-Banner und Tracking |
| **💰 Preis- & Angebotsbeobachtung** | Produktseiten regelmäßig per **↻ Retry** neu crawlen und Versionen vergleichen |
| **📝 Docs offline verfügbar machen** | API-Dokus und Anleitungen als Markdown sichern (z. B. vor Offline-Phasen oder Paywalls) |
| **🔍 SEO- / Content-Audit** | Mehrere eigene Seiten auf einmal crawlen (Fit-Markdown = KI-bereinigt) und Inhalte prüfen |
| **📰 Monitoring / Change-Detection** | Wichtige Seiten per Retry neu holen; Fehler landen getrennt im Verlauf (`nur Fehler`-Filter) |
| **🧹 Webseiten entmüllen** | Fit-Modus entfernt Navigation/Werbung — übrig bleibt der eigentliche Inhalt |

Tipp: **Bulk-Feld** (eine URL pro Zeile) für Listen, **Suche + Statusfilter** im Verlauf zum Wiederfinden,
**Tabs Markdown/HTML/Fehler** in der Ergebnisansicht je nach Bedarf.

## 🚀 Einzeiler (auf dem Proxmox-Host als root)

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/Crawl4AI-Proxmox/main/install/crawl4ai.sh)"
```

Bei Fehlern mit Debug-Log:

```bash
bash -x <(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/Crawl4AI-Proxmox/main/install/crawl4ai.sh)
```

Erwartete Abfragen:

- `Container-ID eingeben [251]:` → Enter oder eigene ID ≥ 100
- `Optionales Web-Passwort (versteckt, leer = kein Login):` → Passwort oder Enter

Erwartete Endausgabe:

```text
✓ Crawl4AI-Web erfolgreich installiert!
✓ Container-ID: 251
✓ Web UI:       http://192.168.x.x:8000
✓ Health:       http://192.168.x.x:8000/api/health
✓ Login:        kein Passwort (offen im Heimnetz) / Passwortschutz AKTIV
```

Danach: Browser öffnen → URL eintragen → **Crawlen**.

## 🖥️ Web-UI im Überblick

- **📊 Übersicht:** Zähler für Crawls gesamt / erfolgreich / fehlerhaft / Zeichen + letzter Crawl
- **+ Crawlen:** einzelne URL (Enter genügt) oder **📦 Bulk** mit mehreren URLs (eine pro Zeile, mit Fortschrittsbalken)
- **📄 Ergebnis:** Tabs **Markdown / HTML / Fehler-Info**, dazu Laden, Kopieren und **↻ Erneut crawlen**
- **🗂 Verlauf:** Live-**Suche** in URL/Titel, **Statusfilter** (alle / nur erfolgreich / nur Fehler), Ansehen, Retry, Löschen, **Alle löschen**
- **🔐 Sitzungen** überleben Container-Reboots (signierte Cookies, 30 Tage); bei Ablauf meldet sich die UI von selbst

## 📦 Was installiert wird

| Komponente | Details |
|---|---|
| Container | Debian 12, unprivilegiert, `nesting=1`, `onboot=1` |
| Ressourcen | 4 vCPU, 4096 MB RAM, 16 GB Disk (Browser braucht das!) |
| App | FastAPI (`app/main.py`) + `crawl4ai` + Playwright/Chromium |
| Port | `8000`, bind `0.0.0.0` |
| Service | `crawl4ai.service` (`Restart=always`, `enable --now`) |
| Daten | `/var/lib/crawl4ai/crawls.db` (SQLite-Verlauf) |
| Passwort | optional via `/etc/crawl4ai-web.env` (`CRAWL4AI_PASSWORD`) |

## 🖥️ VM statt LXC?

Crawl4AI + Chromium ist leistungshungrig. Falls der LXC zu langsam ist oder kein `nesting` erlaubt ist:
VM mit Debian 12, 4 vCPU / 8 GB RAM erstellen und **im Gast** ausführen:

```bash
git clone https://github.com/HatchetMan111/Crawl4AI-Proxmox.git /opt/crawl4ai-web
cd /opt/crawl4ai-web
python3 -m venv venv && ./venv/bin/pip install -r app/requirements.txt
./venv/bin/python -m playwright install --with-deps chromium
cp systemd/crawl4ai.service /etc/systemd/system/ && systemctl enable --now crawl4ai
curl -fsS http://localhost:8000/api/health
```

## 🔧 Betrieb

```bash
# im Container (pct enter 251):
systemctl status crawl4ai
journalctl -u crawl4ai -f
curl -fsS http://localhost:8000/api/health

# Reboot-Test:
pct reboot 251 && sleep 20 && curl -fsS http://<CT-IP>:8000/api/health
```

## 🔄 Update / 🗑 Deinstallieren

```bash
# Update:
pct exec 251 -- bash -c 'cd /opt/crawl4ai-web && git pull && ./venv/bin/pip install -r app/requirements.txt && systemctl restart crawl4ai'

# Deinstallieren:
pct stop 251 && pct destroy 251 --purge
```

## 🧪 Testdurchlauf (Nachweis)

```bash
bash -n install/crawl4ai.sh && echo "bash-syntax OK"
python3 -m py_compile app/main.py && echo "python-syntax OK"
curl -fsS http://<CT-IP>:8000/api/health
curl -fsS -X POST http://<CT-IP>:8000/api/crawl -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","mode":"markdown"}' | head -c 500
pct reboot 251; sleep 25; curl -fsS http://<CT-IP>:8000/api/health
```

## 📁 Repo-Struktur

```text
Crawl4AI-Proxmox/
  install/crawl4ai.sh      ← Proxmox-Host-Script (Einzeiler)
  app/main.py              ← FastAPI + Web UI (Single File, kein Build nötig)
  app/requirements.txt
  systemd/crawl4ai.service
  README.md
```
