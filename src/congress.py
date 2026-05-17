"""House of Representatives stock trade disclosures (PTR filings).

Pulls directly from the House Clerk's own data — the most authoritative
source possible, free, no auth, refreshed continuously by the government:

  https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip

The ZIP contains an XML index of all filings; PTR (Periodic Transaction
Report) entries link to per-member PDFs we parse with pdfplumber.

Senate is intentionally NOT included — efdsearch.senate.gov is behind
Akamai bot protection that blocks scripted access. Would require a
headless browser (Playwright) which doesn't run cleanly on GHA without
significant added complexity.

Caching:
  - ZIP index: 4 hours (refreshes throughout the day as filings post)
  - PDFs:       permanent (DocIDs are immutable)
  - Parsed:     reuses the PDF cache, parsed on demand
"""
from __future__ import annotations
import io
import json
import re
import zipfile
from dataclasses import dataclass, asdict
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
import requests
import pdfplumber

from . import config


USER_AGENT = "scott-bot-trader/1.0 (research)"
CACHE_DIR = config.DATA_DIR / "congress_cache"
PDF_CACHE = CACHE_DIR / "pdfs"
INDEX_CACHE = CACHE_DIR / "index"
INDEX_TTL_HOURS = 4

# Transaction type codes per House Clerk reference:
#   P = Purchase, S = Sale (full), S (partial) = partial sale, E = Exchange
PURCHASE_CODES = {"P"}
SALE_CODES = {"S"}
# Exchange/corporate actions usually aren't conviction signals — flag separately
EXCHANGE_CODES = {"E"}

# Ticker pattern: 1-5 uppercase letters in parentheses or brackets, optionally with dots
TICKER_PATTERN = re.compile(r"\(([A-Z]{1,5}(?:\.[A-Z])?)\)")
TRANSACTION_TYPE_PATTERN = re.compile(r"\s([PSE])(?:\s\(partial\))?\s+(\d{2}/\d{2}/\d{4})")
AMOUNT_PATTERN = re.compile(r"\$[\d,]+\s*-\s*\$[\d,]+|\$[\d,]+\+|Over\s+\$[\d,]+")


@dataclass
class CongressTrade:
    member_name: str
    state_district: str
    owner: str             # SP (spouse), DC (dependent child), JT (joint), or member
    ticker: str
    asset_desc: str
    transaction_type: str  # "purchase", "sale", "exchange"
    transaction_date: str  # MM/DD/YYYY
    amount_range: str
    filing_date: str
    doc_id: str


def _ensure_cache_dirs():
    PDF_CACHE.mkdir(parents=True, exist_ok=True)
    INDEX_CACHE.mkdir(parents=True, exist_ok=True)


def _http_get(url: str, timeout: int = 30) -> requests.Response | None:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        if r.ok:
            return r
        return None
    except requests.RequestException:
        return None


def _index_cache_path(year: int) -> Path:
    return INDEX_CACHE / f"{year}_index.json"


def _is_cache_fresh(p: Path, ttl_hours: int) -> bool:
    if not p.exists():
        return False
    age_h = (datetime.now(timezone.utc).timestamp() - p.stat().st_mtime) / 3600
    return age_h < ttl_hours


def download_index(year: int | None = None) -> list[dict]:
    """Return list of {Last, First, FilingType, FilingDate, DocID, State, ...} entries.
    Cached for INDEX_TTL_HOURS."""
    _ensure_cache_dirs()
    year = year or date.today().year
    cache_path = _index_cache_path(year)
    if _is_cache_fresh(cache_path, INDEX_TTL_HOURS):
        try:
            return json.loads(cache_path.read_text())
        except json.JSONDecodeError:
            pass

    url = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
    print(f"[congress] downloading {url}...")
    resp = _http_get(url)
    if resp is None:
        return []

    try:
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        xml_bytes = z.read(f"{year}FD.xml")
    except (zipfile.BadZipFile, KeyError) as e:
        print(f"[congress] zip parse failed: {e}")
        return []

    root = ET.fromstring(xml_bytes)
    entries = []
    for member in root.iter("Member"):
        fields = {child.tag: (child.text or "").strip() for child in member}
        entries.append(fields)
    cache_path.write_text(json.dumps(entries))
    return entries


def recent_ptrs(days: int = 14, year: int | None = None) -> list[dict]:
    """PTR filings from the last N days, most recent first."""
    year = year or date.today().year
    entries = download_index(year)
    cutoff = date.today() - timedelta(days=days)
    ptrs = []
    for e in entries:
        if e.get("FilingType") != "P":
            continue
        try:
            filing_date = datetime.strptime(e.get("FilingDate", ""), "%m/%d/%Y").date()
        except ValueError:
            continue
        if filing_date >= cutoff:
            ptrs.append(e)
    ptrs.sort(key=lambda e: datetime.strptime(e["FilingDate"], "%m/%d/%Y"), reverse=True)
    return ptrs


def fetch_ptr_pdf(doc_id: str, year: int | None = None) -> bytes | None:
    """Return PDF bytes. Cached permanently — DocIDs are immutable."""
    _ensure_cache_dirs()
    year = year or date.today().year
    p = PDF_CACHE / f"{year}_{doc_id}.pdf"
    if p.exists() and p.stat().st_size > 0:
        return p.read_bytes()

    url = f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
    resp = _http_get(url)
    if resp is None or len(resp.content) < 100:
        return None
    p.write_bytes(resp.content)
    return resp.content


def parse_ptr_pdf(pdf_bytes: bytes, member_name: str, state_district: str,
                  filing_date: str, doc_id: str) -> list[CongressTrade]:
    """Extract trades from a PTR PDF. Best-effort — skip unparseable lines."""
    trades: list[CongressTrade] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        print(f"[congress] PDF parse failed for {doc_id}: {e}")
        return trades

    # Strategy: split on tickers — each "(TICKER)" anchors a transaction block.
    # Then within the surrounding text, extract transaction type and date.
    matches = list(TICKER_PATTERN.finditer(text))
    for i, m in enumerate(matches):
        ticker = m.group(1)
        # Skip common false positives (state codes, asset type codes already in brackets)
        if ticker in {"PS", "ST", "CT", "RP", "OL", "ET", "MA", "OP", "EQ", "GS"}:
            continue
        # Window: this ticker to the next ticker (or end of text)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else min(start + 800, len(text))
        block = text[start:end]

        # Extract transaction type + date
        tx_match = TRANSACTION_TYPE_PATTERN.search(text[max(0, m.start() - 100): end])
        if not tx_match:
            continue
        tx_code = tx_match.group(1)
        tx_date = tx_match.group(2)

        if tx_code in PURCHASE_CODES:
            tx_type = "purchase"
        elif tx_code in SALE_CODES:
            tx_type = "sale"
        elif tx_code in EXCHANGE_CODES:
            tx_type = "exchange"
        else:
            continue

        # Amount range
        amount_match = AMOUNT_PATTERN.search(block[:300])
        amount = amount_match.group(0).strip() if amount_match else ""

        # Owner — appears before the asset usually. Default to "self".
        owner = "self"
        # Look in the 100 chars before the ticker for SP, DC, JT markers
        pre_text = text[max(0, m.start() - 200): m.start()].upper()
        if " SP " in pre_text or pre_text.endswith(" SP"):
            owner = "spouse"
        elif " DC " in pre_text or pre_text.endswith(" DC"):
            owner = "dependent_child"
        elif " JT " in pre_text or pre_text.endswith(" JT"):
            owner = "joint"

        # Asset description — text immediately before the ticker on its line
        line_start = text.rfind("\n", 0, m.start()) + 1
        asset_desc = text[line_start:m.start()].strip()[:120]

        trades.append(CongressTrade(
            member_name=member_name,
            state_district=state_district,
            owner=owner,
            ticker=ticker,
            asset_desc=asset_desc,
            transaction_type=tx_type,
            transaction_date=tx_date,
            amount_range=amount,
            filing_date=filing_date,
            doc_id=doc_id,
        ))
    return trades


def recent_congressional_trades(symbols: list[str], days: int = 14) -> dict[str, list[dict]]:
    """Top-level: for each symbol in `symbols`, return list of recent congressional trades.

    Only PURCHASES and SALES (skips Exchange/corporate actions). Looks at PTRs
    filed in the last `days` days.
    """
    universe = set(symbols)
    ptrs = recent_ptrs(days=days)
    print(f"[congress] {len(ptrs)} PTRs filed in last {days} days")

    by_symbol: dict[str, list[dict]] = {s: [] for s in symbols}
    n_pdfs_fetched = 0
    n_trades_parsed = 0

    for ptr in ptrs:
        doc_id = ptr.get("DocID", "")
        if not doc_id:
            continue
        first = ptr.get("First", "")
        last = ptr.get("Last", "")
        member_name = f"{first} {last}".strip()
        state_district = f"{ptr.get('StateDst', '')}{ptr.get('District', '')}".strip()
        filing_date = ptr.get("FilingDate", "")

        pdf_bytes = fetch_ptr_pdf(doc_id)
        if pdf_bytes is None:
            continue
        n_pdfs_fetched += 1

        trades = parse_ptr_pdf(pdf_bytes, member_name, state_district, filing_date, doc_id)
        for t in trades:
            n_trades_parsed += 1
            if t.ticker in universe and t.transaction_type in ("purchase", "sale"):
                by_symbol[t.ticker].append(asdict(t))

    print(f"[congress] parsed {n_trades_parsed} trades from {n_pdfs_fetched} PDFs")
    hit_symbols = [s for s, ts in by_symbol.items() if ts]
    if hit_symbols:
        print(f"[congress] universe matches: {hit_symbols}")
    return by_symbol


def summarize_congress(trades: list[dict]) -> dict:
    """Reduce a symbol's recent congressional trades to a summary."""
    if not trades:
        return {"n_trades": 0, "n_buys": 0, "n_sells": 0, "buyer_names": [], "seller_names": []}
    buys = [t for t in trades if t["transaction_type"] == "purchase"]
    sells = [t for t in trades if t["transaction_type"] == "sale"]
    return {
        "n_trades": len(trades),
        "n_buys": len(buys),
        "n_sells": len(sells),
        "buyer_names": list({t["member_name"] for t in buys})[:5],
        "seller_names": list({t["member_name"] for t in sells})[:5],
        "cluster_buy": len({t["member_name"] for t in buys}) >= 2,  # 2+ separate members = cluster
    }
