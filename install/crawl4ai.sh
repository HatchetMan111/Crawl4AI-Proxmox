#!/usr/bin/env bash
# Copyright (c) 2026 Crawl4AI-Web Contributors
# License: MIT | Upstream: https://github.com/unclecode/crawl4AI
#
# LXC Container Installer für Proxmox VE — im Stil der Proxmox VE Community Scripts.
# Erstellt einen unprivilegierten Debian-12-Container und installiert
# crawl4ai-web (FastAPI + Crawl4AI + Playwright/Chromium) als systemd-Dienst.
#
# Einzeiler (auf dem Proxmox-Host als root):
#   bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/Crawl4AI-Proxmox/main/install/crawl4ai.sh)"
# Debug bei Fehlern:
#   bash -x <(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/Crawl4AI-Proxmox/main/install/crawl4ai.sh)

# ── Variablen (oben, Community-Scripts-konform) ─────────────────────
APP="Crawl4AI-Web"
APP_DIR="/opt/crawl4ai-web"
DATA_DIR="/var/lib/crawl4ai"
SERVICE="crawl4ai"
APP_PORT="8000"
var_cpu="4"
var_ram="4096"
var_disk="16"
var_os="debian"
var_version="12"
DEFAULT_CTID="251"
# Repo mit App-Code + systemd-Unit:
REPO_URL="https://github.com/HatchetMan111/Crawl4AI-Proxmox.git"
REPO_BRANCH="main"

YW='\033[33m'
GN='\033[1;32m'
RD='\033[1;31m'
BL='\033[36m'
CL='\033[m'
CM="${GN}✓${CL}"
CR="${RD}✗${CL}"

set -Eeuo pipefail
shopt -s expand_aliases

CTID=""
APP_PW=""

function header_info {
  clear
  cat <<"EOF"

  ┌──────────────────────────────────────────────────────┐
  │                                                      │
  │      C R A W L 4 A I   W E B                         │
  │      ───────────────────────                         │
  │      Lokale Crawl-Station · LLM-ready Markdown       │
  │                                                      │
  │      Proxmox VE  ·  LXC Container Installer          │
  │                                                      │
  └──────────────────────────────────────────────────────┘
   Upstream: https://github.com/unclecode/crawl4AI

EOF
}

function msg_info()  { echo -e "${YW}● ${CL}${BL}${1}...${CL}"; }
function msg_ok()    { echo -e "${CM} ${GN}${1}${CL}"; }
function msg_error() { echo -e "${CR} ${RD}${1}${CL}"; }

# Komplette Fehlerkette ausgeben (niemals nur letzte Zeile)
function error_handler() {
  local exit_code="$?"
  local line_number="$1"
  local cmd="${BASH_COMMAND:-unbekannt}"
  echo -e "\n${CR} ${RD}FEHLER in Zeile ${line_number} (Exit-Code ${exit_code})${CL}"
  echo -e "${RD}Befehl: ${cmd}${CL}"
  echo -e "${YW}--- Stacktrace (call stack) ---${CL}"
  local i=0
  while caller $i 2>/dev/null; do i=$((i+1)); done
  echo -e "${YW}--- Container-Logs (falls Dienst existiert) ---${CL}"
  if [[ -n "${CTID:-}" ]] && command -v pct &>/dev/null && pct status "$CTID" &>/dev/null; then
    pct exec "$CTID" -- journalctl -u "$SERVICE" -n 50 --no-pager 2>&1 || true
    echo -e "${YW}--- Service-Status ---${CL}"
    pct exec "$CTID" -- systemctl status "$SERVICE" --no-pager 2>&1 || true
  fi
  echo -e "${YW}Tipp: erneut mit Debug-Log starten:${CL}"
  echo -e "  bash -x <(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/Crawl4AI-Proxmox/main/install/crawl4ai.sh)"
  echo -e "${YW}Interaktiv im Container:${CL}  pct enter ${CTID:-$DEFAULT_CTID}"
  exit "${exit_code}"
}
trap 'error_handler $LINENO' ERR

function die() {
  msg_error "$1"
  echo -e "${RD}--- Abbruch mit kompletter Meldung siehe oben ---${CL}"
  exit 1
}

# ── Voraussetzungen prüfen ──────────────────────────────────────────
command -v pct &>/dev/null || die "Dieses Script muss auf einem Proxmox VE Host als root ausgeführt werden! (pct nicht gefunden)"
command -v pvesm &>/dev/null || die "pvesm nicht gefunden — ist das ein vollständiger Proxmox VE Host?"
[[ "$(id -u)" -eq 0 ]] || die "Bitte als root ausführen."

# ── Template sicherstellen ──────────────────────────────────────────
TEMPLATE="local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"
TEMPLATE_FILE="/var/lib/vz/template/cache/debian-12-standard_12.7-1_amd64.tar.zst"
if [[ ! -f "$TEMPLATE_FILE" ]]; then
  msg_info "Lade Debian-12-Template herunter"
  set +e
  pveam update >/dev/null 2>&1
  pveam download local debian-12-standard_12.7-1_amd64.tar.zst 2>&1 \
    || die "Template-Download fehlgeschlagen. Manuell: pveam download local debian-12-standard_12.7-1_amd64.tar.zst"
  set -e
  msg_ok "Template heruntergeladen"
fi

header_info
echo -e "\n${YW}Dies erstellt einen unprivilegierten LXC-Container mit ${APP}.${CL}"
echo -e "  ${BL}Hinweis:${CL} Crawl4AI braucht einen echten Browser (Chromium) →"
echo -e "  ${BL}CPU:  ${GN}${var_cpu}${CL}  ${BL}RAM: ${GN}${var_ram} MiB${CL}  ${BL}Disk: ${GN}${var_disk} GiB${CL}  ${BL}Port: ${GN}${APP_PORT}${CL}"
echo -e "  ${BL}Falls zu langsam → als VM mit 4 vCPU / 8 GB RAM installieren (siehe README).${CL}\n"

read -rp "Container-ID eingeben [${DEFAULT_CTID}]: " CTID
CTID="${CTID:-$DEFAULT_CTID}"
if ! [[ "$CTID" =~ ^[0-9]+$ ]] || [[ "$CTID" -lt 100 ]]; then
  die "Ungültige Container-ID: '$CTID' (muss Zahl >= 100 sein)"
fi

echo -ne "${YW}Optionales Web-Passwort (versteckt, leer = kein Login): ${CL}"
read -rs APP_PW
echo ""

if pct status "$CTID" &>/dev/null; then
  echo -ne "${YW}Container ${CTID} existiert bereits. Löschen und neu erstellen? (j/n) ${CL}"
  read -r -n 1 REPLY
  echo
  if [[ ! $REPLY =~ ^[Jj]$ ]]; then
    die "Abgebrochen — andere ID wählen."
  fi
  msg_info "Stoppe Container ${CTID}"
  pct stop "$CTID" >/dev/null 2>&1 || true
  sleep 2
  msg_info "Lösche Container ${CTID}"
  pct destroy "$CTID" --purge >/dev/null 2>&1 || die "pct destroy fehlgeschlagen (pct destroy $CTID --purge)"
  sleep 2
  msg_ok "Gelöscht"
fi

# ── Container erstellen ─────────────────────────────────────────────
msg_info "Erstelle LXC Container ${CTID} (${var_cpu} CPU / ${var_ram} MB / ${var_disk} GB)"
pct create "$CTID" "$TEMPLATE" \
  --hostname crawl4ai \
  --memory "$var_ram" \
  --swap 1024 \
  --cores "$var_cpu" \
  --rootfs "local-lvm:${var_disk}" \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp,firewall=1 \
  --ostype debian \
  --unprivileged 1 \
  --onboot 1 \
  --startup order=2 \
  --features nesting=1 >/dev/null \
  || die "pct create fehlgeschlagen (Storage 'local-lvm' / Bridge 'vmbr0' prüfen: pvesm status; ip link show vmbr0)"
msg_ok "Container erstellt (onboot=1, nesting=1)"

msg_info "Starte Container"
pct start "$CTID" >/dev/null || die "pct start $CTID fehlgeschlagen"
msg_ok "Container gestartet"

# ── Auf Netzwerk warten ─────────────────────────────────────────────
msg_info "Warte auf Netzwerk im Container (max. 60s)"
NET_OK=""
for _ in $(seq 1 30); do
  if pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1; then
    NET_OK=1
    break
  fi
  sleep 2
done
[[ -n "$NET_OK" ]] || die "Netzwerk im Container nach 60s nicht bereit (DHCP/DNS prüfen: pct exec $CTID -- ip a; cat /etc/resolv.conf)"
msg_ok "Netzwerk bereit"

# ── Grundpakete ─────────────────────────────────────────────────────
msg_info "Installiere Grundpakete (python3, venv, git, curl)"
pct exec "$CTID" -- bash -c 'set -euo pipefail; export DEBIAN_FRONTEND=noninteractive; apt-get update 2>&1 | tail -2; apt-get install -y -qq curl git ca-certificates python3 python3-venv python3-pip sqlite3 2>&1 | tail -5' \
  || die "Grundpakete konnten nicht installiert werden (apt-Log siehe oben)"
msg_ok "Grundpakete installiert"

# ── App-Code bereitstellen ──────────────────────────────────────────
msg_info "Lade App-Code (${REPO_BRANCH} @ ${REPO_URL})"
pct exec "$CTID" -- bash -c "set -euo pipefail; rm -rf $APP_DIR; git clone --depth 1 --branch $REPO_BRANCH $REPO_URL $APP_DIR 2>&1 | tail -3" \
  || die "git clone fehlgeschlagen (REPO_URL prüfen: $REPO_URL)"
msg_ok "Repo geklont"

# ── Python-venv + Abhängigkeiten + Chromium ─────────────────────────
msg_info "Erstelle venv + installiere crawl4ai (dauert 3–8 Min)"
pct exec "$CTID" -- bash <<'IN_CT' || die "venv/pip/playwright-Installation fehlgeschlagen (Details siehe oben)"
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
python3 -m venv /opt/crawl4ai-web/venv
/opt/crawl4ai-web/venv/bin/pip install --quiet --upgrade pip wheel
if [[ -f /opt/crawl4ai-web/app/requirements.txt ]]; then
  /opt/crawl4ai-web/venv/bin/pip install -r /opt/crawl4ai-web/app/requirements.txt
else
  /opt/crawl4ai-web/venv/bin/pip install "crawl4ai>=0.7.0" "fastapi>=0.110" "uvicorn[standard]>=0.29" playwright
fi
# Browser-Abhängigkeiten + Chromium (für LXC mit nesting=1)
/opt/crawl4ai-web/venv/bin/python -m playwright install --with-deps chromium
/opt/crawl4ai-web/venv/bin/python -m playwright install-deps chromium || true
/opt/crawl4ai-web/venv/bin/crawl4ai-setup || true
IN_CT
msg_ok "Python-Pakete + Chromium installiert"

# ── App-Dateien + systemd ───────────────────────────────────────────
msg_info "Richte App + systemd-Dienst ein"
# Falls das Script aus einem lokalen Checkout läuft (statt Einzeiler),
# werden die Dateien per pct push mitgeschickt; beim Einzeiler liegen sie
# bereits durch git clone IM Container unter $APP_DIR.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/../app/main.py" ]]; then
  pct push "$CTID" "$SCRIPT_DIR/../app/main.py" "$APP_DIR/app/main.py" || die "pct push main.py fehlgeschlagen"
fi
if [[ -f "$SCRIPT_DIR/../systemd/crawl4ai.service" ]]; then
  pct push "$CTID" "$SCRIPT_DIR/../systemd/crawl4ai.service" "$APP_DIR/systemd/crawl4ai.service" || die "pct push service fehlgeschlagen"
fi

pct exec "$CTID" -- env CRAWL4AI_PW="$APP_PW" bash <<'IN_CT' || exit 1
set -euo pipefail
mkdir -p /var/lib/crawl4ai /opt/crawl4ai-web
# main.py an den vom Service erwarteten Ort legen
if [[ ! -f /opt/crawl4ai-web/main.py && -f /opt/crawl4ai-web/app/main.py ]]; then
  cp /opt/crawl4ai-web/app/main.py /opt/crawl4ai-web/main.py
fi
test -f /opt/crawl4ai-web/main.py || { echo "main.py fehlt in /opt/crawl4ai-web"; ls -la /opt/crawl4ai-web; exit 1; }
# systemd-Unit aus dem Repo übernehmen (Einzeiler-Fall) oder vorhandene nutzen
if [[ ! -f /etc/systemd/system/crawl4ai.service ]]; then
  if [[ -f /opt/crawl4ai-web/systemd/crawl4ai.service ]]; then
    cp /opt/crawl4ai-web/systemd/crawl4ai.service /etc/systemd/system/crawl4ai.service
  else
    echo "crawl4ai.service weder in /etc/systemd/system noch im Repo gefunden"
    ls -la /opt/crawl4ai-web
    exit 1
  fi
fi
# Optionales Passwort als EnvironmentFile (idempotent)
if [[ -n "${CRAWL4AI_PW:-}" ]]; then
  printf 'CRAWL4AI_PASSWORD=%s\n' "$CRAWL4AI_PW" > /etc/crawl4ai-web.env
  chmod 600 /etc/crawl4ai-web.env
else
  rm -f /etc/crawl4ai-web.env
fi
systemctl daemon-reload
systemctl enable --now crawl4ai
IN_CT
msg_ok "Dienst aktiviert (systemctl enable --now crawl4ai)"

# ── Verifikation ────────────────────────────────────────────────────
msg_info "Verifiziere Installation (Service + Web UI)"
SVC_OK=""
for _ in $(seq 1 15); do
  if pct exec "$CTID" -- systemctl is-active --quiet crawl4ai 2>/dev/null; then
    SVC_OK=1
    break
  fi
  sleep 2
done
if [[ -z "$SVC_OK" ]]; then
  echo -e "${RD}--- systemctl status ---${CL}"
  pct exec "$CTID" -- systemctl status crawl4ai --no-pager || true
  echo -e "${RD}--- journalctl (letzte 60 Zeilen, komplette Kette) ---${CL}"
  pct exec "$CTID" -- journalctl -u crawl4ai -n 60 --no-pager || true
  die "crawl4ai.service läuft nicht — Logs siehe oben."
fi
pct exec "$CTID" -- curl -fsS "http://localhost:${APP_PORT}/api/health" \
  || die "Web UI antwortet nicht auf http://localhost:${APP_PORT}/api/health (journalctl -u crawl4ai prüfen)"
FRONT_CODE=$(pct exec "$CTID" -- bash -c "curl -s -o /dev/null -w '%{http_code}' http://localhost:${APP_PORT}/")
[[ "$FRONT_CODE" == "200" ]] || die "Frontend antwortet nicht (HTTP $FRONT_CODE statt 200)"
msg_ok "Installation verifiziert (Service aktiv + HTTP 200)"

# ── IP ermitteln ────────────────────────────────────────────────────
msg_info "Ermittle Container-IP"
CT_IP=""
for _ in $(seq 1 10); do
  CT_IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')
  [[ -n "$CT_IP" ]] && break
  sleep 2
done
[[ -n "$CT_IP" ]] || CT_IP="<CONTAINER-IP>"

# ── Zusammenfassung ─────────────────────────────────────────────────
echo ""
echo -e "  ${CM} ${GN}${APP} erfolgreich installiert!${CL}"
echo -e "  ${CM} Container-ID: ${YW}${CTID}${CL}"
echo -e "  ${CM} Web UI:       ${YW}http://${CT_IP}:${APP_PORT}${CL}"
echo -e "  ${CM} Health:       ${YW}http://${CT_IP}:${APP_PORT}/api/health${CL}"
echo -e "  ${CM} Service:      ${YW}systemctl status ${SERVICE}${CL} (im Container: pct enter ${CTID})"
echo -e "  ${CM} Logs:         ${YW}journalctl -u ${SERVICE} -f${CL}"
echo -e "  ${CM} Daten:        ${YW}${DATA_DIR}/crawls.db${CL}"
if [[ -n "$APP_PW" ]]; then
  echo -e "  ${CM} Login:        ${YW}Passwortschutz AKTIV (beim Installieren gesetzt)${CL}"
else
  echo -e "  ${CM} Login:        ${YW}kein Passwort (offen im Heimnetz)${CL}"
fi
echo ""
echo -e "  ${YW}So geht's weiter:${CL}"
echo -e "  ${YW}  1. Browser öffnen: http://${CT_IP}:${APP_PORT}${CL}"
echo -e "  ${YW}  2. Webseite eintragen (z. B. https://example.com) → Crawlen${CL}"
echo -e "  ${YW}  3. Markdown ansehen / laden, Verlauf bleibt in SQLite${CL}"
echo ""
echo -e "  ${YW}Update:${CL}    pct exec ${CTID} -- bash -c 'cd ${APP_DIR} && git pull && ./venv/bin/pip install -r app/requirements.txt && systemctl restart ${SERVICE}'"
echo -e "  ${YW}Deinstallieren:${CL} pct stop ${CTID} && pct destroy ${CTID} --purge"
echo ""
