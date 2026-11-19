# Rubrica

Rubrica aziendale web, condivisa da tutti gli utenti, con autenticazione **Active Directory**.

- **Lettura** per tutti gli utenti di dominio autenticati (opzionalmente solo per un gruppo).
- **Modifica** solo per i membri di un gruppo AD configurabile (default `Administrators`, gruppi annidati inclusi).
- **SSO**: chi è già loggato su un PC Windows del dominio entra in automatico (Kerberos/SPNEGO); altrimenti login con utente e password di dominio (LDAP).
- Contatti con **nome, più numeri di telefono, più email, note e più tag** (tutti opzionali tranne il nome).
- **Ricerca** libera su tutti i campi (senza distinzione di maiuscole e accenti; i numeri si trovano anche senza spazi) e **filtri** per nome, telefono, email, note, tag (più tag = AND) e "senza tag".
- Esportazione CSV dei risultati filtrati.
- Un solo container, dati in SQLite su volume Docker, servito in HTTP.

Tutta la configurazione specifica dell'azienda (dominio, gruppi, account di servizio) si passa come **variabili d'ambiente del container**: nel repository non c'è alcun dato aziendale.

---

## Installazione rapida

Requisiti: un host con Docker e Docker Compose v2 che raggiunga i domain controller (porta 389, o 636 per LDAPS) e ne risolva il dominio via DNS.

```bash
mkdir rubrica && cd rubrica
curl -O https://raw.githubusercontent.com/bluwolfer777/Contacts/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/bluwolfer777/Contacts/main/.env.example
mkdir krb5            # qui andrà il keytab se si usa l'SSO
nano .env             # imposta almeno LDAP_DOMAIN
docker compose up -d
```

Apri `http://<host>:8080`. Senza keytab l'SSO è disattivato in automatico e si accede con utente e password.

Per compilare l'immagine invece di scaricarla da GHCR: clona il repository, decommenta `build: .` in `docker-compose.yml` e lancia `docker compose up -d --build`.

### Aggiornamento

```bash
docker compose pull && docker compose up -d
```

### Backup

I dati sono nel volume `rubrica-data` (file `contacts.db`):

```bash
docker compose exec rubrica python -c "import sqlite3;s=sqlite3.connect('/data/contacts.db');d=sqlite3.connect('/data/backup.db');s.backup(d)"
docker compose cp rubrica:/data/backup.db ./backup-$(date +%F).db
```

---

## Parametri

| Variabile | Default | Descrizione |
|---|---|---|
| `HTTP_PORT` | `8080` | Porta pubblicata sull'host (solo nel compose). |
| `APP_TITLE` | `Rubrica` | Titolo mostrato nell'interfaccia. |
| `LDAP_DOMAIN` | — **obbligatorio** | Dominio AD DNS (es. `example.local`). Gli utenti accedono come `utente`, `DOMINIO\utente` o `utente@dominio`. |
| `LDAP_SERVER` | `ldap://<LDAP_DOMAIN>` | Uno o più URL separati da virgola (`ldap://dc1.example.local,ldap://dc2.example.local`). Usa `ldaps://` per LDAP su TLS. |
| `LDAP_BASE_DN` | ricavato dal dominio | Base di ricerca di utenti e gruppi (es. `DC=example,DC=local`). |
| `LDAP_START_TLS` | `false` | Usa StartTLS sulla porta 389. |
| `LDAP_TLS_VERIFY` | `false` | Verifica il certificato del DC (con `LDAP_CA_FILE` per una CA interna montata nel container). |
| `LDAP_BIND_USER` / `LDAP_BIND_PASSWORD` | — | Account di servizio in sola lettura. **Necessario per l'SSO** (serve a leggere i gruppi degli utenti entrati con Windows). |
| `ADMIN_GROUP` | `Administrators` | Gruppo che può modificare: nome (`sAMAccountName`/CN), DN completo o SID. Le appartenenze annidate valgono. |
| `READ_GROUP` | vuoto | Se impostato, solo i membri di questo gruppo possono consultare la rubrica. |
| `SSO_ENABLED` | `true` | Attiva l'SSO se è presente il keytab. |
| `KRB5_KEYTAB` | `/etc/krb5/krb5.keytab` | Percorso del keytab nel container. |
| `KRB5_REALM` | `LDAP_DOMAIN` in maiuscolo | Realm Kerberos accettato. |
| `SECRET_KEY` | generata | Chiave che firma i cookie di sessione; se vuota viene generata e salvata nel volume. |
| `SESSION_HOURS` | `10` | Durata della sessione. |

> **Gruppo `Administrators`**: è il gruppo builtin del dominio (`CN=Builtin`). Su un AD installato in un'altra lingua il nome può cambiare: in quel caso usa il suo SID fisso `S-1-5-32-544`. Di norma conviene creare un gruppo dedicato (es. `Rubrica-Editor`).

> **Firma LDAP**: se i DC richiedono la firma LDAP ("LDAP server signing requirements") il bind semplice su 389 viene rifiutato. In quel caso usa `LDAP_SERVER=ldaps://...` oppure `LDAP_START_TLS=true`.

---

## SSO con la sessione di Windows (Kerberos)

Funziona in HTTP; Kerberos non richiede HTTPS. Servono un account di servizio, un nome DNS e un keytab.

1. **Nome DNS**: crea un record **A** (non CNAME) per il server, ad es. `rubrica.example.local`. Gli utenti devono usare questo nome: con l'indirizzo IP il browser non usa Kerberos.

2. **Account di servizio**: crea in AD un utente, ad es. `svc-rubrica`, con password che non scade. Nelle proprietà dell'account, scheda *Account*, spunta *This account supports Kerberos AES 256 bit encryption*.

3. **SPN e keytab**: da un prompt amministrativo su un PC del dominio:

   ```bat
   ktpass -princ HTTP/rubrica.example.local@EXAMPLE.LOCAL -mapuser EXAMPLE\svc-rubrica ^
          -pass * -crypto AES256-SHA1 -ptype KRB5_NT_PRINCIPAL -out krb5.keytab
   ```

   `ktpass` registra l'SPN e **reimposta la password** dell'account con quella inserita: usa la stessa in `LDAP_BIND_PASSWORD`. Se la password cambia, rigenera il keytab. Verifica con `setspn -L svc-rubrica`.

4. **Copia il keytab** sull'host Docker in `./krb5/krb5.keytab` e rendilo leggibile all'utente del container (uid 10001):

   ```bash
   sudo chown 10001 krb5/krb5.keytab && sudo chmod 400 krb5/krb5.keytab
   ```

5. **`.env`**: imposta `LDAP_BIND_USER=svc-rubrica@example.local` e `LDAP_BIND_PASSWORD=...`, poi `docker compose up -d`.

6. **Browser**: il sito deve essere considerato Intranet.
   - **Edge / Chrome / IE (Windows)**: aggiungi `http://rubrica.example.local` all'area *Intranet locale* (GPO: *Site to Zone Assignment List*, valore `1`), oppure imposta la policy `AuthServerAllowlist` = `rubrica.example.local`.
   - **Firefox**: policy `Authentication` → `SPNEGO` = `rubrica.example.local` (oppure `network.negotiate-auth.trusted-uris` in `about:config`).

Come funziona: alla prima visita il server risponde `401 Negotiate`. Se il browser invia un ticket Kerberos l'utente entra subito; se non può (PC fuori dominio, sito non in Intranet, accesso via IP) viene mostrato il form di accesso. **Cambia utente** esce e porta al form, per esempio per entrare con un account amministrativo; da lì *Usa la sessione di Windows* riattiva l'SSO.

---

## Sicurezza

- Il servizio è in **HTTP**: con il login tramite form la password passa in chiaro sulla rete interna. Con l'SSO Kerberos la password non viene mai trasmessa. Se serve HTTPS si può mettere il container dietro un reverse proxy (Traefik, nginx, IIS ARR).
- Cookie di sessione `HttpOnly`, `SameSite=Lax`, firmati; le API di modifica richiedono un header anti-CSRF.
- I permessi di modifica vengono letti da AD a ogni accesso.
- Il container gira come utente non privilegiato.

## Sviluppo

```bash
pip install -r requirements.txt pytest httpx
DATA_DIR=./data SSO_ENABLED=false LDAP_DOMAIN=example.local uvicorn app.main:app --reload --port 8080
DATA_DIR=$(mktemp -d) SSO_ENABLED=false python -m pytest tests
```

Struttura:

```
app/config.py   parametri da variabili d'ambiente
app/auth.py     login LDAP, SSO Kerberos, controllo gruppi AD
app/db.py       SQLite, ricerca e filtri
app/main.py     FastAPI: pagine, API REST, CSV
app/static/     interfaccia (HTML/CSS/JS, nessuna dipendenza esterna)
```

API (stessa sessione del browser): `GET /api/contacts?q=&name=&phone=&email=&notes=&tag=&untagged=`, `GET /api/tags`, `POST/PUT/DELETE /api/contacts[/{id}]` (gruppo admin, header `X-Requested-With: rubrica`), `GET /api/export.csv`.

Ogni push su `main` compila e pubblica l'immagine su `ghcr.io/bluwolfer777/contacts` tramite GitHub Actions.

## Licenza

GPL-3.0, vedi [LICENSE](LICENSE).
