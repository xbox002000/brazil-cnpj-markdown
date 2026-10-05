"""CNPJ JSON providers → one normalized, contact-free company record.

Providers (official/public JSON over HTTPS only, no HTML scraping):
- brasilapi : https://brasilapi.com.br/api/cnpj/v1/{cnpj}          (keyless)
- cnpjws    : https://publica.cnpj.ws/cnpj/{cnpj}                  (keyless, ~3 req/min)
              https://comercial.cnpj.ws/cnpj/{cnpj}  + x_api_token   (buyer key)
- receitaws : https://receitaws.com.br/v1/cnpj/{cnpj}               (keyless ~3 req/min, or Bearer buyer key)

Email / telephone / fax / DDD fields are dropped from every payload BEFORE normalization and the
normalized record is whitelist-built, then scrubbed again for email-looking strings.
"""
from __future__ import annotations

import asyncio
import os
import random
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from cnpj import format_cnpj

UA = "Mozilla/5.0 (compatible; BrazilCnpjMarkdown/0.1; +https://apify.com)"

BASES = {
    "brasilapi": "https://brasilapi.com.br",
    "cnpjws": "https://publica.cnpj.ws",
    "cnpjws_commercial": "https://comercial.cnpj.ws",
    "receitaws": "https://receitaws.com.br",
}
_test_base = os.getenv("CNPJ_TEST_BASE", "")
if _test_base.startswith(("http://127.0.0.1", "http://localhost")):
    BASES = {k: f"{_test_base.rstrip('/')}/{k}" for k in BASES}

SOURCE_LABELS = {
    "brasilapi": "BrasilAPI",
    "cnpjws": "CNPJ.ws",
    "receitaws": "ReceitaWS",
}

KEY_HINT = (
    "Keyless public endpoints are rate-limited (CNPJ.ws / ReceitaWS ≈ 3 requests/min). For production "
    "volume pass your own ReceitaWS token (provider=receitaws) or CNPJ.ws commercial token "
    "(provider=cnpjws) in apiKey. Failed rows are not charged."
)

# --- contact stripping --------------------------------------------------------------------------
_CONTACT_KEY_RE = re.compile(r"(e_?mail|telefone|phone|fax|ddd|celular|mobile|whats|contato|contact)", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def strip_contacts(obj: Any) -> Any:
    """Recursively drop any key that looks like email / telephone / fax / DDD / contact."""
    if isinstance(obj, dict):
        return {k: strip_contacts(v) for k, v in obj.items() if not _CONTACT_KEY_RE.search(str(k))}
    if isinstance(obj, list):
        return [strip_contacts(v) for v in obj]
    return obj


def scrub_values(obj: Any) -> Any:
    """Final guard: blank any string value containing an email address."""
    if isinstance(obj, dict):
        return {k: scrub_values(v) for k, v in strip_contacts(obj).items()}
    if isinstance(obj, list):
        return [scrub_values(v) for v in obj]
    if isinstance(obj, str) and _EMAIL_RE.search(obj):
        return _EMAIL_RE.sub("[redacted]", obj)
    return obj


def redact(text: str, secret: str | None) -> str:
    if not text:
        return text
    if secret:
        text = text.replace(secret, "***")
    return re.sub(r"(?i)(bearer\s+|x_api_token[=:]\s*|token=)[^\s&\"']+", r"\1***", text)


# --- small normalizers --------------------------------------------------------------------------
def _s(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _date(v: Any) -> str | None:
    s = _s(v)
    if not s:
        return None
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else s


def _float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".")) if not isinstance(v, (int, float)) else float(v)
    except ValueError:
        return None


def cnae_code(v: Any) -> str | None:
    d = re.sub(r"\D", "", str(v or ""))
    if not d or set(d) == {"0"}:
        return None
    return d.zfill(7)[-7:]


def cnae_fmt(code: str | None) -> str | None:
    return f"{code[:4]}-{code[4]}/{code[5:]}" if code and len(code) == 7 else code


def _cnae(code: Any, desc: Any) -> dict | None:
    c = cnae_code(code)
    if not c and not _s(desc):
        return None
    return {"code": c, "codeFormatted": cnae_fmt(c), "description": _s(desc)}


def _natureza(code: Any, desc: Any) -> dict | None:
    d = re.sub(r"\D", "", str(code or ""))
    if not d and not _s(desc):
        return None
    fmt = f"{d[:3]}-{d[3:]}" if len(d) == 4 else (d or None)
    return {"code": fmt, "description": _s(desc)}


def _cep(v: Any) -> str | None:
    d = re.sub(r"\D", "", str(v or ""))
    return d.zfill(8) if d else None


def _yes(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    s = _s(v)
    if s is None:
        return None
    return s.lower() in ("sim", "s", "true", "yes", "1")


def address_line(a: dict | None) -> str | None:
    if not a:
        return None
    street = " ".join(x for x in (a.get("tipoLogradouro"), a.get("logradouro")) if x)
    if street and a.get("tipoLogradouro") and a.get("logradouro", "").upper().startswith(a["tipoLogradouro"].upper()):
        street = a["logradouro"]
    first = ", ".join(x for x in (street, a.get("numero"), a.get("complemento")) if x)
    city = "/".join(x for x in (a.get("municipio"), a.get("uf")) if x)
    cep = a.get("cep")
    parts = [first, a.get("bairro"), city, f"CEP {cep[:5]}-{cep[5:]}" if cep and len(cep) == 8 else None]
    if a.get("pais") and str(a["pais"]).upper() not in ("BRASIL", "BRAZIL"):
        parts.append(a["pais"])
    s = " — ".join(x for x in parts if x)
    return s or None


def _base_record(cnpj: str) -> dict[str, Any]:
    return {"cnpj": cnpj, "cnpjFormatted": format_cnpj(cnpj), "cnpjRoot": cnpj[:8]}


def normalize_brasilapi(d: dict, cnpj: str, include_qsa: bool) -> dict[str, Any]:
    d = strip_contacts(d)
    addr = {
        "tipoLogradouro": _s(d.get("descricao_tipo_de_logradouro")),
        "logradouro": _s(d.get("logradouro")),
        "numero": _s(d.get("numero")),
        "complemento": _s(d.get("complemento")),
        "bairro": _s(d.get("bairro")),
        "municipio": _s(d.get("municipio")),
        "uf": _s(d.get("uf")),
        "cep": _cep(d.get("cep")),
        "codigoMunicipioIbge": _s(d.get("codigo_municipio_ibge")),
        "pais": _s(d.get("pais")) or _s(d.get("nome_cidade_no_exterior")),
    }
    qsa = [
        {"name": _s(q.get("nome_socio")), "role": _s(q.get("qualificacao_socio")),
         "since": _date(q.get("data_entrada_sociedade"))}
        for q in (d.get("qsa") or []) if isinstance(q, dict)
    ]
    sec = [_cnae(c.get("codigo"), c.get("descricao")) for c in (d.get("cnaes_secundarios") or []) if isinstance(c, dict)]
    rec = _base_record(_s(d.get("cnpj")) or cnpj)
    rec.update({
        "razaoSocial": _s(d.get("razao_social")),
        "nomeFantasia": _s(d.get("nome_fantasia")),
        "matrizFilial": _s(d.get("descricao_identificador_matriz_filial")),
        "situacao": (_s(d.get("descricao_situacao_cadastral")) or "").upper() or None,
        "situacaoDate": _date(d.get("data_situacao_cadastral")),
        "situacaoMotivo": _s(d.get("descricao_motivo_situacao_cadastral")),
        "situacaoEspecial": _s(d.get("situacao_especial")),
        "situacaoEspecialDate": _date(d.get("data_situacao_especial")),
        "dataInicioAtividade": _date(d.get("data_inicio_atividade")),
        "naturezaJuridica": _natureza(d.get("codigo_natureza_juridica"), d.get("natureza_juridica")),
        "porte": _s(d.get("porte")),
        "capitalSocial": _float(d.get("capital_social")),
        "enteFederativo": _s(d.get("ente_federativo_responsavel")),
        "cnaePrincipal": _cnae(d.get("cnae_fiscal"), d.get("cnae_fiscal_descricao")),
        "cnaesSecundarios": [c for c in sec if c],
        "address": {k: v for k, v in addr.items() if v} or None,
        "simples": {"optante": _yes(d.get("opcao_pelo_simples")), "dataOpcao": _date(d.get("data_opcao_pelo_simples")),
                    "dataExclusao": _date(d.get("data_exclusao_do_simples"))},
        "mei": {"optante": _yes(d.get("opcao_pelo_mei")), "dataOpcao": _date(d.get("data_opcao_pelo_mei")),
                "dataExclusao": _date(d.get("data_exclusao_do_mei"))},
        "qsa": qsa if include_qsa else None,
        "sourceUpdatedAt": None,
    })
    return rec


def normalize_cnpjws(d: dict, cnpj: str, include_qsa: bool) -> dict[str, Any]:
    d = strip_contacts(d)
    e = d.get("estabelecimento") or {}
    nat = d.get("natureza_juridica") or {}
    porte = d.get("porte") or {}
    simples = d.get("simples") or {}
    motivo = e.get("motivo_situacao_cadastral")
    addr = {
        "tipoLogradouro": _s(e.get("tipo_logradouro")),
        "logradouro": _s(e.get("logradouro")),
        "numero": _s(e.get("numero")),
        "complemento": _s(e.get("complemento")),
        "bairro": _s(e.get("bairro")),
        "municipio": _s((e.get("cidade") or {}).get("nome")),
        "uf": _s((e.get("estado") or {}).get("sigla")),
        "cep": _cep(e.get("cep")),
        "codigoMunicipioIbge": _s((e.get("cidade") or {}).get("ibge_id")),
        "pais": _s((e.get("pais") or {}).get("nome")) or _s(e.get("nome_cidade_exterior")),
    }
    qsa = [
        {"name": _s(q.get("nome")), "role": _s((q.get("qualificacao_socio") or {}).get("descricao")),
         "since": _date(q.get("data_entrada"))}
        for q in (d.get("socios") or []) if isinstance(q, dict)
    ]
    ap = e.get("atividade_principal") or {}
    sec = [_cnae(c.get("id"), c.get("descricao")) for c in (e.get("atividades_secundarias") or []) if isinstance(c, dict)]
    rec = _base_record(_s(e.get("cnpj")) or cnpj)
    rec.update({
        "razaoSocial": _s(d.get("razao_social")),
        "nomeFantasia": _s(e.get("nome_fantasia")),
        "matrizFilial": (_s(e.get("tipo")) or "").upper() or None,
        "situacao": (_s(e.get("situacao_cadastral")) or "").upper() or None,
        "situacaoDate": _date(e.get("data_situacao_cadastral")),
        "situacaoMotivo": _s(motivo.get("descricao")) if isinstance(motivo, dict) else _s(motivo),
        "situacaoEspecial": _s(e.get("situacao_especial")),
        "situacaoEspecialDate": _date(e.get("data_situacao_especial")),
        "dataInicioAtividade": _date(e.get("data_inicio_atividade")),
        "naturezaJuridica": _natureza(nat.get("id"), nat.get("descricao")),
        "porte": (_s(porte.get("descricao")) or "").upper() or None,
        "capitalSocial": _float(d.get("capital_social")),
        "enteFederativo": _s(d.get("responsavel_federativo")),
        "cnaePrincipal": _cnae(ap.get("id"), ap.get("descricao")),
        "cnaesSecundarios": [c for c in sec if c],
        "address": {k: v for k, v in addr.items() if v} or None,
        "simples": {"optante": _yes(simples.get("simples")), "dataOpcao": _date(simples.get("data_opcao_simples")),
                    "dataExclusao": _date(simples.get("data_exclusao_simples"))},
        "mei": {"optante": _yes(simples.get("mei")), "dataOpcao": _date(simples.get("data_opcao_mei")),
                "dataExclusao": _date(simples.get("data_exclusao_mei"))},
        "qsa": qsa if include_qsa else None,
        "sourceUpdatedAt": _s(e.get("atualizado_em")) or _s(d.get("atualizado_em")),
    })
    return rec


def normalize_receitaws(d: dict, cnpj: str, include_qsa: bool) -> dict[str, Any]:
    d = strip_contacts(d)
    nat_raw = _s(d.get("natureza_juridica")) or ""
    m = re.match(r"^([\d-]+)\s*-\s*(.+)$", nat_raw)
    nat = _natureza(m.group(1), m.group(2)) if m else _natureza(None, nat_raw)
    addr = {
        "logradouro": _s(d.get("logradouro")),
        "numero": _s(d.get("numero")),
        "complemento": _s(d.get("complemento")),
        "bairro": _s(d.get("bairro")),
        "municipio": _s(d.get("municipio")),
        "uf": _s(d.get("uf")),
        "cep": _cep(d.get("cep")),
    }
    qsa = []
    for q in d.get("qsa") or []:
        if not isinstance(q, dict):
            continue
        role = _s(q.get("qual")) or ""
        role = re.sub(r"^\d+-", "", role) or None
        qsa.append({"name": _s(q.get("nome")), "role": role, "since": None})
    aps = d.get("atividade_principal") or []
    ap = aps[0] if aps and isinstance(aps[0], dict) else {}
    sec = [_cnae(c.get("code"), c.get("text")) for c in (d.get("atividades_secundarias") or []) if isinstance(c, dict)]
    simples = d.get("simples") or {}
    simei = d.get("simei") or {}
    rec = _base_record(re.sub(r"\D", "", d.get("cnpj") or "") or cnpj)
    rec.update({
        "razaoSocial": _s(d.get("nome")),
        "nomeFantasia": _s(d.get("fantasia")),
        "matrizFilial": (_s(d.get("tipo")) or "").upper() or None,
        "situacao": (_s(d.get("situacao")) or "").upper() or None,
        "situacaoDate": _date(d.get("data_situacao")),
        "situacaoMotivo": _s(d.get("motivo_situacao")),
        "situacaoEspecial": _s(d.get("situacao_especial")),
        "situacaoEspecialDate": _date(d.get("data_situacao_especial")),
        "dataInicioAtividade": _date(d.get("abertura")),
        "naturezaJuridica": nat,
        "porte": _s(d.get("porte")),
        "capitalSocial": _float(d.get("capital_social")),
        "enteFederativo": _s(d.get("efr")),
        "cnaePrincipal": _cnae(ap.get("code"), ap.get("text")),
        "cnaesSecundarios": [c for c in sec if c and c.get("code")],
        "address": {k: v for k, v in addr.items() if v} or None,
        "simples": {"optante": _yes(simples.get("optante")), "dataOpcao": _date(simples.get("data_opcao")),
                    "dataExclusao": _date(simples.get("data_exclusao"))},
        "mei": {"optante": _yes(simei.get("optante")), "dataOpcao": _date(simei.get("data_opcao")),
                "dataExclusao": _date(simei.get("data_exclusao"))},
        "qsa": qsa if include_qsa else None,
        "sourceUpdatedAt": _s(d.get("ultima_atualizacao")),
    })
    return rec


NORMALIZERS = {"brasilapi": normalize_brasilapi, "cnpjws": normalize_cnpjws, "receitaws": normalize_receitaws}


def finalize(rec: dict[str, Any]) -> dict[str, Any]:
    if rec.get("address"):
        rec["addressLine"] = address_line(rec["address"])
    rec["cnaesSecundariosCount"] = len(rec.get("cnaesSecundarios") or [])
    rec["qsaCount"] = len(rec["qsa"]) if rec.get("qsa") is not None else None
    for k in ("simples", "mei"):
        if rec.get(k) and all(v is None for v in rec[k].values()):
            rec[k] = None
    return scrub_values(rec)


# --- HTTP ---------------------------------------------------------------------------------------
def request_spec(provider: str, cnpj: str, api_key: str | None) -> tuple[str, dict[str, str], bool]:
    """Return (url, extra_headers, keyed)."""
    if provider == "brasilapi":
        return f"{BASES['brasilapi']}/api/cnpj/v1/{cnpj}", {}, False
    if provider == "cnpjws":
        if api_key:
            return f"{BASES['cnpjws_commercial']}/cnpj/{cnpj}", {"x_api_token": api_key}, True
        return f"{BASES['cnpjws']}/cnpj/{cnpj}", {}, False
    if provider == "receitaws":
        if api_key:
            return f"{BASES['receitaws']}/v1/cnpj/{cnpj}", {"Authorization": f"Bearer {api_key}"}, True
        return f"{BASES['receitaws']}/v1/cnpj/{cnpj}", {}, False
    raise ValueError(f"unknown provider {provider}")


def public_url(provider: str, cnpj: str, keyed: bool) -> str:
    if provider == "brasilapi":
        return f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
    if provider == "cnpjws":
        return f"https://{'comercial' if keyed else 'publica'}.cnpj.ws/cnpj/{cnpj}"
    return f"https://receitaws.com.br/v1/cnpj/{cnpj}"


def classify(status: int, body: Any, text: str = "") -> str:
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status == 400:
        msg = (str(body.get("message") or "") if isinstance(body, dict) else text).lower()
        return "invalid_input" if "inv" in msg else "bad_request"
    if status >= 500:
        return "server_error"
    return f"http_{status}"


@dataclass
class Attempt:
    provider: str
    ok: bool
    record: dict | None = None
    error_class: str | None = None
    http_status: int | None = None
    message: str | None = None
    attempts: int = 0
    keyed: bool = False


async def fetch_provider(
    client: httpx.AsyncClient, provider: str, cnpj: str, *, api_key: str | None, include_qsa: bool,
    max_retries: int = 3, timeout: float = 30.0, backoff_base: float = 1.0, max_wait: float = 30.0,
) -> Attempt:
    url, extra, keyed = request_spec(provider, cnpj, api_key)
    headers = {"Accept": "application/json", "User-Agent": UA, **extra}
    last = Attempt(provider, False, keyed=keyed)
    for attempt in range(max_retries + 1):
        last.attempts = attempt + 1
        retry_after: float | None = None
        try:
            resp = await client.get(url, headers=headers, timeout=timeout)
            status = resp.status_code
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = None
            text = redact(resp.text[:300], api_key)
            if status == 200 and isinstance(body, dict):
                # ReceitaWS reports errors with HTTP 200 + {"status":"ERROR"}
                if str(body.get("status", "")).upper() == "ERROR":
                    msg = redact(str(body.get("message") or "error"), api_key)[:200]
                    low = msg.lower()
                    if "too many" in low or "limite" in low:
                        last.error_class, last.http_status, last.message = "rate_limited", 429, msg
                    else:
                        cls = "invalid_input" if "inválido" in low or "invalido" in low else "not_found"
                        return Attempt(provider, False, error_class=cls, http_status=200, message=msg,
                                       attempts=last.attempts, keyed=keyed)
                else:
                    rec = NORMALIZERS[provider](body, cnpj, include_qsa)
                    if not rec.get("razaoSocial"):
                        return Attempt(provider, False, error_class="bad_response", http_status=200,
                                       message="Provider returned JSON without razão social.",
                                       attempts=last.attempts, keyed=keyed)
                    rec["source"] = provider
                    rec["sourceLabel"] = SOURCE_LABELS[provider]
                    rec["sourceUrl"] = public_url(provider, cnpj, keyed)
                    return Attempt(provider, True, record=rec, http_status=200, attempts=last.attempts, keyed=keyed)
            elif status == 200:
                last.error_class, last.http_status, last.message = "bad_response", 200, "Non-JSON 200 response."
            else:
                cls = classify(status, body, text)
                msg = (body.get("message") or body.get("detalhes") or body.get("titulo")) if isinstance(body, dict) else None
                last.error_class, last.http_status = cls, status
                last.message = redact(str(msg or text or f"HTTP {status}"), api_key)[:200]
                if status in (400, 401, 403, 404) or (400 <= status < 500 and status != 429):
                    return Attempt(provider, False, error_class=cls, http_status=status, message=last.message,
                                   attempts=last.attempts, keyed=keyed)
                ra = resp.headers.get("retry-after")
                if ra:
                    try:
                        retry_after = float(ra)
                    except ValueError:
                        retry_after = None
        except httpx.TimeoutException:
            last.error_class, last.http_status, last.message = "timeout", None, f"Timed out after {timeout:.0f}s"
        except httpx.HTTPError as e:
            last.error_class, last.http_status = "network", None
            last.message = redact(f"{type(e).__name__}: {e}", api_key)[:200]
        if attempt < max_retries:
            wait = retry_after if retry_after is not None else backoff_base * (2 ** attempt) + random.uniform(0, 0.5)
            await asyncio.sleep(min(max(wait, 0.0), max_wait))
    return last


def provider_chain(provider: str, api_key: str | None, key_provider: str) -> list[tuple[str, str | None]]:
    """Ordered (provider, key) pairs to try."""
    if provider != "auto":
        return [(provider, api_key if provider in ("cnpjws", "receitaws") else None)]
    if api_key:
        other = "cnpjws" if key_provider == "receitaws" else "receitaws"
        return [("brasilapi", None), (key_provider, api_key), (other, None)]
    return [("brasilapi", None), ("cnpjws", None), ("receitaws", None)]


@dataclass
class LookupResult:
    ok: bool
    item: dict[str, Any]
    auth_failed: bool = False
    tried: list[dict] = field(default_factory=list)


HINTS = {
    "not_found": "CNPJ not found in the provider database(s). Check the number; very new companies can lag behind the monthly open-data dump.",
    "invalid_input": "Not a valid CNPJ (14 characters with correct check digits).",
    "rate_limited": "Provider rate limit hit after retries. Lower maxConcurrency, or use your own ReceitaWS / CNPJ.ws commercial key.",
    "unauthorized": "Your provider API key was rejected (HTTP 401). Check apiKey / keyProvider.",
    "forbidden": "Provider refused the request (HTTP 403). Check your plan / key permissions.",
    "server_error": "Provider server error after retries. Try again later.",
    "timeout": "Provider timed out after retries.",
    "network": "Network error reaching the provider.",
    "bad_response": "Provider returned an unexpected payload.",
    "skipped_auth": "Skipped the keyed provider after an earlier authentication failure (stopOnAuthError).",
}


async def lookup_cnpj(
    client: httpx.AsyncClient, cnpj: str, *, provider: str, api_key: str | None, key_provider: str,
    include_qsa: bool, max_retries: int, timeout: float, skip_keyed: bool = False, backoff_base: float = 1.0,
) -> LookupResult:
    chain = provider_chain(provider, api_key, key_provider)
    tried: list[dict] = []
    errors: list[Attempt] = []
    auth_failed = False
    total_attempts = 0
    for prov, key in chain:
        if key and skip_keyed:
            a = Attempt(prov, False, error_class="skipped_auth", message=HINTS["skipped_auth"], keyed=True)
        else:
            a = await fetch_provider(client, prov, cnpj, api_key=key, include_qsa=include_qsa,
                                     max_retries=max_retries, timeout=timeout, backoff_base=backoff_base)
        total_attempts += a.attempts
        tried.append({"provider": prov, "keyed": a.keyed, "ok": a.ok, "errorClass": a.error_class,
                      "httpStatus": a.http_status})
        if a.ok and a.record:
            rec = finalize(a.record)
            rec.update({"status": "ok", "errorClass": None, "attempts": total_attempts, "triedSources": tried})
            return LookupResult(True, rec, auth_failed, tried)
        if a.keyed and a.error_class in ("unauthorized", "forbidden"):
            auth_failed = True
        errors.append(a)
    # pick the most informative error: not_found beats transient errors; invalid_input beats all
    pick = next((e for e in errors if e.error_class == "invalid_input"), None) \
        or next((e for e in errors if e.error_class == "not_found"), None) \
        or next((e for e in errors if e.error_class in ("unauthorized", "forbidden")), None) \
        or errors[0]
    item = _base_record(cnpj)
    item.update({
        "status": "failed",
        "source": pick.provider if pick else "none",
        "sourceLabel": SOURCE_LABELS.get(pick.provider) if pick else None,
        "errorClass": pick.error_class or "error",
        "errorMessage": pick.message,
        "httpStatus": pick.http_status,
        "hint": HINTS.get(pick.error_class or "", None),
        "attempts": total_attempts,
        "triedSources": tried,
    })
    return LookupResult(False, scrub_values(item), auth_failed, tried)
