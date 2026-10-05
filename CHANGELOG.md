# Changelog

## 0.1 (2026-10-05)
- Initial private build: CNPJ list → Markdown due-diligence cards (Identity, Status, CNAE, Address,
  QSA, Source attribution) + RAG chunks. BrasilAPI keyless default with CNPJ.ws / ReceitaWS fallback,
  optional buyer key (ReceitaWS Bearer / CNPJ.ws commercial). Check-digit validation (numeric +
  alphanumeric), dataset / KV inputs, contact-field stripping, PPE `cnpj-card`, failed rows free.
