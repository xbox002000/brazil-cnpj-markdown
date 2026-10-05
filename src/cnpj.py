"""CNPJ normalization + check-digit validation (numeric and the 2026 alphanumeric format)."""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

CNPJ_FIELDS = ("cnpj", "CNPJ", "taxId", "tax_id", "cnpjNumber", "documento", "document")
_W1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
_W2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
_SHAPE = re.compile(r"^[A-Z0-9]{12}\d{2}$")


def _dv(base: str, weights: list[int]) -> int:
    # Receita Federal rule: value = ord(char) - 48 (digits 0-9, letters A=17 …), mod 11
    s = sum((ord(c) - 48) * w for c, w in zip(base, weights))
    r = s % 11
    return 0 if r < 2 else 11 - r


def is_valid_cnpj(c: str) -> bool:
    if not c or not _SHAPE.match(c):
        return False
    if c.isdigit() and len(set(c)) == 1:  # 00000000000000, 11111111111111 …
        return False
    return _dv(c[:12], _W1) == int(c[12]) and _dv(c[:13], _W2) == int(c[13])


def clean_cnpj(raw: Any) -> str:
    """Strip punctuation/spaces; uppercase. Pure-digit values shorter than 14 are zero-padded
    (spreadsheets drop leading zeros, e.g. 191 → 00000000000191)."""
    s = re.sub(r"[^0-9A-Za-z]", "", str(raw or "")).upper()
    if s.isdigit() and len(s) < 14:
        s = s.zfill(14)
    return s


def normalize_cnpj(raw: Any) -> str | None:
    c = clean_cnpj(raw)
    return c if is_valid_cnpj(c) else None


def format_cnpj(c: str) -> str:
    if not c or len(c) != 14:
        return c or ""
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}"


def cnpj_from_item(item: dict, field: str | None = None) -> Any:
    fields = ((field,) if field else ()) + CNPJ_FIELDS
    for f in fields:
        if f and item.get(f) not in (None, ""):
            return item.get(f)
    return None


def normalize_cnpjs(values: Iterable[Any] | str | None, field: str | None = None) -> tuple[list[str], list[str]]:
    """Return (unique valid CNPJs, invalid raw strings). Order preserved."""
    if values is None:
        return [], []
    if isinstance(values, str):
        values = [v for v in re.split(r"[\r\n,;]+", values) if v.strip()]
    out: list[str] = []
    bad: list[str] = []
    seen: set[str] = set()
    for v in values:
        raw = cnpj_from_item(v, field) if isinstance(v, dict) else v
        if raw is None or str(raw).strip() == "":
            if isinstance(v, dict):
                bad.append(json.dumps(v, ensure_ascii=False)[:80])
            continue
        c = normalize_cnpj(raw)
        if c:
            if c not in seen:
                seen.add(c)
                out.append(c)
        else:
            bad.append(str(raw).strip()[:80])
    return out, bad


def split_payload(payload: Any, field: str | None = None) -> tuple[list[str], list[str]]:
    """KV record → (cnpjs, invalid). Accepts list, newline/comma text, or {"cnpjs": [...]}."""
    if payload is None:
        return [], []
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        s = payload.strip()
        if s.startswith(("[", "{")):
            try:
                return split_payload(json.loads(s), field)
            except ValueError:
                pass
        return normalize_cnpjs(s, field)
    if isinstance(payload, dict):
        arr = payload.get("cnpjs") or payload.get("CNPJs") or payload.get("items")
        if arr:
            return normalize_cnpjs(arr, field)
        return normalize_cnpjs([payload], field)
    if isinstance(payload, list):
        return normalize_cnpjs(payload, field)
    return [], []
