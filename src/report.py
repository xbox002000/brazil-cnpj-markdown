"""Markdown due-diligence card, REPORT.md and RAG chunks. Inputs are already contact-free."""
from __future__ import annotations

import json
from typing import Any

PRIVACY_NOTE = "Email and telephone fields are intentionally not collected or output."

SOURCE_DESC = {
    "brasilapi": "BrasilAPI (public mirror of Receita Federal CNPJ open data)",
    "cnpjws": "CNPJ.ws (Receita Federal CNPJ open data)",
    "receitaws": "ReceitaWS (Receita Federal CNPJ data)",
}


def esc(v: Any, limit: int = 300) -> str:
    if v is None or v == "":
        return "—"
    s = str(v).replace("|", "\\|").replace("\n", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def brl(v: Any) -> str:
    if v is None:
        return "—"
    try:
        s = f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return esc(v)
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def _yn(v: Any) -> str:
    return "—" if v is None else ("Yes" if v else "No")


def card_sections(r: dict) -> list[tuple[str, list[str]]]:
    """Return [(section title, lines)] for one successful company."""
    nat = r.get("naturezaJuridica") or {}
    nat_s = " ".join(x for x in (nat.get("code"), nat.get("description")) if x) or None
    identity = [
        "| Field | Value |", "|---|---|",
        f"| Razão social | {esc(r.get('razaoSocial'))} |",
        f"| Nome fantasia | {esc(r.get('nomeFantasia'))} |",
        f"| CNPJ | `{esc(r.get('cnpjFormatted'))}` |",
        f"| Matriz / filial | {esc(r.get('matrizFilial'))} |",
        f"| Natureza jurídica | {esc(nat_s)} |",
        f"| Porte | {esc(r.get('porte'))} |",
        f"| Capital social | {brl(r.get('capitalSocial'))} |",
        f"| Início de atividade | {esc(r.get('dataInicioAtividade'))} |",
    ]
    if r.get("enteFederativo"):
        identity.append(f"| Ente federativo responsável | {esc(r.get('enteFederativo'))} |")

    sit = r.get("situacao")
    status = [
        "| Field | Value |", "|---|---|",
        f"| Situação cadastral | **{esc(sit)}**" + (f" (since {r['situacaoDate']})" if r.get("situacaoDate") else "") + " |",
        f"| Motivo | {esc(r.get('situacaoMotivo'))} |",
        f"| Situação especial | {esc(r.get('situacaoEspecial'))}"
        + (f" ({r['situacaoEspecialDate']})" if r.get("situacaoEspecialDate") else "") + " |",
    ]
    simp, mei = r.get("simples") or {}, r.get("mei") or {}
    if r.get("simples") is not None or r.get("mei") is not None:
        status.append(f"| Simples Nacional | {_yn(simp.get('optante'))}"
                      + (f" (opted {simp['dataOpcao']})" if simp.get("dataOpcao") else "")
                      + (f" (excluded {simp['dataExclusao']})" if simp.get("dataExclusao") else "") + " |")
        status.append(f"| MEI | {_yn(mei.get('optante'))} |")
    if sit and sit != "ATIVA":
        status += ["", f"> ⚠️ Registration status is **{esc(sit)}** — not active."]

    cp = r.get("cnaePrincipal") or {}
    cnae = [f"- **Principal:** `{esc(cp.get('codeFormatted'))}` — {esc(cp.get('description'))}"]
    sec = r.get("cnaesSecundarios") or []
    if sec:
        cnae.append(f"- **Secundários ({len(sec)}):**")
        cnae += [f"  - `{esc(c.get('codeFormatted'))}` — {esc(c.get('description'))}" for c in sec]
    else:
        cnae.append("- **Secundários:** none")

    a = r.get("address") or {}
    address = [f"{esc(r.get('addressLine'))}", ""]
    if a.get("codigoMunicipioIbge"):
        address.append(f"- Município IBGE code: `{a['codigoMunicipioIbge']}`")
    address.append(f"- _{PRIVACY_NOTE}_")

    sections = [("Identity", identity), ("Status", status), ("CNAE (economic activity)", cnae), ("Address", address)]

    if r.get("qsa") is not None:
        q = r.get("qsa") or []
        if q:
            qsa = ["| Name | Role | Since |", "|---|---|---|"]
            qsa += [f"| {esc(p.get('name'))} | {esc(p.get('role'))} | {esc(p.get('since'))} |" for p in q]
            qsa += ["", "_Names and roles only — no CPF fragments, ages or contact details._"]
        else:
            qsa = ["_No partners / administrators (QSA) listed by the source._"]
        sections.append((f"QSA — partners & administrators ({len(q)})", qsa))

    src = [
        f"- Source: {SOURCE_DESC.get(r.get('source') or '', esc(r.get('sourceLabel')))}",
        f"- Endpoint: <{r.get('sourceUrl')}>" if r.get("sourceUrl") else "- Endpoint: —",
    ]
    if r.get("sourceUpdatedAt"):
        src.append(f"- Source record updated: {r['sourceUpdatedAt']}")
    if r.get("checkedAt"):
        src.append(f"- Fetched: {r['checkedAt']}")
    src.append("- Underlying data: Receita Federal do Brasil — Cadastro Nacional da Pessoa Jurídica (public open data).")
    src.append(f"- {PRIVACY_NOTE}")
    sections.append(("Source attribution", src))
    return sections


def company_markdown(r: dict) -> str:
    title = r.get("razaoSocial") or r.get("cnpjFormatted")
    L = [f"## {esc(title, 160)}", "",
         f"**CNPJ** `{esc(r.get('cnpjFormatted'))}` · **{esc(r.get('situacao'))}** · source: {esc(r.get('sourceLabel'))}", ""]
    for name, lines in card_sections(r):
        L += [f"### {name}", "", *lines, ""]
    return "\n".join(L).rstrip() + "\n"


def failed_markdown(r: dict) -> str:
    return (f"## CNPJ `{esc(r.get('cnpjFormatted') or r.get('cnpj'))}` — failed (not charged)\n\n"
            f"- Error: `{esc(r.get('errorClass'))}`" + (f" (HTTP {r['httpStatus']})" if r.get("httpStatus") else "") + "\n"
            f"- Message: {esc(r.get('errorMessage'))}\n- Hint: {esc(r.get('hint'))}\n")


def build_markdown(rows: list[dict], summary: dict) -> str:
    ok = [r for r in rows if r.get("status") == "ok"]
    bad = [r for r in rows if r.get("status") != "ok"]
    L = ["# Brazil CNPJ — company due-diligence cards", "",
         f"- Generated: {summary.get('finishedAt')}",
         f"- Companies OK: **{len(ok)}** · failed (not charged): **{len(bad)}**",
         f"- Provider mode: `{summary.get('provider')}` · sources used: `{summary.get('bySource') or {}}`",
         f"- {PRIVACY_NOTE}", ""]
    for r in ok:
        L += [r.get("markdown") or company_markdown(r), "---", ""]
    if bad:
        L += ["## Failed (not charged)", "", "| CNPJ | Error | HTTP | Hint |", "|---|---|---|---|"]
        for r in bad:
            L.append(f"| `{esc(r.get('cnpjFormatted') or r.get('cnpj'), 40)}` | {esc(r.get('errorClass'))} | "
                     f"{esc(r.get('httpStatus'))} | {esc(r.get('hint') or r.get('errorMessage'), 160)} |")
        L.append("")
    return "\n".join(L).rstrip() + "\n"


def rag_chunks(r: dict, max_chars: int = 1200) -> list[dict]:
    """One chunk per card section (long sections split on line boundaries). Each chunk carries a
    self-describing header so it can be embedded standalone."""
    if r.get("status") != "ok":
        return []
    name = r.get("razaoSocial") or r.get("cnpjFormatted")
    head = f"{name} (CNPJ {r.get('cnpjFormatted')})"
    out: list[dict] = []
    for title, lines in card_sections(r):
        buf: list[str] = []
        parts: list[list[str]] = []
        size = 0
        for ln in lines:
            if buf and size + len(ln) + 1 > max_chars:
                parts.append(buf)
                buf, size = [], 0
            buf.append(ln)
            size += len(ln) + 1
        if buf:
            parts.append(buf)
        for i, p in enumerate(parts, 1):
            body = "\n".join(p).strip()
            # keep table header on continuation chunks
            if i > 1 and lines and lines[0].startswith("|") and not body.startswith(lines[0]):
                body = "\n".join(lines[:2]) + "\n" + body
            sec = title if len(parts) == 1 else f"{title} (part {i}/{len(parts)})"
            out.append({
                "id": f"{r.get('cnpj')}-{len(out) + 1}",
                "cnpj": r.get("cnpj"),
                "razaoSocial": r.get("razaoSocial"),
                "section": sec,
                "source": r.get("source"),
                "text": f"# {head} — {sec}\n\n{body}",
            })
    for i, c in enumerate(out, 1):
        c["chunkIndex"], c["chunkCount"] = i, len(out)
    return out


def build_chunks_jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(c, ensure_ascii=False) + "\n" for r in rows for c in rag_chunks(r))
