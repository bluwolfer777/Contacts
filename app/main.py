"""Rubrica aziendale condivisa — applicazione FastAPI."""
from __future__ import annotations

import csv
import html
import io
import logging
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.sessions import SessionMiddleware

from . import db
from .auth import AuthError, AuthUser, NotKerberos, ldap_login, sso_login
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rubrica")

STATIC = Path(__file__).parent / "static"
NO_SSO_COOKIE = "rubrica_nosso"

app = FastAPI(title=settings.app_title, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="rubrica_session",
    max_age=settings.session_hours * 3600,
    same_site="lax",
    https_only=False,  # il servizio è pubblicato in HTTP
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# ---------------------------------------------------------------- sessione / permessi

def session_user(request: Request) -> AuthUser | None:
    data = request.session.get("user")
    if not data:
        return None
    try:
        return AuthUser(**data)
    except TypeError:
        request.session.clear()
        return None


def require_user(request: Request) -> AuthUser:
    user = session_user(request)
    if not user:
        raise HTTPException(401, "Non autenticato")
    return user


def require_admin(request: Request, user: AuthUser = Depends(require_user)) -> AuthUser:
    # Protezione CSRF: le richieste di modifica devono arrivare dal nostro JavaScript.
    if request.headers.get("x-requested-with") != "rubrica":
        raise HTTPException(403, "Richiesta non valida")
    if not user.is_admin:
        raise HTTPException(403, "Non hai i permessi per modificare la rubrica")
    return user


def _login_redirect(request: Request) -> RedirectResponse:
    if settings.sso_available and not request.cookies.get(NO_SSO_COOKIE):
        return RedirectResponse("/auth/sso", status_code=303)
    return RedirectResponse("/login", status_code=303)


def _start_session(request: Request, user: AuthUser) -> None:
    request.session.clear()
    request.session["user"] = user.to_session()
    log.info("Accesso %s: %s (admin=%s)", user.method, user.username, user.is_admin)


# ---------------------------------------------------------------- pagine

@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def index(request: Request):
    if not session_user(request):
        return _login_redirect(request)
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(page.replace("{{TITLE}}", html.escape(settings.app_title)))


def _login_page(error: str = "", username: str = "", status: int = 200) -> HTMLResponse:
    page = (STATIC / "login.html").read_text(encoding="utf-8")
    sso = ('<a class="btn secondary wide" href="/auth/sso?retry=1">Usa la sessione di Windows</a>'
           if settings.sso_available else "")
    domain = html.escape(settings.ldap_domain)
    page = (page.replace("{{TITLE}}", html.escape(settings.app_title))
                .replace("{{ERROR}}", f'<p class="error">{html.escape(error)}</p>' if error else "")
                .replace("{{USERNAME}}", html.escape(username))
                .replace("{{DOMAIN}}", domain)
                .replace("{{SSO}}", sso))
    return HTMLResponse(page, status_code=status)


@app.get("/login", include_in_schema=False)
def login_form(request: Request, error: str = ""):
    messages = {
        "sso": "Accesso automatico con Windows non disponibile da questo computer: inserisci le credenziali.",
        "ntlm": "Il browser non ha usato Kerberos per questo indirizzo: inserisci le credenziali.",
    }
    return _login_page(messages.get(error, error[:200]))


@app.post("/login", include_in_schema=False)
def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    try:
        user = ldap_login(username, password)
    except AuthError as e:
        return _login_page(str(e), username, status=401)
    _start_session(request, user)
    return RedirectResponse("/", status_code=303)


@app.get("/auth/sso", include_in_schema=False)
def auth_sso(request: Request):
    if not settings.sso_available:
        return RedirectResponse("/login", status_code=303)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("negotiate "):
        try:
            user = sso_login(header)
        except NotKerberos:
            return RedirectResponse("/login?error=ntlm", status_code=303)
        except AuthError as e:
            return RedirectResponse("/login?error=" + quote(str(e)), status_code=303)
        _start_session(request, user)
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(NO_SSO_COOKIE)
        return resp
    # Nessun ticket: chiediamo al browser di negoziare. Se non è in grado, il corpo
    # della risposta lo porta subito al form di accesso.
    body = ('<!doctype html><meta http-equiv="refresh" content="0;url=/login?error=sso">'
            '<a href="/login?error=sso">Accedi</a>')
    return HTMLResponse(body, status_code=401, headers={"WWW-Authenticate": "Negotiate"})


@app.post("/logout", include_in_schema=False)
def logout(request: Request):
    request.session.clear()
    resp = RedirectResponse("/login", status_code=303)
    # Dopo un logout esplicito non rientriamo in automatico con la sessione Windows
    resp.set_cookie(NO_SSO_COOKIE, "1", max_age=settings.session_hours * 3600, httponly=True, samesite="lax")
    return resp


# ---------------------------------------------------------------- API

class ContactIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    phones: list[str] = Field(default_factory=list, max_length=50)
    emails: list[str] = Field(default_factory=list, max_length=50)
    tags: list[str] = Field(default_factory=list, max_length=50)
    notes: str = Field("", max_length=10000)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Il nome è obbligatorio")
        return v

    @field_validator("phones")
    @classmethod
    def _phones(cls, v: list[str]) -> list[str]:
        for p in v:
            if len(p) > 50 or (p.strip() and not re.fullmatch(r"[0-9+()./\-\s]*(\s*(int|ext|x)\.?\s*\d+)?", p.strip(), re.I)):
                raise ValueError(f"Numero di telefono non valido: {p}")
        return v

    @field_validator("emails")
    @classmethod
    def _emails(cls, v: list[str]) -> list[str]:
        for e in v:
            if e.strip() and (len(e) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e.strip())):
                raise ValueError(f"Indirizzo email non valido: {e}")
        return v

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        for t in v:
            if len(t) > 50:
                raise ValueError(f"Tag troppo lungo: {t}")
        return v


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/api/me")
def api_me(user: AuthUser = Depends(require_user)):
    return {**user.to_session(), "title": settings.app_title, "sso": settings.sso_available}


@app.get("/api/contacts")
def api_list(
    q: str = "", name: str = "", phone: str = "", email: str = "", notes: str = "",
    tag: list[str] = Query(default=[]), untagged: bool = False,
    user: AuthUser = Depends(require_user),
):
    items = db.search(q=q, name=name, phone=phone, email=email, notes=notes, tags=tag, untagged=untagged)
    return {"items": items, "total": db.count()}


@app.get("/api/contacts/{contact_id}")
def api_get(contact_id: int, user: AuthUser = Depends(require_user)):
    c = db.get(contact_id)
    if not c:
        raise HTTPException(404, "Contatto non trovato")
    return c


@app.get("/api/tags")
def api_tags(user: AuthUser = Depends(require_user)):
    return db.list_tags()


@app.post("/api/contacts", status_code=201)
def api_create(data: ContactIn, user: AuthUser = Depends(require_admin)):
    cid = db.create(data.model_dump(), user.username)
    log.info("%s ha creato il contatto %s", user.username, cid)
    return db.get(cid)


@app.put("/api/contacts/{contact_id}")
def api_update(contact_id: int, data: ContactIn, user: AuthUser = Depends(require_admin)):
    if not db.update(contact_id, data.model_dump(), user.username):
        raise HTTPException(404, "Contatto non trovato")
    log.info("%s ha modificato il contatto %s", user.username, contact_id)
    return db.get(contact_id)


@app.delete("/api/contacts/{contact_id}", status_code=204)
def api_delete(contact_id: int, user: AuthUser = Depends(require_admin)):
    if not db.delete(contact_id):
        raise HTTPException(404, "Contatto non trovato")
    log.info("%s ha eliminato il contatto %s", user.username, contact_id)
    return Response(status_code=204)


@app.get("/api/export.csv")
def api_export(
    q: str = "", name: str = "", phone: str = "", email: str = "", notes: str = "",
    tag: list[str] = Query(default=[]), untagged: bool = False,
    user: AuthUser = Depends(require_user),
):
    items = db.search(q=q, name=name, phone=phone, email=email, notes=notes, tags=tag, untagged=untagged)
    buf = io.StringIO()
    buf.write("﻿")  # BOM per Excel
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Nome", "Telefoni", "Email", "Tag", "Note"])
    def cell(v: str) -> str:
        # Evita che Excel interpreti il contenuto come formula (CSV injection)
        return "'" + v if v[:1] in ("=", "@", "\t", "\r") or (v[:1] in "+-" and not re.match(r"[+-][\d\s]", v)) else v

    for c in items:
        w.writerow([cell(c["name"]), cell(", ".join(c["phones"])), cell(", ".join(c["emails"])),
                    cell(", ".join(c["tags"])), cell(c["notes"])])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="rubrica.csv"'})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")
