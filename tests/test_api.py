"""Test end-to-end delle API con autenticazione simulata (nessun AD necessario).

Esecuzione:  DATA_DIR=$(mktemp -d) SSO_ENABLED=false python -m pytest tests
"""
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("SSO_ENABLED", "false")
os.environ.setdefault("LDAP_DOMAIN", "example.local")

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.auth import AuthError, AuthUser  # noqa: E402

H = {"X-Requested-With": "rubrica"}


def fake_login(username, password):
    if password != "ok":
        raise AuthError("Nome utente o password errati.")
    return AuthUser(username=username, display_name=username.title(), is_admin=username == "admin", method="ldap")


main.ldap_login = fake_login


def client(user=None):
    c = TestClient(main.app)
    if user:
        r = c.post("/login", data={"username": user, "password": "ok"}, follow_redirects=False)
        assert r.status_code == 303
    return c


def test_requires_auth():
    c = client()
    assert c.get("/api/contacts").status_code == 401
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.post("/login", data={"username": "x", "password": "bad"}).status_code == 401


def test_reader_cannot_write():
    c = client("mario")
    assert c.get("/api/me").json()["is_admin"] is False
    assert c.post("/api/contacts", json={"name": "X"}, headers=H).status_code == 403


def test_csrf_header_required():
    c = client("admin")
    assert c.post("/api/contacts", json={"name": "X"}).status_code == 403


def test_crud_and_search():
    a = client("admin")
    r = a.post("/api/contacts", headers=H, json={
        "name": "Élodie Rossi", "phones": ["+41 79 123 45 67", "091 555 00 11"],
        "emails": ["elodie@example.com", "e.rossi@example.org"], "tags": ["Fornitori", "IT"],
        "notes": "Referente server"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    a.post("/api/contacts", headers=H, json={"name": "Senza Niente"})
    a.post("/api/contacts", headers=H, json={"name": "Luca Bianchi", "tags": ["it", "Clienti"]})

    reader = client("mario")
    s = lambda **p: [c["name"] for c in reader.get("/api/contacts", params=p).json()["items"]]
    assert s(q="elodie") == ["Élodie Rossi"]               # accenti ignorati
    assert s(q="791234567") == ["Élodie Rossi"]            # telefono senza spazi
    assert s(phone="555 0011") == ["Élodie Rossi"]
    assert s(email="example.org") == ["Élodie Rossi"]
    assert s(notes="server") == ["Élodie Rossi"]
    assert s(tag="IT") == ["Élodie Rossi", "Luca Bianchi"]  # tag case-insensitive
    assert s(tag=["it", "clienti"]) == ["Luca Bianchi"]     # più tag = AND
    assert s(untagged="true") == ["Senza Niente"]
    assert s(q="rossi fornitori") == ["Élodie Rossi"]
    tags = {t["name"]: t["count"] for t in reader.get("/api/tags").json()}
    assert tags == {"Clienti": 1, "Fornitori": 1, "IT": 2}

    r = a.put(f"/api/contacts/{cid}", headers=H, json={"name": "Elodie Rossi", "phones": [], "emails": [], "tags": []})
    assert r.status_code == 200 and r.json()["phones"] == [] and r.json()["updated_by"] == "admin"
    assert "Fornitori" not in {t["name"] for t in reader.get("/api/tags").json()}

    assert a.post("/api/contacts", headers=H, json={"name": "X", "emails": ["no"]}).status_code == 422
    assert a.post("/api/contacts", headers=H, json={"name": "  "}).status_code == 422

    csv = reader.get("/api/export.csv").text
    assert "Luca Bianchi" in csv

    assert a.delete(f"/api/contacts/{cid}", headers=H).status_code == 204
    assert a.get(f"/api/contacts/{cid}").status_code == 404
