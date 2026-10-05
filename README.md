# Brazil CNPJ → Company Markdown DD Card

Turn a list of **Brazilian CNPJs** into clean **company due-diligence cards** — Markdown for humans and LLM/RAG pipelines, structured JSON for your ERP, KYB or vendor-master cleanup.

- **Keyless by default** — uses the public **BrasilAPI** CNPJ JSON (Receita Federal open data), with automatic fallback to **CNPJ.ws** and **ReceitaWS** public endpoints
- **Bring your own key** for production volume — ReceitaWS token or CNPJ.ws commercial token
- **Markdown card per company**: Identity · Status · CNAE · Address · QSA (partners) · Source attribution
- **RAG chunks** (`CHUNKS.jsonl`) — one self-describing chunk per card section, ready to embed
- **No contact harvesting** — email, telephone, fax and DDD fields are stripped from every provider payload. This is a due-diligence tool, not a lead list.
- **256 MB** default memory, no browser, official/public JSON APIs only
- **Failed rows are free** — invalid CNPJ, not found, rate-limited, provider errors

## Quick start

```json
{
  "cnpjs": ["33.000.167/0001-01", "00.000.000/0001-91"]
}
```

That is all you need — `provider: auto` tries BrasilAPI first and falls back automatically.

## Input

| Field | Type | Default | Notes |
|---|---|---|---|
| `cnpjs` | string[] | — | Punctuation OK. Digits-only values shorter than 14 are left-padded with zeros (spreadsheets drop them). Check digits are validated; invalid rows are free. Supports the 2026 alphanumeric CNPJ format. |
| `datasetId` | string | — | Read CNPJs from another run's dataset (`cnpjField`, else `cnpj` / `CNPJ` / `taxId` / `documento`) |
| `keyValueStoreId` + `keyValueRecordKey` | string | `INPUT_CNPJS` | JSON array, `{"cnpjs": [...]}` or one CNPJ per line |
| `cnpjField` | string | — | Field name in dataset items / JSON objects |
| `provider` | enum | `auto` | `auto` · `brasilapi` · `cnpjws` · `receitaws` |
| `apiKey` | secret | — | Optional ReceitaWS token (`Authorization: Bearer`) or CNPJ.ws commercial token (`x_api_token`) |
| `keyProvider` | enum | `receitaws` | Which provider your key belongs to in `auto` mode |
| `stopOnAuthError` | boolean | true | After a 401/403 from the keyed provider, skip it for the remaining CNPJs |
| `includeQsa` | boolean | true | Partners/administrators: **name, role, entry date only** |
| `includeRagChunks` | boolean | true | Write `CHUNKS.jsonl` |
| `maxItems` | integer | 100 | 0 = no limit |
| `maxConcurrency` | integer | 2 | 1–5 |
| `maxRetries` | integer | 3 | 429 (honours `Retry-After`), 5xx, timeouts |
| `requestTimeoutSecs` | integer | 30 | Per HTTP call |

### Provider order (`auto`)

1. **BrasilAPI** `https://brasilapi.com.br/api/cnpj/v1/{cnpj}` (keyless)
2. With `apiKey`: your keyed provider (ReceitaWS or CNPJ.ws commercial). Without: **CNPJ.ws** public
3. The remaining keyless endpoint (ReceitaWS or CNPJ.ws public)

Keyless CNPJ.ws / ReceitaWS endpoints are rate-limited (about 3 requests/minute), which is why they are fallbacks only.

## Output

**Dataset** — one row per CNPJ:

`cnpj`, `cnpjFormatted`, `razaoSocial`, `nomeFantasia`, `matrizFilial`, `situacao`, `situacaoDate`, `situacaoMotivo`, `situacaoEspecial`, `dataInicioAtividade`, `naturezaJuridica {code, description}`, `porte`, `capitalSocial`, `cnaePrincipal {code, codeFormatted, description}`, `cnaesSecundarios[]`, `address {tipoLogradouro, logradouro, numero, complemento, bairro, municipio, uf, cep, codigoMunicipioIbge}`, `addressLine`, `simples`, `mei`, `qsa[] {name, role, since}`, `qsaCount`, `markdown`, `source`, `sourceUrl`, `sourceUpdatedAt`, `triedSources`, `status`, `errorClass`, `errorMessage`, `hint`.

Views: **Overview** and **Failed (not charged)**.

**Key-value store**:

- `REPORT.md` — all cards + a failed table
- `CHUNKS.jsonl` — `{id, cnpj, razaoSocial, section, source, text, chunkIndex, chunkCount}` per line
- `SUMMARY` / `OUTPUT` — counts by result class and source, charged events, duration, peak memory

### Example card (excerpt)

```markdown
## PETROLEO BRASILEIRO S A PETROBRAS

**CNPJ** `33.000.167/0001-01` · **ATIVA** · source: BrasilAPI

### CNAE (economic activity)

- **Principal:** `0600-0/01` — Extração de petróleo e gás natural
- **Secundários (5):** …

### Address

AVENIDA REPUBLICA DO CHILE, 65 — CENTRO — RIO DE JANEIRO/RJ — CEP 20031-170
```

`errorClass` values: `invalid_input`, `not_found`, `rate_limited`, `unauthorized`, `forbidden`, `server_error`, `timeout`, `network`, `bad_response`, `skipped_auth`.

## Privacy

- Email, telephone, fax and DDD fields are **dropped** from every provider payload before processing, and every output string is scrubbed for email addresses.
- QSA contains **names and roles only** (plus entry date when the source has it) — no masked CPF fragments, no age bands, no contact data.
- Your `apiKey` is stored encrypted by Apify and is never logged or written to output.

## Pricing (pay per event)

| Event | Price | When |
|---|---|---|
| Actor start | $0.001 | once per run |
| `cnpj-card` | $0.003 | per **successful** company card |

Invalid CNPJs, not found, rate-limited after retries, auth errors and provider failures are **free**.

## Data sources

Company data originates from the **Receita Federal do Brasil** CNPJ open-data release, served by BrasilAPI, CNPJ.ws and ReceitaWS. Each card names the provider and endpoint it came from. Use of a keyed provider follows that provider's own terms.
