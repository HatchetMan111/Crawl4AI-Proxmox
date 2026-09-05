#!/usr/bin/env python3
"""crawl4ai-web — lokale Web-UI zum Crawlen von Webseiten (FastAPI + Crawl4AI).

- URL eintragen -> crawlen -> Markdown/HTML ansehen, downloaden, Verlauf in SQLite.
- Optionaler Passwortschutz via Env CRAWL4AI_PASSWORD (leer = kein Login).
- Bindet auf 0.0.0.0, Port via Env PORT (Default 8000).
- Bei Fehlern: komplette Tracebacks in Logs + API-Antwort (detail).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import sqlite3
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

APP_NAME = "crawl4ai-web"
APP_PORT = int(os.environ.get("PORT", "8000"))
DATA_DIR = Path(os.environ.get("CRAWL4AI_DATA_DIR", "/var/lib/crawl4ai"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "crawls.db"
APP_PASSWORD = os.environ.get("CRAWL4AI_PASSWORD", "")

# ---------- Auth (stateless, signierte Cookies — überleben Reboots) ----------
COOKIE_NAME = "crawl4ai_session"
SESSION_DAYS = 30


def is_protected() -> bool:
    return bool(APP_PASSWORD)


def _session_secret() -> bytes:
    # Aus dem Passwort abgeleitet: kein Server-State nötig,
    # Sessions überleben Container-Reboots und Service-Restarts.
    return hashlib.sha256(f"crawl4ai-web|{APP_PASSWORD}".encode()).digest()


def _sign_session(token: str) -> str:
    sig = hmac.new(_session_secret(), token.encode(), hashlib.sha256).hexdigest()
    return f"{token}.{sig}"


def check_auth(session: Optional[str]) -> bool:
    if not is_protected():
        return True
    if not session or "." not in session:
        return False
    token, _, sig = session.rpartition(".")
    if not token or not sig:
        return False
    expected = hmac.new(_session_secret(), token.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def require_auth(session: Optional[str]):
    if not check_auth(session):
        raise HTTPException(status_code=401, detail="Sitzung abgelaufen — bitte neu anmelden")


app = FastAPI(title=APP_NAME)


# ---------- DB ----------
def db() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE IF NOT EXISTS crawls(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          url TEXT NOT NULL,
          mode TEXT NOT NULL DEFAULT 'markdown',
          status TEXT NOT NULL DEFAULT 'ok',
          title TEXT DEFAULT '',
          markdown TEXT DEFAULT '',
          html TEXT DEFAULT '',
          error TEXT DEFAULT '',
          duration_ms INTEGER DEFAULT 0,
          created_at TEXT NOT NULL
        )"""
    )
    con.commit()
    return con


# ---------- Models ----------
class CrawlRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=2000)
    mode: str = Field(default="markdown", pattern="^(markdown|fit|html)$")
    word_threshold: int = Field(default=10, ge=0, le=1000)


def normalise_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ValueError("URL ist leer")
    if any(c.isspace() for c in raw):
        raise ValueError("URL enthält Leerzeichen — bitte gültige URL eingeben")
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    if p.scheme not in ("http", "https"):
        raise ValueError(f"Nur http(s) erlaubt, erhalten: {p.scheme}")
    if not p.hostname:
        raise ValueError("Ungültige URL (kein Host)")
    if not p.netloc or ("." not in p.netloc and p.hostname != "localhost"):
        # grobe Host-Prüfung (IP-Adressen enthalten Punkte, Hostnamen auch)
        if not p.hostname or not all(ch.isalnum() or ch in ".-:@" for ch in p.netloc):
            raise ValueError(f"Ungültiger Host in URL: {p.netloc!r}")
    if p.hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or str(p.hostname).endswith(
        (".local", ".lan", ".internal")
    ):
        # Schutz vor SSRF auf interne Dienste — bewusst locker für Heimnetz,
        # aber Loopback wird geblockt (Docker-API-Härtung analog zu Crawl4AI 0.9).
        if p.hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            raise ValueError("Loopback-Adressen sind blockiert (SSRF-Schutz)")
    return raw


async def run_crawl(url: str, mode: str, word_threshold: int):
    """Führt den eigentlichen Crawl aus. Gibt (title, markdown, html) zurück.
    Wirft Exception mit komplettem Traceback bei Fehlern (wird geloggt + gespeichert)."""
    import time

    t0 = time.monotonic()
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    except Exception as e:
        raise RuntimeError(
            f"crawl4ai Import fehlgeschlagen: {e}\n{traceback.format_exc()}"
        ) from e

    browser_cfg = BrowserConfig(headless=True, verbose=False)
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        word_count_threshold=word_threshold,
    )
    try:
        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            result = await crawler.arun(url=url, config=run_cfg)
    except Exception as e:
        raise RuntimeError(f"Crawler-Lauf fehlgeschlagen: {e}\n{traceback.format_exc()}") from e

    if not result or not result.success:
        msg = getattr(result, "error_message", "unbekannter Fehler") if result else "kein Result"
        raise RuntimeError(f"Crawl nicht erfolgreich für {url}: {msg}")

    md = getattr(result, "markdown", "") or ""
    # crawl4ai liefert markdown als str oder Objekt mit raw/fit_markdown
    if not isinstance(md, str):
        try:
            md = md.fit_markdown or md.raw_markdown or str(md)
        except Exception:
            md = str(md)
    html = getattr(result, "cleaned_html", "") or getattr(result, "html", "") or ""
    title = getattr(result, "metadata", None) or {}
    title_str = ""
    try:
        title_str = (title.get("title", "") if isinstance(title, dict) else str(title))[:300]
    except Exception:
        title_str = ""
    duration_ms = int((time.monotonic() - t0) * 1000)
    return title_str, md, html, duration_ms


# ---------- API ----------
@app.get("/api/health")
def health():
    return {"status": "ok", "app": APP_NAME, "protected": is_protected(), "db": str(DB_PATH)}


@app.get("/api/config")
def config():
    return {"protected": is_protected(), "app": APP_NAME}


@app.get("/api/me")
def me(session: Optional[str] = Cookie(default=None, alias="crawl4ai_session")):
    return {"protected": is_protected(), "authed": check_auth(session), "app": APP_NAME}


@app.post("/api/login")
async def login(req: Request, resp: Response):
    if not is_protected():
        return {"ok": True, "message": "kein Passwort gesetzt"}
    try:
        body = await req.json()
    except Exception:
        body = {}
    pw = str(body.get("password", ""))
    if hmac.compare_digest(pw, APP_PASSWORD):
        tok = _sign_session(secrets.token_hex(24))
        resp.set_cookie(
            COOKIE_NAME, tok, httponly=True, samesite="lax", path="/",
            max_age=SESSION_DAYS * 86400,
        )
        return {"ok": True}
    raise HTTPException(status_code=401, detail="Falsches Passwort")


@app.post("/api/logout")
def logout(resp: Response):
    # Stateless-Session: serverseitig nichts zu löschen — Cookie beim Client entfernen.
    resp.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@app.post("/api/crawl")
async def crawl(req: CrawlRequest, session: Optional[str] = Cookie(default=None, alias="crawl4ai_session")):
    require_auth(session)
    try:
        url = normalise_url(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await _run_and_store(url, req.mode, req.word_threshold)


async def _run_and_store(url: str, mode: str, word_threshold: int):
    """Crawlt eine URL, speichert das Ergebnis in SQLite und gibt die Kurzfassung zurück.
    Wird von /api/crawl und /api/crawls/{id}/retry gemeinsam genutzt."""
    import time

    t0 = time.monotonic()
    try:
        # Timeout-Schutz: max 300s wie Crawl4AI-Docker-Default
        title, md, html, duration_ms = await asyncio.wait_for(
            run_crawl(url, mode, word_threshold), timeout=300
        )
        if mode == "fit":
            # fit_markdown steckt bereits in md (fit bevorzugt); Hinweis im Titel
            pass
        con = db()
        cur = con.execute(
            "INSERT INTO crawls(url,mode,status,title,markdown,html,error,duration_ms,created_at)"
            " VALUES(?,?,?,?,?,?,?, ?,?)",
            (url, mode, "ok", title, md[:500000], html[:500000], "",
             duration_ms, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
        cid = cur.lastrowid
        con.close()
        return {
            "id": cid, "url": url, "title": title,
            "markdown_len": len(md), "html_len": len(html),
            "markdown": md[:200000], "duration_ms": duration_ms,
        }
    except asyncio.TimeoutError:
        err = f"Timeout nach 300s für {url}\n{traceback.format_exc()}"
        print(err, flush=True)
        con = db()
        con.execute(
            "INSERT INTO crawls(url,mode,status,title,markdown,html,error,duration_ms,created_at)"
            " VALUES(?,?,?,?,?,?,?, ?,?)",
            (url, mode, "error", "", "", "", err[:10000],
             int((time.monotonic() - t0) * 1000), datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
        con.close()
        raise HTTPException(status_code=504, detail=err[:2000])
    except HTTPException:
        raise
    except Exception as e:
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        print(err, flush=True)
        try:
            con = db()
            con.execute(
                "INSERT INTO crawls(url,mode,status,title,markdown,html,error,duration_ms,created_at)"
                " VALUES(?,?,?,?,?,?,?, ?,?)",
                (url, mode, "error", "", "", "", err[:10000],
                 int((time.monotonic() - t0) * 1000), datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
            con.close()
        except Exception:
            print(traceback.format_exc(), flush=True)
        raise HTTPException(status_code=500, detail=err[:3000])


@app.get("/api/crawls")
def list_crawls(
    limit: int = 50,
    q: str = "",
    status: str = "",
    session: Optional[str] = Cookie(default=None, alias="crawl4ai_session"),
):
    require_auth(session)
    limit = max(1, min(limit, 200))
    clauses, params = [], []
    if q.strip():
        clauses.append("(url LIKE ? OR title LIKE ?)")
        params += [f"%{q.strip()}%", f"%{q.strip()}%"]
    if status in ("ok", "error"):
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    con = db()
    rows = con.execute(
        "SELECT id,url,mode,status,title,length(markdown) AS md_len,error,"
        f"duration_ms,created_at FROM crawls {where} ORDER BY id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    con.close()
    return {"crawls": [dict(r) for r in rows]}


@app.get("/api/stats")
def stats(session: Optional[str] = Cookie(default=None, alias="crawl4ai_session")):
    require_auth(session)
    con = db()
    row = con.execute(
        "SELECT count(*) AS total,"
        " sum(status='ok') AS ok, sum(status='error') AS err,"
        " coalesce(sum(length(markdown)),0) AS chars,"
        " max(created_at) AS last FROM crawls"
    ).fetchone()
    con.close()
    return dict(row)


@app.delete("/api/crawls")
def del_all_crawls(session: Optional[str] = Cookie(default=None, alias="crawl4ai_session")):
    require_auth(session)
    con = db()
    cur = con.execute("DELETE FROM crawls")
    con.commit()
    con.close()
    return {"ok": True, "deleted": cur.rowcount}


@app.post("/api/crawls/{cid}/retry")
async def retry_crawl(cid: int, session: Optional[str] = Cookie(default=None, alias="crawl4ai_session")):
    require_auth(session)
    con = db()
    row = con.execute("SELECT url,mode FROM crawls WHERE id=?", (cid,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(status_code=404, detail="nicht gefunden")
    try:
        url = normalise_url(row["url"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await _run_and_store(url, row["mode"] or "markdown", 10)


@app.get("/api/crawls/{cid}")
def get_crawl(cid: int, session: Optional[str] = Cookie(default=None, alias="crawl4ai_session")):
    require_auth(session)
    con = db()
    row = con.execute("SELECT * FROM crawls WHERE id=?", (cid,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(status_code=404, detail="nicht gefunden")
    return dict(row)


@app.get("/api/crawls/{cid}/markdown")
def get_markdown(cid: int, session: Optional[str] = Cookie(default=None, alias="crawl4ai_session")):
    require_auth(session)
    con = db()
    row = con.execute("SELECT markdown,url FROM crawls WHERE id=?", (cid,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(status_code=404, detail="nicht gefunden")
    safe = "".join(c if c.isalnum() else "_" for c in (row["url"] or "crawl"))[:60]
    return PlainTextResponse(
        row["markdown"] or "",
        headers={"Content-Disposition": f'attachment; filename="crawl-{cid}-{safe}.md"'},
    )


@app.delete("/api/crawls/{cid}")
def del_crawl(cid: int, session: Optional[str] = Cookie(default=None, alias="crawl4ai_session")):
    require_auth(session)
    con = db()
    con.execute("DELETE FROM crawls WHERE id=?", (cid,))
    con.commit()
    con.close()
    return {"ok": True}


# ---------- UI ----------
PAGE = """<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crawl4AI Web — lokale Crawl-Station</title>
<style>
:root{--bg:#0f1420;--card:#1a2233;--line:#2a3650;--txt:#e8eef7;--mut:#93a1b8;--acc:#5aa2ff;--ok:#3ecf8e;--err:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:18px 22px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:18px;margin:0}header .tag{font-size:12px;color:var(--mut);border:1px solid var(--line);border-radius:20px;padding:2px 10px}
main{max-width:1100px;margin:0 auto;padding:20px;display:grid;gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
label{font-size:13px;color:var(--mut);display:block;margin-bottom:6px}
input,select,textarea{width:100%;background:#0d1320;border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:10px 12px;font-size:14px}
.row{display:flex;gap:10px;flex-wrap:wrap}.row>*{flex:1;min-width:160px}
button{background:var(--acc);border:0;color:#06101f;font-weight:700;border-radius:8px;padding:10px 16px;cursor:pointer;font-size:14px}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--txt);font-weight:400}
button:disabled{opacity:.5;cursor:wait}
pre{white-space:pre-wrap;word-break:break-word;background:#0d1320;border:1px solid var(--line);border-radius:8px;padding:12px;max-height:480px;overflow:auto;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
a{color:var(--acc)}.mut{color:var(--mut);font-size:13px}.ok{color:var(--ok)}.err{color:var(--err)}
#login{display:none}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--mut);margin-right:6px;vertical-align:baseline}
.dot.on{background:var(--ok)}.dot.off{background:var(--err)}
#toasts{position:fixed;top:12px;right:12px;display:grid;gap:8px;z-index:50;max-width:min(420px,90vw)}
.toast{background:#0d1320;border:1px solid var(--line);border-left:4px solid var(--acc);border-radius:8px;padding:10px 14px;font-size:13px;box-shadow:0 4px 18px rgba(0,0,0,.4)}
.toast.ok{border-left-color:var(--ok)}.toast.err{border-left-color:var(--err)}
.spin{display:inline-block;animation:rot 1s linear infinite}@keyframes rot{to{transform:rotate(360deg)}}
.hidden{display:none!important}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
.stat{background:#0d1320;border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.stat b{font-size:20px;display:block}.stat span{font-size:12px;color:var(--mut)}
.tabs{display:flex;gap:6px;margin-bottom:8px}
.tabs button{padding:6px 12px;font-size:13px;font-weight:400}
.tabs button.active{background:var(--acc);color:#06101f;font-weight:700}
textarea#bulk{min-height:90px;font-family:inherit;resize:vertical}
.progress{height:8px;background:#0d1320;border:1px solid var(--line);border-radius:6px;overflow:hidden;margin-top:8px}
.progress>div{height:100%;background:var(--acc);width:0%}
</style></head><body>
<header><h1>🕷️ Crawl4AI Web</h1><span class="tag">lokal · LXC · Port 8000</span><span class="tag"><span class="dot" id="healthDot"></span><span id="healthTxt">verbinde …</span></span><span class="tag" id="authTag">…</span>
<span style="flex:1"></span><button class="ghost hidden" id="logoutBtn">Abmelden</button></header>
<div id="toasts"></div>
<main>
<div class="card" id="login"><h3 style="margin-top:0">🔐 Anmelden</h3>
<p class="mut">Diese Instanz ist passwortgeschützt. Nach der Anmeldung bleibst du eingeloggt — auch über Container-Neustarts hinweg.</p>
<div class="row"><input type="password" id="pw" placeholder="Passwort" autocomplete="current-password"><button id="loginBtn" onclick="doLogin()">Anmelden</button></div>
<p class="err" id="loginErr"></p></div>
<div class="card" id="dash"><h3 style="margin-top:0">📊 Übersicht <button class="ghost" onclick="loadStats()">↻</button></h3>
<div class="stats">
<div class="stat"><b id="statTotal">–</b><span>Crawls gesamt</span></div>
<div class="stat"><b id="statOk" class="ok">–</b><span>erfolgreich</span></div>
<div class="stat"><b id="statErr" class="err">–</b><span>fehlerhaft</span></div>
<div class="stat"><b id="statChars">–</b><span>Zeichen gesamt</span></div>
</div><p class="mut" id="statLast" style="margin-bottom:0"></p></div>
<div class="card"><h3 style="margin-top:0">+ Neue Seite crawlen</h3>
<label>URL (z. B. https://example.com)</label>
<input id="url" placeholder="https://…" value="https://example.com">
<div class="row" style="margin-top:10px">
<div><label>Modus</label><select id="mode"><option value="markdown">Markdown (voll)</option><option value="fit">Fit-Markdown (KI-bereinigt)</option><option value="html">HTML (roh)</option></select></div>
<div><label>Wort-Schwelle</label><select id="wt"><option>0</option><option selected>10</option><option>50</option></select></div>
<div style="display:flex;align-items:flex-end"><button id="go" onclick="doCrawl()">🚀 Crawlen</button></div>
</div><p class="mut" id="status"></p>
<details style="margin-top:6px"><summary class="mut" style="cursor:pointer">📦 Mehrere URLs auf einmal (Bulk, eine pro Zeile)</summary>
<div style="margin-top:8px"><textarea id="bulk" placeholder="https://seite1.de&#10;https://seite2.de/artikel&#10;https://seite3.de"></textarea>
<div class="row" style="margin-top:8px"><div style="display:flex;align-items:flex-end"><button id="goBulk" onclick="doBulk()">📦 Bulk starten</button></div></div>
<div class="progress hidden" id="bulkBar"><div id="bulkFill"></div></div>
<p class="mut" id="bulkStatus"></p></div></details></div>
<div class="card"><h3 style="margin-top:0">📄 Ergebnis</h3>
<div class="tabs"><button id="tabMd" class="active" onclick="showTab('md')">Markdown</button><button id="tabHtml" onclick="showTab('html')">HTML</button><button id="tabErr" onclick="showTab('err')">Fehler-Info</button></div>
<div class="row" style="margin-bottom:8px"><button class="ghost" onclick="dl()">⬇ Markdown laden</button>
<button class="ghost" onclick="copyMd()">📋 Kopieren</button><button class="ghost" id="retryBtn" onclick="retryLast()">↻ Erneut crawlen</button><span class="mut" id="meta"></span></div>
<pre id="out">Noch nichts gecrawlt. URL oben eintragen → Crawlen.</pre></div>
<div class="card"><h3 style="margin-top:0">🗂 Verlauf <button class="ghost" onclick="loadList()">↻</button> <button class="ghost" onclick="delAll()">🗑 Alle löschen</button></h3>
<div class="row" style="margin-bottom:8px"><div><input id="q" placeholder="🔍 Suchen in URL / Titel …"></div>
<div style="max-width:190px"><select id="fStatus"><option value="">alle Status</option><option value="ok">nur erfolgreich</option><option value="error">nur Fehler</option></select></div></div>
<table><thead><tr><th>ID</th><th>URL / Titel</th><th>Modus</th><th>Status</th><th>Info</th><th>Aktion</th></tr></thead>
<tbody id="list"></tbody></table></div>
</main>
<script>
let lastId=null, current=null, curTab='md';
const $=id=>document.getElementById(id);
function toast(msg,kind=''){const t=document.createElement('div');t.className='toast '+kind;t.textContent=msg;$('toasts').appendChild(t);setTimeout(()=>t.remove(),kind==='err'?9000:4500);}
function setAuthed(on,label){$('authTag').textContent=label;$('login').style.display=on?'none':'block';$('logoutBtn').classList.toggle('hidden',!on||label.startsWith('offen'));}
async function api(p,o={}){
  let r;
  try{r=await fetch(p,{credentials:'same-origin',...o});}
  catch(e){throw new Error('Server nicht erreichbar ('+p+'): '+e.message);}
  if(r.status===401){setAuthed(false,'🔒 Sitzung abgelaufen');toast('Sitzung abgelaufen — bitte neu anmelden.','err');throw new Error('Sitzung abgelaufen — bitte neu anmelden.');}
  return r;
}
async function health(auto=false){
  try{const r=await fetch('/api/health');const j=await r.json();
    if(j.status==='ok'){$('healthDot').className='dot on';$('healthTxt').textContent='Server online';return true;}
    throw new Error('unbekannt');
  }catch(e){$('healthDot').className='dot off';$('healthTxt').textContent='Server offline';if(!auto)toast('Server nicht erreichbar: '+e.message,'err');return false;}
}
async function init(){
  $('pw').addEventListener('keydown',e=>{if(e.key==='Enter')doLogin();});
  $('url').addEventListener('keydown',e=>{if(e.key==='Enter')doCrawl();});
  $('q').addEventListener('keydown',e=>{if(e.key==='Enter')loadList();});
  $('q').addEventListener('input',()=>{clearTimeout(window._qt);window._qt=setTimeout(loadList,400);});
  $('fStatus').addEventListener('change',loadList);
  $('logoutBtn').onclick=doLogout;
  if(!await health(true)){$('authTag').textContent='⚠ offline';setTimeout(init,5000);return;}
  try{
    const me=await (await api('/api/me')).json();
    if(!me.protected){setAuthed(true,'offen (kein Login)');await refresh();}
    else if(me.authed){setAuthed(true,'🔓 eingeloggt');$('loginErr').textContent='';await refresh();}
    else{setAuthed(false,'🔒 geschützt');}
  }catch(e){/* api() meldet bereits via toast */}
  setInterval(()=>health(true),15000);
}
async function refresh(){await Promise.all([loadList(),loadStats()]);}
async function loadStats(){
  try{
    const j=await (await api('/api/stats')).json();
    $('statTotal').textContent=j.total??0;$('statOk').textContent=j.ok??0;$('statErr').textContent=j.err??0;
    $('statChars').textContent=fmtNum(j.chars??0);
    $('statLast').textContent=j.last?('Letzter Crawl: '+j.last):'Noch nichts gecrawlt.';
  }catch(e){/* still */}
}
function fmtNum(n){return n>=1e6?(n/1e6).toFixed(1)+' Mio':n>=1e3?(n/1e3).toFixed(1)+'k':String(n);}
async function doLogin(){
  const b=$('loginBtn');b.disabled=true;$('loginErr').textContent='';
  try{
    const r=await fetch('/api/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:$('pw').value})});
    const j=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error((j.detail||'Anmeldung fehlgeschlagen')+' (HTTP '+r.status+')');
    $('pw').value='';setAuthed(true,'🔓 eingeloggt');toast('✓ Erfolgreich angemeldet.');
    await loadList();
  }catch(e){$('loginErr').textContent='✗ '+e.message;toast('Anmeldung fehlgeschlagen: '+e.message,'err');}
  b.disabled=false;
}
async function doLogout(){try{await fetch('/api/logout',{method:'POST',credentials:'same-origin'});}catch(e){}setAuthed(false,'🔒 geschützt');toast('Abgemeldet.');}
async function doCrawl(){
  const b=$('go'),s=$('status');b.disabled=true;
  const t0=Date.now(),tick=setInterval(()=>{s.innerHTML='<span class="mut"><span class="spin">⏳</span> Crawle … '+Math.round((Date.now()-t0)/1000)+'s (Browser startet, kann 10–60s dauern)</span>';},500);
  s.innerHTML='<span class="mut"><span class="spin">⏳</span> Crawle …</span>';
  try{
    const r=await api('/api/crawl',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:$('url').value,mode:$('mode').value,word_threshold:parseInt($('wt').value)})});
    const j=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error((typeof j.detail==='string'?j.detail:r.statusText||('HTTP '+r.status)));
    lastId=j.id;$('out').textContent=j.markdown||'(leer — Seite lieferte keinen Text)';
    $('meta').textContent=`#${j.id} · ${j.markdown_len} Zeichen · ${j.duration_ms} ms · ${j.title||''}`;
    s.innerHTML='<span class="ok">✓ fertig (#'+j.id+')</span>';toast('✓ Crawl #'+j.id+' fertig ('+j.markdown_len+' Zeichen).');
    await view(j.id);await refresh();
  }catch(e){clearInterval(tick);s.innerHTML='<span class="err">✗ '+String(e.message||e).slice(0,2000)+'</span>';$('out').textContent='Fehler:\n'+String(e.message||e).slice(0,4000);toast('Crawl fehlgeschlagen: '+String(e.message||e).slice(0,300),'err');}
  finally{clearInterval(tick);b.disabled=false;}
}
async function doBulk(){
  const urls=$('bulk').value.split('\n').map(s=>s.trim()).filter(Boolean);
  if(!urls.length){toast('Keine URLs im Bulk-Feld — eine URL pro Zeile eintragen.','err');return;}
  const b=$('goBulk');b.disabled=true;$('bulkBar').classList.remove('hidden');
  let ok=0,fail=0,lastOkId=null;
  for(let i=0;i<urls.length;i++){
    $('bulkStatus').textContent=`⏳ ${i+1}/${urls.length}: ${urls[i].slice(0,80)}`;
    $('bulkFill').style.width=Math.round(i/urls.length*100)+'%';
    try{
      const r=await api('/api/crawl',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:urls[i],mode:$('mode').value,word_threshold:parseInt($('wt').value)})});
      const j=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(typeof j.detail==='string'?j.detail.slice(0,300):('HTTP '+r.status));
      ok++;lastOkId=j.id;
    }catch(e){fail++;toast(`Bulk ${i+1}/${urls.length} fehlgeschlagen (${urls[i].slice(0,60)}): `+String(e.message||e).slice(0,200),'err');}
  }
  $('bulkFill').style.width='100%';
  $('bulkStatus').innerHTML=`<span class="ok">✓ ${ok} ok</span> · <span class="err">${fail} Fehler</span> von ${urls.length}`;
  toast(`Bulk fertig: ${ok} ok, ${fail} Fehler von ${urls.length}.`,fail?'err':'');
  b.disabled=false;if(lastOkId)await view(lastOkId);await refresh();
}
async function loadList(){
  try{
    const qp=new URLSearchParams({limit:50,q:$('q').value,status:$('fStatus').value});
    const r=await api('/api/crawls?'+qp);const j=await r.json();
    $('list').innerHTML=j.crawls.map(c=>`<tr><td>${c.id}</td><td><a href="${c.url}" target="_blank" rel="noopener">${esc(c.url).slice(0,60)}</a><br><span class="mut">${esc(c.title||'').slice(0,80)}</span></td><td>${c.mode}</td><td>${c.status==='ok'?'<span class="ok">ok</span>':'<span class="err">error</span>'}</td><td class="mut">${c.md_len||0} Z. · ${c.duration_ms} ms<br>${esc(c.created_at||'')}</td><td><button class="ghost" onclick="view(${c.id})">Ansehen</button> <button class="ghost" onclick="retry(${c.id})" title="Gleiche URL erneut crawlen">↻</button> <button class="ghost" onclick="del(${c.id})">🗑</button></td></tr>`).join('')||'<tr><td colspan=6 class=mut>leer — noch nichts gecrawlt (oder Filter passt nicht)</td></tr>';
  }catch(e){$('list').innerHTML='<tr><td colspan=6 class=err>Verlauf lädt nicht: '+esc(String(e.message||e)).slice(0,300)+'</td></tr>';toast('Verlauf lädt nicht: '+String(e.message||e).slice(0,300),'err');}
}
function showTab(t){curTab=t;['md','html','err'].forEach(k=>$('tab'+k[0].toUpperCase()+k.slice(1)).classList.toggle('active',k===t));renderTab();}
function renderTab(){
  if(!current){$('out').textContent='Noch nichts ausgewählt.';return;}
  $('out').textContent=curTab==='md'?(current.markdown||'(kein Markdown)') : curTab==='html'?(current.html||'(kein HTML gespeichert)') : (current.error||'(kein Fehler — Crawl war erfolgreich)');
}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function view(id){try{const r=await api('/api/crawls/'+id);const j=await r.json();if(!r.ok)throw new Error(j.detail||('HTTP '+r.status));lastId=id;current=j;showTab(curTab);$('meta').textContent=`#${j.id} · ${j.url} · ${j.status}`;window.scrollTo({top:0,behavior:'smooth'});}catch(e){toast('Eintrag lädt nicht: '+e.message,'err');}}
async function retry(id){
  try{
    toast('↻ Crawle #'+id+' erneut …');
    const r=await api('/api/crawls/'+id+'/retry',{method:'POST'});const j=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error(typeof j.detail==='string'?j.detail.slice(0,500):('HTTP '+r.status));
    toast('✓ Retry fertig als #'+j.id+' ('+j.markdown_len+' Zeichen).');await view(j.id);await refresh();
  }catch(e){toast('Retry fehlgeschlagen: '+String(e.message||e).slice(0,300),'err');}
}
async function retryLast(){if(!lastId){toast('Erst crawlen oder einen Verlaufseintrag ansehen.','err');return;}await retry(lastId);}
async function delAll(){const n=parseInt(($('statTotal')||{textContent:'?'}).textContent)||'?';if(!confirm('Wirklich ALLE Verlaufseinträge löschen? ('+n+' Einträge)'))return;try{const r=await api('/api/crawls',{method:'DELETE'});const j=await r.json();if(!r.ok)throw new Error('HTTP '+r.status);toast('🗑 '+(j.deleted??'?')+' Einträge gelöscht.');current=null;lastId=null;renderTab();await refresh();}catch(e){toast('Löschen fehlgeschlagen: '+e.message,'err');}}
async function del(id){if(!confirm('Eintrag #'+id+' löschen?'))return;try{const r=await api('/api/crawls/'+id,{method:'DELETE'});if(!r.ok)throw new Error('HTTP '+r.status);toast('Eintrag #'+id+' gelöscht.');await loadList();}catch(e){toast('Löschen fehlgeschlagen: '+e.message,'err');}}
function dl(){if(!lastId){toast('Erst crawlen oder einen Verlaufseintrag ansehen.','err');return;}window.location='/api/crawls/'+lastId+'/markdown';}
async function copyMd(){try{await navigator.clipboard.writeText($('out').textContent);toast('✓ In Zwischenablage kopiert.');}catch(e){toast('Kopieren fehlgeschlagen: '+e.message,'err');}}
init();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.exception_handler(Exception)
async def all_errors(req: Request, exc: Exception):
    # Komplette Kette loggen, nie nur letzte Zeile
    tb = traceback.format_exc()
    print(f"UNHANDLED {type(exc).__name__}: {exc}\n{tb}", flush=True)
    if isinstance(exc, HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return JSONResponse({"detail": f"{type(exc).__name__}: {exc}\n{tb}"[:4000]}, status_code=500)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=APP_PORT)
