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

# Einfache In-Memory Sessions (reicht für lokalen LXC; nach Reboot neu login)
SESSIONS: set[str] = set()

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


# ---------- Auth ----------
def is_protected() -> bool:
    return bool(APP_PASSWORD)


def check_auth(session: Optional[str]) -> bool:
    if not is_protected():
        return True
    return bool(session) and session in SESSIONS


def require_auth(session: Optional[str]):
    if not check_auth(session):
        raise HTTPException(status_code=401, detail="Login erforderlich")


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
        tok = secrets.token_hex(24)
        SESSIONS.add(tok)
        resp.set_cookie("crawl4ai_session", tok, httponly=True, samesite="lax", path="/")
        return {"ok": True}
    raise HTTPException(status_code=401, detail="Falsches Passwort")


@app.post("/api/logout")
def logout(session: Optional[str] = Cookie(default=None, alias="crawl4ai_session")):
    if session and session in SESSIONS:
        SESSIONS.discard(session)
    return {"ok": True}


@app.post("/api/crawl")
async def crawl(req: CrawlRequest, session: Optional[str] = Cookie(default=None, alias="crawl4ai_session")):
    require_auth(session)
    try:
        url = normalise_url(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    import time

    t0 = time.monotonic()
    try:
        # Timeout-Schutz: max 300s wie Crawl4AI-Docker-Default
        title, md, html, duration_ms = await asyncio.wait_for(
            run_crawl(url, req.mode, req.word_threshold), timeout=300
        )
        if req.mode == "fit":
            # fit_markdown steckt bereits in md (fit bevorzugt); Hinweis im Titel
            pass
        con = db()
        cur = con.execute(
            "INSERT INTO crawls(url,mode,status,title,markdown,html,error,duration_ms,created_at)"
            " VALUES(?,?,?,?,?,?,?, ?,?)",
            (url, req.mode, "ok", title, md[:500000], html[:500000], "",
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
            (url, req.mode, "error", "", "", "", err[:10000],
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
                (url, req.mode, "error", "", "", "", err[:10000],
                 int((time.monotonic() - t0) * 1000), datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
            con.close()
        except Exception:
            print(traceback.format_exc(), flush=True)
        raise HTTPException(status_code=500, detail=err[:3000])


@app.get("/api/crawls")
def list_crawls(limit: int = 50, session: Optional[str] = Cookie(default=None, alias="crawl4ai_session")):
    require_auth(session)
    limit = max(1, min(limit, 200))
    con = db()
    rows = con.execute(
        "SELECT id,url,mode,status,title,length(markdown) AS md_len,error,"
        "duration_ms,created_at FROM crawls ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    con.close()
    return {"crawls": [dict(r) for r in rows]}


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
</style></head><body>
<header><h1>🕷️ Crawl4AI Web</h1><span class="tag">lokal · LXC · Port 8000</span><span class="tag" id="authTag">…</span>
<span style="flex:1"></span><button class="ghost" id="logoutBtn" style="display:none">Logout</button></header>
<main>
<div class="card" id="login"><h3 style="margin-top:0">🔐 Login</h3>
<p class="mut">Diese Instanz ist passwortgeschützt. Passwort eingeben (beim Install-Script gesetzt).</p>
<div class="row"><input type="password" id="pw" placeholder="Passwort"><button onclick="doLogin()">Anmelden</button></div>
<p class="err" id="loginErr"></p></div>
<div class="card"><h3 style="margin-top:0">+ Neue Seite crawlen</h3>
<label>URL (z. B. https://example.com)</label>
<input id="url" placeholder="https://…" value="https://example.com">
<div class="row" style="margin-top:10px">
<div><label>Modus</label><select id="mode"><option value="markdown">Markdown (voll)</option><option value="fit">Fit-Markdown (KI-bereinigt)</option><option value="html">HTML (roh)</option></select></div>
<div><label>Wort-Schwelle</label><select id="wt"><option>0</option><option selected>10</option><option>50</option></select></div>
<div style="display:flex;align-items:flex-end"><button id="go" onclick="doCrawl()">🚀 Crawlen</button></div>
</div><p class="mut" id="status"></p></div>
<div class="card"><h3 style="margin-top:0">📄 Ergebnis</h3>
<div class="row" style="margin-bottom:8px"><button class="ghost" onclick="dl()">⬇ Markdown laden</button>
<button class="ghost" onclick="copyMd()">📋 Kopieren</button><span class="mut" id="meta"></span></div>
<pre id="out">Noch nichts gecrawlt. URL oben eintragen → Crawlen.</pre></div>
<div class="card"><h3 style="margin-top:0">🗂 Verlauf <button class="ghost" onclick="loadList()">↻</button></h3>
<table><thead><tr><th>ID</th><th>URL / Titel</th><th>Modus</th><th>Status</th><th>Info</th><th>Aktion</th></tr></thead>
<tbody id="list"></tbody></table></div>
</main>
<script>
let lastId=null, authed=true;
async function api(p,o={}){const r=await fetch(p,{credentials:'same-origin',...o});if(r.status===401){authed=false;showLogin();throw new Error('Login erforderlich');}return r;}
function showLogin(){document.getElementById('login').style.display='block';document.getElementById('authTag').textContent='🔒 geschützt';document.getElementById('logoutBtn').style.display='inline-block';}
async function init(){const c=await (await fetch('/api/config')).json();authed=!c.protected;if(c.protected){const t=await fetch('/api/crawls',{credentials:'same-origin'});if(t.status===401){showLogin();}else{document.getElementById('authTag').textContent='🔓 eingeloggt';document.getElementById('logoutBtn').style.display='inline-block';loadList();}}else{document.getElementById('authTag').textContent='offen (kein Login)';loadList();}}
async function doLogin(){const pw=document.getElementById('pw').value;const r=await fetch('/api/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});if(r.ok){location.reload();}else{document.getElementById('loginErr').textContent='Falsches Passwort';}}
document.getElementById('logoutBtn').onclick=async()=>{await fetch('/api/logout',{method:'POST',credentials:'same-origin'});location.reload();};
async function doCrawl(){const b=document.getElementById('go'),s=document.getElementById('status');b.disabled=true;s.textContent='⏳ Crawle … (Browser startet, kann 10–60s dauern)';try{const r=await api('/api/crawl',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:document.getElementById('url').value,mode:document.getElementById('mode').value,word_threshold:parseInt(document.getElementById('wt').value)})});const j=await r.json();if(!r.ok)throw new Error(j.detail||r.statusText);lastId=j.id;document.getElementById('out').textContent=j.markdown||'(leer)';document.getElementById('meta').textContent=`#${j.id} · ${j.markdown_len} Zeichen · ${j.duration_ms} ms · ${j.title||''}`;s.innerHTML='<span class="ok">✓ fertig (#'+j.id+')</span>';loadList();}catch(e){s.innerHTML='<span class="err">✗ '+String(e.message||e).slice(0,2000)+'</span>';}b.disabled=false;}
async function loadList(){try{const r=await api('/api/crawls?limit=50');const j=await r.json();document.getElementById('list').innerHTML=j.crawls.map(c=>`<tr><td>${c.id}</td><td><a href="${c.url}" target="_blank">${c.url.slice(0,60)}</a><br><span class="mut">${(c.title||'').slice(0,80)}</span></td><td>${c.mode}</td><td>${c.status==='ok'?'<span class="ok">ok</span>':'<span class="err">error</span>'}</td><td class="mut">${c.md_len||0} Z. · ${c.duration_ms} ms<br>${c.created_at||''}</td><td><button class="ghost" onclick="view(${c.id})">Ansehen</button> <button class="ghost" onclick="del(${c.id})">🗑</button></td></tr>`).join('')||'<tr><td colspan=6 class=mut>leer</td></tr>';}catch(e){}}
async function view(id){const r=await api('/api/crawls/'+id);const j=await r.json();lastId=id;document.getElementById('out').textContent=j.markdown||j.html||j.error||'(leer)';document.getElementById('meta').textContent=`#${j.id} · ${j.url}`;window.scrollTo({top:0,behavior:'smooth'});}
async function del(id){if(!confirm('Eintrag #'+id+' löschen?'))return;await api('/api/crawls/'+id,{method:'DELETE'});loadList();}
function dl(){if(!lastId)return alert('Erst crawlen');window.location='/api/crawls/'+lastId+'/markdown';}
function copyMd(){navigator.clipboard.writeText(document.getElementById('out').textContent);}
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
