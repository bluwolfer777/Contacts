# ---- build: compila le dipendenze native (gssapi) ----
FROM python:3.12-slim AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libkrb5-dev krb5-config \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ---- runtime ----
FROM python:3.12-slim
LABEL org.opencontainers.image.title="Rubrica" \
      org.opencontainers.image.description="Rubrica aziendale condivisa con autenticazione Active Directory (LDAP + SSO Kerberos)" \
      org.opencontainers.image.source="https://github.com/bluwolfer777/Contacts" \
      org.opencontainers.image.licenses="GPL-3.0"
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgssapi-krb5-2 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --home /app rubrica \
 && mkdir -p /data /etc/krb5 && chown rubrica /data
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
WORKDIR /app
COPY app ./app
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8080
USER rubrica
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
