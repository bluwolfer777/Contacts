"""Configurazione letta esclusivamente da variabili d'ambiente (parametri del container)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "si", "sì")


def _str(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v.strip() if v is not None and v.strip() != "" else default


def _base_dn_from_domain(domain: str) -> str:
    return ",".join(f"DC={p}" for p in domain.split(".") if p)


@dataclass
class Settings:
    app_title: str = field(default_factory=lambda: _str("APP_TITLE", "Rubrica"))
    data_dir: Path = field(default_factory=lambda: Path(_str("DATA_DIR", "/data")))

    # --- LDAP / Active Directory ---
    ldap_domain: str = field(default_factory=lambda: _str("LDAP_DOMAIN").lower())
    ldap_server: str = field(default_factory=lambda: _str("LDAP_SERVER"))
    ldap_base_dn: str = field(default_factory=lambda: _str("LDAP_BASE_DN"))
    ldap_bind_user: str = field(default_factory=lambda: _str("LDAP_BIND_USER"))
    ldap_bind_password: str = field(default_factory=lambda: os.environ.get("LDAP_BIND_PASSWORD", ""))
    ldap_start_tls: bool = field(default_factory=lambda: _bool("LDAP_START_TLS", False))
    ldap_tls_verify: bool = field(default_factory=lambda: _bool("LDAP_TLS_VERIFY", False))
    ldap_ca_file: str = field(default_factory=lambda: _str("LDAP_CA_FILE"))
    ldap_timeout: int = field(default_factory=lambda: int(_str("LDAP_TIMEOUT", "5")))

    # --- Autorizzazione ---
    # Gruppo i cui membri (anche annidati) possono modificare la rubrica.
    # Accetta: nome (sAMAccountName/CN), DN completo (CN=...,DC=...) oppure SID (S-1-...).
    admin_group: str = field(default_factory=lambda: _str("ADMIN_GROUP", "Administrators"))
    # Gruppo opzionale richiesto per la sola lettura. Vuoto = tutti gli utenti autenticati.
    read_group: str = field(default_factory=lambda: _str("READ_GROUP"))

    # --- SSO Kerberos (sessione Windows) ---
    sso_enabled: bool = field(default_factory=lambda: _bool("SSO_ENABLED", True))
    krb5_keytab: str = field(default_factory=lambda: _str("KRB5_KEYTAB", "/etc/krb5/krb5.keytab"))
    krb5_realm: str = field(default_factory=lambda: _str("KRB5_REALM"))

    # --- Sessione web ---
    secret_key: str = field(default_factory=lambda: _str("SECRET_KEY"))
    session_hours: int = field(default_factory=lambda: int(_str("SESSION_HOURS", "10")))

    def __post_init__(self) -> None:
        if not self.ldap_server and self.ldap_domain:
            self.ldap_server = f"ldap://{self.ldap_domain}"
        if not self.ldap_base_dn and self.ldap_domain:
            self.ldap_base_dn = _base_dn_from_domain(self.ldap_domain)
        if not self.krb5_realm and self.ldap_domain:
            self.krb5_realm = self.ldap_domain.upper()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.secret_key:
            # Genera e persiste una chiave nel volume dati, così le sessioni sopravvivono ai riavvii.
            key_file = self.data_dir / ".secret_key"
            if key_file.exists():
                self.secret_key = key_file.read_text().strip()
            else:
                self.secret_key = secrets.token_urlsafe(48)
                key_file.write_text(self.secret_key)
                try:
                    key_file.chmod(0o600)
                except OSError:
                    pass

    @property
    def sso_available(self) -> bool:
        return self.sso_enabled and Path(self.krb5_keytab).is_file()

    @property
    def lookup_available(self) -> bool:
        """True se è configurato un account di servizio per interrogare AD senza la password dell'utente."""
        return bool(self.ldap_bind_user and self.ldap_bind_password)


settings = Settings()
