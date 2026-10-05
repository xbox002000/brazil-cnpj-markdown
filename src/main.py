"""Apify Actor: Brazil CNPJ list → Markdown due-diligence cards (+ RAG chunks).

Providers: BrasilAPI (keyless, default) with fallback to CNPJ.ws / ReceitaWS public JSON, or the
buyer's own ReceitaWS / CNPJ.ws commercial key. Never outputs email / telephone. No lead lists.
Output: dataset row per CNPJ + KV REPORT.md, SUMMARY, OUTPUT, optional CHUNKS.jsonl.
Pay-per-event: `cnpj-card` per successful company card. Invalid / not found / failed rows are free.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import resource
import time
from collections import Counter

import httpx
from apify import Actor

from charging import Charger
from cnpj import cnpj_from_item, format_cnpj, normalize_cnpjs, split_payload
from providers import KEY_HINT, UA, lookup_cnpj, scrub_values
from report import build_chunks_jsonl, build_markdown, company_markdown

EVENT_CARD = "cnpj-card"
PROVIDERS = ("auto", "brasilapi", "cnpjws", "receitaws")
PLACEHOLDER_KEYS = {"", "YOUR_KEY", "YOUR_TOKEN", "xxx", "TODO", "changeme"}

for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


def _int(inp: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = inp.get(key)
    try:
        v = int(default if v is None or v == "" else v)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


def _clean_key(raw) -> str | None:
    if not raw:
        return None
    k = str(raw).strip()
    return None if k in PLACEHOLDER_KEYS or k.upper() in PLACEHOLDER_KEYS else k


async def load_from_dataset(dataset_id: str, field: str | None, limit: int | None) -> tuple[list, list[str]]:
    vals: list = []
    try:
        ds = await Actor.open_dataset(id=dataset_id, force_cloud=bool(os.getenv("APIFY_IS_AT_HOME")))
        async for item in ds.iterate_items():
            if isinstance(item, dict):
                v = cnpj_from_item(item, field)
                if v is not None:
                    vals.append(v)
            if limit and len(vals) >= limit:
                break
    except Exception as e:  # noqa: BLE001
        return [], [f"Could not read dataset {dataset_id}: {e}"]
    return vals, ([] if vals else [f"No CNPJ fields found in dataset {dataset_id}"])


async def load_from_kv(store_id: str, key: str, field: str | None) -> tuple[list[str], list[str], list[str]]:
    try:
        store = await Actor.open_key_value_store(id=store_id, force_cloud=bool(os.getenv("APIFY_IS_AT_HOME")))
        rec = await store.get_value(key)
    except Exception as e:  # noqa: BLE001
        return [], [], [f"Could not read KV {store_id}/{key}: {e}"]
    if rec is None:
        return [], [], [f"KV record {key} not found in store {store_id}"]
    good, bad = split_payload(rec, field)
    return good, bad, ([] if good or bad else [f"No CNPJs found in KV record {key}"])


async def main() -> None:
    async with Actor:
        t0 = time.time()
        inp = await Actor.get_input() or {}
        charger = Charger(EVENT_CARD)

        provider = str(inp.get("provider") or "auto").strip().lower()
        if provider not in PROVIDERS:
            raise ValueError(f"`provider` must be one of {'|'.join(PROVIDERS)}, got {provider!r}")
        key_provider = str(inp.get("keyProvider") or "receitaws").strip().lower()
        if key_provider not in ("receitaws", "cnpjws"):
            key_provider = "receitaws"
        api_key = _clean_key(inp.get("apiKey"))
        key_source = "input" if api_key else None
        if not api_key:  # private smoke only — never set as Actor env vars
            for env_name, kp in (("RECEITAWS_API_KEY", "receitaws"), ("CNPJWS_API_TOKEN", "cnpjws")):
                if os.getenv(env_name, "").strip() and provider in ("auto", kp):
                    api_key, key_source, key_provider = os.environ[env_name].strip(), f"env:{env_name}", kp
                    break
        if provider == "brasilapi" and api_key:
            Actor.log.info("provider=brasilapi is keyless; apiKey is ignored.")
            api_key = None

        include_qsa = bool(inp.get("includeQsa", True))
        include_chunks = bool(inp.get("includeRagChunks", True))
        max_items = _int(inp, "maxItems", 100, 0, 100000) or None
        conc = _int(inp, "maxConcurrency", 2, 1, 5)
        max_retries = _int(inp, "maxRetries", 3, 0, 6)
        timeout = float(_int(inp, "requestTimeoutSecs", 30, 5, 120))
        stop_on_auth = bool(inp.get("stopOnAuthError", True))
        field = (str(inp.get("cnpjField") or "").strip() or None)

        raw_vals = list(inp.get("cnpjs") or []) if not isinstance(inp.get("cnpjs"), str) else [inp["cnpjs"]]
        warnings: list[str] = []
        if inp.get("datasetId"):
            v, w = await load_from_dataset(str(inp["datasetId"]), field, max_items)
            raw_vals += v
            warnings += w
            Actor.log.info(f"Loaded {len(v)} value(s) from dataset {inp['datasetId']}")
        cnpjs, bad = normalize_cnpjs(raw_vals, field)
        if inp.get("keyValueStoreId"):
            key = str(inp.get("keyValueRecordKey") or "INPUT_CNPJS")
            g, b, w = await load_from_kv(str(inp["keyValueStoreId"]), key, field)
            cnpjs = list(dict.fromkeys(cnpjs + g))
            bad += b
            warnings += w
            Actor.log.info(f"Loaded {len(g)} CNPJ(s) from KV {inp['keyValueStoreId']}/{key}")
        for w in warnings:
            Actor.log.warning(w)
        if max_items is not None and len(cnpjs) > max_items:
            Actor.log.warning(f"{len(cnpjs)} CNPJs; capped to maxItems={max_items}.")
            cnpjs = cnpjs[:max_items]
        if not cnpjs and not bad:
            raise ValueError("Provide at least one CNPJ in `cnpjs` (or a datasetId / keyValueStoreId with CNPJs).")

        Actor.log.info(
            f"{len(cnpjs)} CNPJ(s) (+{len(bad)} invalid); provider={provider}; concurrency={conc}; "
            f"key={'yes (' + key_source + ', ' + key_provider + ', value hidden)' if api_key else 'no (keyless public APIs)'}"
        )
        await Actor.set_status_message(f"Looking up {len(cnpjs)} CNPJ(s) via {provider}…")

        rows: list[tuple[int, dict]] = []
        cls_counts: Counter[str] = Counter()
        src_counts: Counter[str] = Counter()
        state = {"done": 0, "ok": 0, "fail": 0, "skipped": 0, "inflight": 0, "auth_failed": False}

        for i, raw in enumerate(bad):  # invalid inputs → free rows up front
            item = {"cnpj": raw, "cnpjFormatted": raw, "status": "failed", "source": "none",
                    "errorClass": "invalid_input", "httpStatus": None, "attempts": 0,
                    "errorMessage": "Not a valid CNPJ after normalization (14 characters, check digits).",
                    "hint": "Use 14 digits (punctuation is fine), e.g. 33.000.167/0001-01. Check digits are verified."}
            rows.append((-100000 + i, item))
            cls_counts["invalid_input"] += 1
            state["fail"] += 1
            await charger.push_free(item)

        sem = asyncio.Semaphore(conc)
        lock = asyncio.Lock()
        async with httpx.AsyncClient(headers={"User-Agent": UA, "Accept": "application/json"},
                                     follow_redirects=True, limits=httpx.Limits(max_connections=conc + 2)) as client:

            async def one(idx: int, cnpj: str) -> None:
                async with sem:
                    async with lock:
                        if charger.limit_reached:
                            state["skipped"] += 1
                            return
                        b = charger.budget()
                        if b is not None and b - state["inflight"] <= 0:
                            state["skipped"] += 1
                            charger.limit_reached = True
                            return
                        skip_keyed = stop_on_auth and state["auth_failed"]
                        state["inflight"] += 1
                    try:
                        res = await lookup_cnpj(client, cnpj, provider=provider, api_key=api_key,
                                                key_provider=key_provider, include_qsa=include_qsa,
                                                max_retries=max_retries, timeout=timeout, skip_keyed=skip_keyed)
                        if res.auth_failed and not state["auth_failed"]:
                            state["auth_failed"] = True
                            Actor.log.warning("Provider rejected the API key (401/403)"
                                              + ("; skipping the keyed provider for remaining CNPJs." if stop_on_auth else "."))
                    finally:
                        async with lock:
                            state["inflight"] -= 1
                    item = res.item
                    item["checkedAt"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
                    if res.ok:
                        item["markdown"] = company_markdown(item)
                    item = scrub_values(item)
                    async with lock:
                        state["done"] += 1
                        if res.ok:
                            n = await charger.push_and_charge([item], event=EVENT_CARD)
                            if n:
                                state["ok"] += 1
                                cls_counts["ok"] += 1
                                src_counts[item.get("source") or "unknown"] += 1
                                rows.append((idx, item))
                            else:
                                state["skipped"] += 1
                        else:
                            state["fail"] += 1
                            cls_counts[item.get("errorClass") or "error"] += 1
                            rows.append((idx, item))
                            await charger.push_free(item)
                        Actor.log.info(
                            f"[{state['done']}/{len(cnpjs)}] {format_cnpj(cnpj)}: "
                            + (f"OK {item.get('razaoSocial')} [{item.get('situacao')}] via {item.get('source')}"
                               if res.ok else
                               f"FAILED {item.get('errorClass')} (HTTP {item.get('httpStatus')}, free)")
                        )
                        await Actor.set_status_message(
                            f"{state['done']}/{len(cnpjs)} done — ok {state['ok']}, failed {state['fail']}")

            await asyncio.gather(*(one(i, c) for i, c in enumerate(cnpjs)))

        rows.sort(key=lambda t: t[0])
        out_rows = [r for _, r in rows]
        peak_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
        summary = {
            "provider": provider,
            "apiKeyProvided": bool(api_key),
            "keyProvider": key_provider if api_key else None,
            "cnpjsPlanned": len(cnpjs),
            "invalidInputs": len(bad),
            "cnpjsOk": state["ok"],
            "cnpjsFailed": state["fail"],
            "skipped": state["skipped"],
            "byClass": dict(cls_counts),
            "bySource": dict(src_counts),
            "includeQsa": include_qsa,
            "includeRagChunks": include_chunks,
            "authFailed": state["auth_failed"],
            "keyHint": KEY_HINT if (cls_counts.get("rate_limited") or state["auth_failed"]) else None,
            "charged": dict(charger.counts),
            "spendingLimitReached": charger.limit_reached,
            "durationSecs": round(time.time() - t0, 1),
            "peakMemoryMb": peak_mb,
            "finishedAt": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
        store = await Actor.open_key_value_store()
        await store.set_value("REPORT.md", build_markdown(out_rows, summary), content_type="text/markdown; charset=utf-8")
        chunk_count = 0
        if include_chunks:
            jsonl = build_chunks_jsonl(out_rows)
            chunk_count = jsonl.count("\n")
            await store.set_value("CHUNKS.jsonl", jsonl, content_type="application/x-ndjson; charset=utf-8")
        summary["ragChunks"] = chunk_count
        await store.set_value("SUMMARY", summary)
        await store.set_value("OUTPUT", {
            "summary": summary,
            "companies": [{k: r.get(k) for k in ("cnpj", "cnpjFormatted", "status", "razaoSocial", "nomeFantasia",
                                                  "situacao", "source", "errorClass")} for r in out_rows],
        })
        msg = (f"Done: {state['ok']} card(s) OK, {state['fail']} failed (free), {state['skipped']} skipped. "
               f"Charged: {charger.counts or 'nothing'}.")
        Actor.log.info(msg + f" Peak memory {peak_mb} MB, {summary['durationSecs']} s.")
        await Actor.set_status_message(msg, is_terminal=True)


if __name__ == "__main__":
    asyncio.run(main())
