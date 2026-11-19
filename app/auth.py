"""Autenticazione: bind LDAP (Active Directory) e SSO Kerberos/SPNEGO (sessione Windows)."""
from __future__ import annotations

import base64
import logging
import os
import ssl
from dataclasses import asdict, dataclass

from ldap3 import BASE, NONE, SIMPLE, SUBTREE, Connection, Server, ServerPool, Tls, FIRST
from ldap3.core import exceptions as ldap_exc
from ldap3.utils.conv import escape_filter_chars

from .config import settings

log = logging.getLogger("rubrica.auth")

IN_CHAIN = "1.2.840.113556.1.4.1941"  # LDAP_MATCHING_RULE_IN_CHAIN: risolve i gruppi annidati


class AuthError(Exception):
    """Errore mostrabile all'utente."""


@dataclass
class AuthUser:
    username: str
    display_name: str
    is_admin: bool
    method: str  # "sso" | "ldap"

    def to_session(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- LDAP helpers

def _server_pool():
    tls = Tls(
        validate=ssl.CERT_REQUIRED if settings.ldap_tls_verify else ssl.CERT_NONE,
        ca_certs_file=settings.ldap_ca_file or None,
    )
    servers = []
    for url in [u.strip() for u in settings.ldap_server.split(",") if u.strip()]:
        servers.append(
            Server(url, use_ssl=url.lower().startswith("ldaps://"), tls=tls,
                   connect_timeout=settings.ldap_timeout, get_info=NONE)
        )
    if not servers:
        raise AuthError("Server LDAP non configurato (LDAP_DOMAIN / LDAP_SERVER).")
    return ServerPool(servers, FIRST, active=1, exhaust=True) if len(servers) > 1 else servers[0]


def _connect(user: str, password: str) -> Connection:
    if not password:
        # Un bind con password vuota in AD è un bind anonimo "riuscito": va sempre rifiutato.
        raise AuthError("Credenziali non valide.")
    try:
        conn = Connection(_server_pool(), user=user, password=password, authentication=SIMPLE,
                          receive_timeout=settings.ldap_timeout, raise_exceptions=False)
        if settings.ldap_start_tls:
            conn.open()
            conn.start_tls()
        if not conn.bind():
            result = conn.result or {}
            desc = result.get("description", "")
            msg = result.get("message", "")
            log.info("Bind LDAP fallito per %s: %s %s", user, desc, msg)
            if desc == "strongerAuthRequired":
                raise AuthError("Il controller di dominio richiede LDAP firmato: usare ldaps:// o LDAP_START_TLS=true.")
            if desc == "invalidCredentials":
                raise AuthError("Nome utente o password errati.")
            raise AuthError(f"Accesso LDAP rifiutato ({desc or 'errore'}).")
        return conn
    except ldap_exc.LDAPException as e:
        log.warning("Errore di connessione LDAP: %s", e)
        raise AuthError("Impossibile contattare il server LDAP.") from e


def _service_connection() -> Connection:
    if not settings.lookup_available:
        raise AuthError("Account di servizio LDAP non configurato.")
    return _connect(settings.ldap_bind_user, settings.ldap_bind_password)


def _find_user(conn: Connection, sam: str, upn: str | None = None):
    sam_e = escape_filter_chars(sam)
    alt = f"(userPrincipalName={escape_filter_chars(upn)})" if upn else ""
    flt = f"(&(objectCategory=person)(objectClass=user)(|(sAMAccountName={sam_e}){alt}))"
    conn.search(settings.ldap_base_dn, flt, SUBTREE,
                attributes=["sAMAccountName", "displayName", "distinguishedName", "userAccountControl"])
    if not conn.entries:
        raise AuthError("Utente non trovato nella directory.")
    e = conn.entries[0]
    attrs = e.entry_attributes_as_dict

    def first(name: str):
        v = attrs.get(name) or []
        return v[0] if v else None

    if int(first("userAccountControl") or 0) & 0x2:
        raise AuthError("Account disabilitato.")
    sam_value = str(first("sAMAccountName") or sam)
    return {"dn": e.entry_dn, "sam": sam_value, "display": str(first("displayName") or sam_value)}


def _group_dn(conn: Connection, group: str) -> str | None:
    if group.upper().startswith("CN="):
        return group
    if group.upper().startswith("S-1-"):
        flt = f"(&(objectClass=group)(objectSid={escape_filter_chars(group)}))"
    else:
        g = escape_filter_chars(group)
        flt = f"(&(objectClass=group)(|(sAMAccountName={g})(cn={g})))"
    conn.search(settings.ldap_base_dn, flt, SUBTREE, attributes=["distinguishedName"])
    if not conn.entries:
        log.warning("Gruppo '%s' non trovato in %s", group, settings.ldap_base_dn)
        return None
    return conn.entries[0].entry_dn


def _is_member(conn: Connection, user_dn: str, group: str) -> bool:
    gdn = _group_dn(conn, group)
    if not gdn:
        return False
    conn.search(user_dn, f"(memberOf:{IN_CHAIN}:={escape_filter_chars(gdn)})", BASE, attributes=["cn"])
    return len(conn.entries) > 0


def _authorize(conn: Connection, user: dict, method: str) -> AuthUser:
    if settings.read_group and not _is_member(conn, user["dn"], settings.read_group):
        raise AuthError("Non sei autorizzato ad accedere alla rubrica.")
    is_admin = bool(settings.admin_group) and _is_member(conn, user["dn"], settings.admin_group)
    return AuthUser(username=user["sam"], display_name=user["display"], is_admin=is_admin, method=method)


def _split_login(raw: str) -> tuple[str, str]:
    """Ritorna (sAMAccountName, nome per il bind) da 'utente', 'DOMINIO\\utente' o 'utente@dominio'."""
    raw = raw.strip()
    if "\\" in raw:
        raw = raw.split("\\", 1)[1]
    if "@" in raw:
        return raw.split("@", 1)[0], raw
    return raw, f"{raw}@{settings.ldap_domain}"


# ---------------------------------------------------------------- API pubblica

def ldap_login(username: str, password: str) -> AuthUser:
    if not username.strip():
        raise AuthError("Inserisci il nome utente.")
    sam, bind_name = _split_login(username)
    conn = _connect(bind_name, password)
    try:
        user = _find_user(conn, sam, bind_name if "@" in username else None)
        return _authorize(conn, user, "ldap")
    finally:
        conn.unbind()


def _prepare_kerberos_env() -> None:
    conf = settings.data_dir / "krb5.conf"
    realm = settings.krb5_realm
    conf.write_text(
        "[libdefaults]\n"
        f"  default_realm = {realm}\n"
        "  dns_lookup_kdc = true\n"
        "  dns_lookup_realm = false\n"
        "  rdns = false\n"
        "  ignore_acceptor_hostname = true\n"
    )
    os.environ.setdefault("KRB5_CONFIG", str(conf))
    os.environ["KRB5_KTNAME"] = f"FILE:{settings.krb5_keytab}"


if settings.sso_available:
    try:
        _prepare_kerberos_env()
    except OSError as e:  # pragma: no cover
        log.warning("Impossibile preparare krb5.conf: %s", e)


class NotKerberos(AuthError):
    """Il browser ha inviato un token NTLM invece di Kerberos."""


def kerberos_principal(auth_header: str) -> str:
    """Valida l'header 'Negotiate <token>' e ritorna il principal (utente@REALM)."""
    import gssapi  # import ritardato: la libreria serve solo se l'SSO è attivo

    token_b64 = auth_header.split(" ", 1)[1].strip() if " " in auth_header else ""
    try:
        token = base64.b64decode(token_b64)
    except ValueError as e:
        raise AuthError("Token SSO non valido.") from e
    if token.startswith(b"NTLMSSP"):
        raise NotKerberos("Il browser ha usato NTLM: SSO Kerberos non disponibile per questo indirizzo.")
    try:
        ctx = gssapi.SecurityContext(usage="accept")
        ctx.step(token)
        if not ctx.complete:
            raise AuthError("Negoziazione Kerberos incompleta.")
        return str(ctx.initiator_name)
    except gssapi.exceptions.GSSError as e:
        log.warning("Errore Kerberos: %s", e)
        raise AuthError("Autenticazione Windows non riuscita.") from e


def sso_login(auth_header: str) -> AuthUser:
    principal = kerberos_principal(auth_header)
    name, _, realm = principal.partition("@")
    if realm and settings.krb5_realm and realm.upper() != settings.krb5_realm.upper():
        raise AuthError("Dominio dell'utente non autorizzato.")
    if not settings.lookup_available:
        # Senza account di servizio non si possono leggere i gruppi: sola lettura.
        if settings.read_group:
            raise AuthError("SSO non disponibile: account di servizio LDAP non configurato.")
        return AuthUser(username=name, display_name=name, is_admin=False, method="sso")
    conn = _service_connection()
    try:
        user = _find_user(conn, name)
        return _authorize(conn, user, "sso")
    finally:
        conn.unbind()
