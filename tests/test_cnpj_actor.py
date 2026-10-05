import asyncio
import json
import os
import re

import httpx
import pytest

import cnpj as C
import providers as P
import report as R

HERE = os.path.dirname(__file__)
FX = lambda n: json.load(open(os.path.join(HERE, "fixtures", n)))  # noqa: E731
PETRO = "33000167000101"
CONTACT_NEEDLES = ("petrobras.com.br", "2166", "21660000", "2121660000", "213224", "@", "telefone", "email", "ddd", "fax")


def run(c):
    return asyncio.run(c)


def assert_no_contacts(obj):
    blob = json.dumps(obj, ensure_ascii=False).lower()
    for n in CONTACT_NEEDLES:
        assert n not in blob, f"contact leak: {n}"


def client_for(routes: dict, calls: list | None = None):
    """routes: host-substring → callable(request) or (status, json)."""
    def handler(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(req)
        for key, val in routes.items():
            if key in str(req.url):
                if callable(val):
                    return val(req)
                status, body = val
                return httpx.Response(status, json=body)
        return httpx.Response(599, json={"message": "unrouted"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------- normalization / validation ----------
def test_cnpj_checksum_and_normalize():
    assert C.normalize_cnpj("33.000.167/0001-01") == PETRO
    assert C.normalize_cnpj(" 33000167000101 ") == PETRO
    assert C.normalize_cnpj("191") == "00000000000191"  # Banco do Brasil, leading zeros dropped by spreadsheets
    assert C.normalize_cnpj("33000167000102") is None  # bad check digit
    assert C.normalize_cnpj("11111111111111") is None  # repeated digits
    assert C.normalize_cnpj("") is None and C.normalize_cnpj("abc") is None
    assert C.format_cnpj(PETRO) == "33.000.167/0001-01"
    good, bad = C.normalize_cnpjs(["33.000.167/0001-01", PETRO, "123", {"cnpj": "11222333000181"}, "x"])
    assert good == [PETRO, "11222333000181"]
    assert "123" in bad and "x" in bad


def test_alphanumeric_cnpj_checksum():
    # Official Receita Federal example of the 2026 alphanumeric format: 12.ABC.345/01DE-35
    assert C.normalize_cnpj("12.ABC.345/01DE-35") == "12ABC34501DE35"
    assert C.normalize_cnpj("12ABC34501DE36") is None


def test_split_payload_variants():
    assert C.split_payload(["33000167000101"])[0] == [PETRO]
    assert C.split_payload({"cnpjs": ["33.000.167/0001-01"]})[0] == [PETRO]
    assert C.split_payload("33000167000101\n11222333000181\n")[0] == [PETRO, "11222333000181"]
    assert C.split_payload(b'["33000167000101"]')[0] == [PETRO]
    assert C.split_payload([{"taxId": PETRO}])[0] == [PETRO]


def test_strip_contacts_and_scrub():
    raw = {"email": "a@b.com", "ddd_telefone_1": "1199", "telefone1": "x", "ddd_fax": "1", "fax": "2",
           "nested": [{"contact_email": "z", "nome": "OK", "obs": "write to foo@bar.com"}], "keep": 1}
    s = P.scrub_values(raw)
    assert s == {"nested": [{"nome": "OK", "obs": "write to [redacted]"}], "keep": 1}


@pytest.mark.parametrize("prov", ["brasilapi", "cnpjws", "receitaws"])
def test_normalizers_petrobras_no_contacts(prov):
    rec = P.finalize(P.NORMALIZERS[prov](FX(f"{prov}_{PETRO}.json"), PETRO, True))
    assert rec["cnpj"] == PETRO
    assert rec["razaoSocial"] == "PETROLEO BRASILEIRO S A PETROBRAS"
    assert rec["nomeFantasia"] == "PETROBRAS - EDISE"
    assert rec["situacao"] == "ATIVA"
    assert rec["cnaePrincipal"]["codeFormatted"] == "0600-0/01"
    assert rec["cnaesSecundariosCount"] == 5
    assert rec["address"]["uf"] == "RJ" and rec["address"]["cep"] == "20031170"
    assert rec["naturezaJuridica"]["code"] == "203-8"
    assert rec["capitalSocial"] > 2e11
    assert rec["qsaCount"] >= 8
    for q in rec["qsa"]:
        assert set(q) == {"name", "role", "since"}
    assert "MAGDA MARIA DE REGINA CHAMBRIARD" in [q["name"] for q in rec["qsa"]]
    assert_no_contacts(rec)
    assert "***" not in json.dumps(rec)  # no masked CPF fragments


def test_include_qsa_false():
    rec = P.finalize(P.normalize_brasilapi(FX(f"brasilapi_{PETRO}.json"), PETRO, False))
    assert rec["qsa"] is None and rec["qsaCount"] is None
    md = R.company_markdown({**rec, "source": "brasilapi", "sourceLabel": "BrasilAPI"})
    assert "### QSA" not in md


# ---------- HTTP / providers ----------
def test_brasilapi_success_auto():
    calls = []
    async def go():
        async with client_for({"brasilapi": (200, FX(f"brasilapi_{PETRO}.json"))}, calls) as c:
            return await P.lookup_cnpj(c, PETRO, provider="auto", api_key=None, key_provider="receitaws",
                                       include_qsa=True, max_retries=0, timeout=5)
    res = run(go())
    assert res.ok and res.item["source"] == "brasilapi"
    assert res.item["sourceUrl"] == f"https://brasilapi.com.br/api/cnpj/v1/{PETRO}"
    assert len(calls) == 1
    assert_no_contacts(res.item)


def test_auto_fallback_to_cnpjws_on_brasilapi_500():
    async def go():
        async with client_for({"brasilapi": (500, {"message": "boom"}),
                               "publica.cnpj.ws": (200, FX(f"cnpjws_{PETRO}.json"))}) as c:
            return await P.lookup_cnpj(c, PETRO, provider="auto", api_key=None, key_provider="receitaws",
                                       include_qsa=True, max_retries=1, timeout=5, backoff_base=0)
    res = run(go())
    assert res.ok and res.item["source"] == "cnpjws"
    assert [t["provider"] for t in res.item["triedSources"]] == ["brasilapi", "cnpjws"]
    assert res.item["triedSources"][0]["errorClass"] == "server_error"


def test_auto_all_not_found_is_free_failure():
    async def go():
        async with client_for({"brasilapi": (404, {"message": "CNPJ não encontrado"}),
                               "cnpj.ws": (404, {"status": 404, "titulo": "Não encontrado"}),
                               "receitaws": (200, {"status": "ERROR", "message": "CNPJ rejeitado pela Receita Federal"})}) as c:
            return await P.lookup_cnpj(c, "11222333000181", provider="auto", api_key=None, key_provider="receitaws",
                                       include_qsa=True, max_retries=0, timeout=5)
    res = run(go())
    assert not res.ok
    assert res.item["errorClass"] == "not_found" and res.item["status"] == "failed"
    assert len(res.item["triedSources"]) == 3


def test_429_retry_honours_retry_after_then_ok():
    n = {"i": 0}
    def h(req):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"})
        return httpx.Response(200, json=FX(f"brasilapi_{PETRO}.json"))
    async def go():
        async with client_for({"brasilapi": h}) as c:
            return await P.lookup_cnpj(c, PETRO, provider="brasilapi", api_key=None, key_provider="receitaws",
                                       include_qsa=True, max_retries=2, timeout=5, backoff_base=0)
    res = run(go())
    assert res.ok and res.item["attempts"] == 2


def test_receitaws_keyed_bearer_and_401_redacted():
    seen = []
    def h(req):
        seen.append(req.headers.get("authorization"))
        return httpx.Response(401, text="invalid token SECRET123")
    async def go():
        async with client_for({"receitaws": h}) as c:
            return await P.lookup_cnpj(c, PETRO, provider="receitaws", api_key="SECRET123", key_provider="receitaws",
                                       include_qsa=True, max_retries=2, timeout=5)
    res = run(go())
    assert seen == ["Bearer SECRET123"]  # 401 not retried
    assert not res.ok and res.auth_failed and res.item["errorClass"] == "unauthorized"
    assert "SECRET123" not in json.dumps(res.item)


def test_cnpjws_commercial_token_header():
    seen = []
    def h(req):
        seen.append((req.url.host, req.headers.get("x_api_token")))
        return httpx.Response(200, json=FX(f"cnpjws_{PETRO}.json"))
    async def go():
        async with client_for({"cnpj.ws": h}) as c:
            return await P.lookup_cnpj(c, PETRO, provider="cnpjws", api_key="TOK", key_provider="cnpjws",
                                       include_qsa=True, max_retries=0, timeout=5)
    res = run(go())
    assert res.ok and seen == [("comercial.cnpj.ws", "TOK")]
    assert res.item["sourceUrl"].startswith("https://comercial.cnpj.ws/")


def test_auto_with_key_skip_keyed_after_auth_failure():
    calls = []
    async def go():
        async with client_for({"brasilapi": (503, {}), "receitaws": (200, FX(f"receitaws_{PETRO}.json")),
                               "publica.cnpj.ws": (200, FX(f"cnpjws_{PETRO}.json"))}, calls) as c:
            return await P.lookup_cnpj(c, PETRO, provider="auto", api_key="K", key_provider="receitaws",
                                       include_qsa=True, max_retries=0, timeout=5, skip_keyed=True)
    res = run(go())
    assert res.ok and res.item["source"] == "cnpjws"
    assert res.item["triedSources"][1]["errorClass"] == "skipped_auth"
    assert not any("receitaws" in str(r.url) for r in calls)


def test_timeout_classified():
    def h(req):
        raise httpx.ReadTimeout("slow", request=req)
    async def go():
        async with client_for({"brasilapi": h}) as c:
            return await P.lookup_cnpj(c, PETRO, provider="brasilapi", api_key=None, key_provider="receitaws",
                                       include_qsa=True, max_retries=1, timeout=5, backoff_base=0)
    res = run(go())
    assert not res.ok and res.item["errorClass"] == "timeout" and res.item["attempts"] == 2


def test_provider_chain():
    assert P.provider_chain("auto", None, "receitaws") == [("brasilapi", None), ("cnpjws", None), ("receitaws", None)]
    assert P.provider_chain("auto", "K", "cnpjws") == [("brasilapi", None), ("cnpjws", "K"), ("receitaws", None)]
    assert P.provider_chain("brasilapi", "K", "receitaws") == [("brasilapi", None)]


def test_test_base_override_is_localhost_only():
    src = open(os.path.join(HERE, "..", "src", "providers.py")).read()
    assert 'startswith(("http://127.0.0.1", "http://localhost"))' in src


# ---------- report ----------
def _ok_item():
    rec = P.finalize(P.normalize_brasilapi(FX(f"brasilapi_{PETRO}.json"), PETRO, True))
    rec.update({"source": "brasilapi", "sourceLabel": "BrasilAPI", "status": "ok",
                "sourceUrl": f"https://brasilapi.com.br/api/cnpj/v1/{PETRO}", "checkedAt": "2026-10-05T08:00:00+00:00"})
    rec["markdown"] = R.company_markdown(rec)
    return rec


def test_markdown_card_sections():
    md = _ok_item()["markdown"]
    for h in ("### Identity", "### Status", "### CNAE", "### Address", "### QSA", "### Source attribution"):
        assert h in md
    assert "PETROLEO BRASILEIRO S A PETROBRAS" in md
    assert "33.000.167/0001-01" in md and "0600-0/01" in md
    assert "R$ 205.431.960.000,00" in md
    assert "RIO DE JANEIRO/RJ" in md and "CEP 20031-170" in md
    assert "Receita Federal" in md
    assert_no_contacts(md.replace("Email and telephone fields are intentionally not collected or output.", ""))


def test_report_and_chunks():
    ok = _ok_item()
    bad = {"cnpj": "11222333000181", "cnpjFormatted": "11.222.333/0001-81", "status": "failed",
           "errorClass": "not_found", "httpStatus": 404, "hint": "x"}
    summary = {"finishedAt": "t", "provider": "auto", "bySource": {"brasilapi": 1}}
    md = R.build_markdown([ok, bad], summary)
    assert "Companies OK: **1**" in md and "## Failed (not charged)" in md and "not_found" in md
    lines = R.build_chunks_jsonl([ok, bad]).strip().split("\n")
    chunks = [json.loads(x) for x in lines]
    assert len(chunks) >= 6 and all(c["cnpj"] == PETRO for c in chunks)
    secs = [c["section"] for c in chunks]
    assert secs[0] == "Identity" and any(s.startswith("QSA") for s in secs)
    assert all(c["text"].startswith("# PETROLEO BRASILEIRO S A PETROBRAS (CNPJ 33.000.167/0001-01)") for c in chunks)
    assert chunks[-1]["chunkCount"] == len(chunks)


def test_long_qsa_section_splits_with_header():
    ok = _ok_item()
    ok["qsa"] = [{"name": f"PERSON {i:03d} " + "X" * 40, "role": "Diretor", "since": "2020-01-01"} for i in range(80)]
    chunks = R.rag_chunks(ok)
    qsa = [c for c in chunks if c["section"].startswith("QSA")]
    assert len(qsa) > 1
    assert all("| Name | Role | Since |" in c["text"] for c in qsa)
    assert all(len(c["text"]) < 1600 for c in chunks)


def test_non_active_warning():
    ok = _ok_item()
    ok["situacao"] = "BAIXADA"
    assert "not active" in R.company_markdown(ok)
