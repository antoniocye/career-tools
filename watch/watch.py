#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from email.message import Message
import gzip
import hashlib
from http import cookiejar
import json
import re
import subprocess
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlsplit, urlunsplit


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
DATA_DIR = ROOT_DIR / "data"
DEFAULT_DB_PATH = DATA_DIR / "companies_watch_db.json"
DEFAULT_STATE_PATH = DATA_DIR / "watch_state.json"
DEFAULT_INSPIRATION_DB_PATH = DATA_DIR / "inspiration_people_db.json"
DEFAULT_INSPIRATION_STATE_PATH = DATA_DIR / "inspiration_state.json"
DEFAULT_RESUME_PATH = ROOT_DIR.parent / "resume_latestV0.pdf"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
ALLOWED_HASH_MODES = {"auto", "raw", "text"}
WORD_PATTERN = re.compile(r"[a-z][a-z'-]{5,}")
HASH_WORD_PATTERN = re.compile(r"[a-z][a-z0-9+\-]{5,}")
_VOWELS = frozenset("aeiou")
# Letter pairs that do not appear in English words; tokens containing any of
# these are generated tokens (CSS module hashes, JS bundle IDs, etc.)
_BAD_BIGRAMS = frozenset({
    "bx", "bz",
    "cf", "cg",
    "df", "dq", "dx", "dz",
    "fx",
    "gc", "gd", "gf", "gj", "gk", "gp", "gv", "gx", "gz",
    "hg", "hj", "hk", "hq", "hx", "hz",
    "jb", "jc", "jd", "jf", "jg", "jh", "jj", "jk", "jl", "jm",
    "jn", "jp", "jq", "jr", "js", "jt", "jv", "jw", "jx", "jy", "jz",
    "kf", "kj", "kq", "kv", "kx", "kz",
    "mf", "mg", "mj", "mk", "mq", "mv", "mx", "mz",
    "nq",
    "pj", "pk", "pq", "px", "pz",
    "qa", "qb", "qc", "qd", "qe", "qf", "qg", "qh", "qi",
    "qj", "qk", "ql", "qm", "qn", "qo", "qp", "qq", "qr",
    "qs", "qt", "qv", "qw", "qx", "qy", "qz",
    "tj", "tk", "tn", "tq", "tv", "tx",
    "sv",
    "uu",
    "vb", "vc", "vd", "vf", "vg", "vh", "vj", "vk", "vm",
    "vn", "vp", "vq", "vr", "vs", "vt", "vv", "vw", "vx", "vy", "vz",
    "wc", "wf", "wg", "wj", "wm", "wp", "wq", "wu", "wx", "wz",
    "xb", "xc", "xd", "xf", "xg", "xh", "xj", "xk", "xl",
    "xm", "xn", "xq", "xr", "xv", "xw", "xx", "xy", "xz",
    "yg", "yh", "yj", "yk", "ym", "yq", "yv", "yw", "yx", "yy", "yz",
    "zb", "zc", "zd", "zf", "zg", "zh", "zj", "zk", "zl", "zm",
    "zn", "zp", "zq", "zr", "zs", "zt", "zv", "zw", "zx", "zy", "zz",
})
STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "your",
    "you",
    "our",
    "are",
    "was",
    "were",
    "will",
    "have",
    "has",
    "had",
    "not",
    "but",
    "can",
    "all",
    "any",
    "new",
    "job",
    "jobs",
    "role",
    "roles",
    "team",
    "work",
    "about",
    "into",
    "out",
    "who",
    "what",
    "when",
    "where",
    "why",
    "how",
    "their",
    "them",
    "they",
    "there",
    "here",
    "more",
    "less",
    "than",
    "also",
    "over",
    "under",
    "between",
    "through",
    "each",
    "per",
    "via",
    "one",
    "two",
    "three",
}
VOLATILE_TEXT_PATTERNS = [
    r"\b\d{4}-\d{2}-\d{2}\b",
    r"\b\d{1,2}:\d{2}(?::\d{2})?\b",
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+\d{4}\b",
    r"\bupdated\s+\d+\s+(?:second|minute|hour|day|week|month|year)s?\s+ago\b",
    r"\b[a-f0-9]{8,}\b",
]
PROTECTION_RETRY_HTTP_CODES = frozenset({403, 406, 429, 503})


try:
    import brotli as _brotli  # noqa: F401
except Exception:
    _brotli = None


@dataclass
class TargetResult:
    company: str
    url: str
    label: str | None
    status: str
    reason: str
    changed: bool | None
    hash_value: str | None
    added_words: list[str] | None


@dataclass
class FetchPayload:
    url: str
    status_code: int
    body: bytes
    content_type: str | None
    etag: str | None
    last_modified: str | None
    fetch_path: str


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def utc_today() -> str:
    return datetime.now(tz=timezone.utc).date().isoformat()


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
        f.write("\n")


class _VisibleTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "template", "svg", "canvas"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "template", "svg", "canvas"}:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            text = " ".join(data.split())
            if text:
                self._parts.append(text)

    def text(self) -> str:
        return " ".join(self._parts)


def normalize_text_for_hash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def decode_text_body(body: bytes, content_type: str | None) -> str:
    charset = None
    if content_type:
        match = re.search(r"charset=([^\s;]+)", content_type, flags=re.IGNORECASE)
        if match:
            charset = match.group(1).strip().strip('"').strip("'")

    attempts = [enc for enc in [charset, "utf-8", "latin-1"] if enc]
    for enc in attempts:
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def looks_like_html(content_type: str | None, body: bytes) -> bool:
    if content_type and "text/html" in content_type.lower():
        return True
    head = body[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def looks_like_textual_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    ctype = content_type.lower()
    return (
        ctype.startswith("text/")
        or "json" in ctype
        or "xml" in ctype
        or "javascript" in ctype
    )


def sanitize_volatile_text(text: str) -> str:
    clean = text.lower()
    for pattern in VOLATILE_TEXT_PATTERNS:
        clean = re.sub(pattern, " ", clean, flags=re.IGNORECASE)
    return clean


def _token_looks_real(token: str) -> bool:
    if not any(c in _VOWELS for c in token):
        return False
    letters = re.sub(r"[^a-z]", "", token)
    return not any(letters[i:i+2] in _BAD_BIGRAMS for i in range(len(letters) - 1))


def tokenize_for_hash(text: str) -> list[str]:
    clean = sanitize_volatile_text(text)
    tokens = HASH_WORD_PATTERN.findall(clean)
    filtered: list[str] = []
    for token in tokens:
        if token in STOPWORDS:
            continue
        if any(ch.isdigit() for ch in token):
            continue
        if not _token_looks_real(token):
            continue
        filtered.append(token)
    return filtered


def build_html_token_fingerprint(text: str) -> str:
    tokens = set(tokenize_for_hash(text))
    if not tokens:
        return ""
    return "\n".join(sorted(tokens))


def hash_payload(
    body: bytes,
    content_type: str | None,
    hash_mode: str,
) -> tuple[str, str, str, str | None]:
    raw_digest = hashlib.sha256(body).hexdigest()

    if hash_mode == "raw":
        return raw_digest, raw_digest, "raw-bytes", None

    text_body = decode_text_body(body, content_type)
    normalized_text = normalize_text_for_hash(text_body)

    if hash_mode == "text":
        digest = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
        text_for_diff = normalized_text if normalized_text else None
        return digest, raw_digest, "normalized-text", text_for_diff

    if looks_like_html(content_type, body):
        extractor = _VisibleTextExtractor()
        extractor.feed(text_body)
        extractor.close()
        visible_text = normalize_text_for_hash(extractor.text())
        if visible_text:
            fingerprint = build_html_token_fingerprint(visible_text)
            if fingerprint:
                digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
                return digest, raw_digest, "html-token-fingerprint", visible_text
            digest = hashlib.sha256(sanitize_volatile_text(visible_text).encode("utf-8")).hexdigest()
            return digest, raw_digest, "html-visible-text", visible_text

    if looks_like_textual_content_type(content_type):
        digest = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
        text_for_diff = normalized_text if normalized_text else None
        return digest, raw_digest, "normalized-text", text_for_diff

    return raw_digest, raw_digest, "raw-bytes", None


def tokenize_for_diff(text: str) -> list[str]:
    tokens = WORD_PATTERN.findall(text.lower())
    filtered: list[str] = []
    for token in tokens:
        if token in STOPWORDS:
            continue
        if any(ch.isdigit() for ch in token):
            continue
        if not _token_looks_real(token):
            continue
        filtered.append(token)
    return filtered


def token_set_from_record(prev_record: dict[str, Any] | None) -> set[str] | None:
    if not prev_record:
        return None
    raw_tokens = prev_record.get("tokens")
    if not isinstance(raw_tokens, list):
        return None
    tokens: set[str] = set()
    for item in raw_tokens:
        token = str(item).strip().lower()
        if token:
            tokens.add(token)
    return tokens


def compute_added_words(
    text_for_diff: str,
    prev_tokens: set[str] | None,
    limit: int,
) -> tuple[list[str], list[str]]:
    token_counts = Counter(tokenize_for_diff(text_for_diff))
    current_tokens = set(token_counts.keys())
    if not current_tokens:
        return [], []

    added_words: list[str] = []
    if prev_tokens is not None:
        added_candidates = current_tokens - prev_tokens
        added_words = sorted(
            added_candidates,
            key=lambda word: (-token_counts[word], word),
        )[:limit]

    return added_words, sorted(current_tokens)


def normalize_db(db: dict[str, Any]) -> dict[str, Any]:
    companies = db.get("companies", [])
    if not isinstance(companies, list):
        raise ValueError("'companies' must be a list")

    normalized: list[dict[str, Any]] = []
    for row in companies:
        if not isinstance(row, dict):
            raise ValueError("Each company entry must be an object")
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Each company must have a non-empty 'name'")
        targets = row.get("targets", [])
        if not isinstance(targets, list):
            raise ValueError(f"'targets' for '{name}' must be a list")
        status_updates = row.get("status_updates", [])
        if not isinstance(status_updates, list):
            raise ValueError(f"'status_updates' for '{name}' must be a list")

        clean_targets: list[dict[str, Any]] = []
        for t in targets:
            if isinstance(t, str):
                url = t.strip()
                label = None
                hash_mode = "auto"
            elif isinstance(t, dict):
                url = str(t.get("url", "")).strip()
                raw_label = t.get("label")
                label = str(raw_label).strip() if raw_label else None
                raw_hash_mode = str(t.get("hash_mode", "auto")).strip().lower()
                hash_mode = raw_hash_mode if raw_hash_mode else "auto"
            else:
                raise ValueError(f"Invalid target format for company '{name}'")

            if not url:
                raise ValueError(f"Empty url in targets for company '{name}'")
            if hash_mode not in ALLOWED_HASH_MODES:
                raise ValueError(
                    f"Invalid hash_mode '{hash_mode}' for company '{name}' target '{url}'"
                )
            clean_targets.append({"url": url, "label": label, "hash_mode": hash_mode})

        clean_status_updates: list[dict[str, str]] = []
        for entry in status_updates:
            if not isinstance(entry, dict):
                raise ValueError(
                    f"Each status update for '{name}' must be an object with date/text"
                )
            raw_text = entry.get("text")
            text = str(raw_text).strip() if raw_text is not None else ""
            if not text:
                raise ValueError(f"Status update text cannot be empty for '{name}'")

            raw_date = entry.get("date")
            date = str(raw_date).strip() if raw_date is not None else ""
            raw_created_at = entry.get("created_at")
            created_at = (
                str(raw_created_at).strip() if raw_created_at is not None else ""
            )

            if not date:
                if created_at and len(created_at) >= 10:
                    date = created_at[:10]
                else:
                    date = "unknown"

            clean_status_updates.append(
                {"date": date, "text": text, "created_at": created_at}
            )

        normalized.append(
            {
                "name": name.strip(),
                "targets": clean_targets,
                "status_updates": clean_status_updates,
            }
        )

    return {"companies": normalized}


def get_company(db: dict[str, Any], company_name: str) -> dict[str, Any] | None:
    for company in db["companies"]:
        if company["name"].lower() == company_name.lower():
            return company
    return None


def ensure_initialized(db_path: Path, state_path: Path) -> None:
    if not db_path.exists():
        save_json(db_path, {"companies": []})
    if not state_path.exists():
        save_json(state_path, {"targets": {}})


def cmd_init(args: argparse.Namespace) -> int:
    db_path: Path = args.db
    state_path: Path = args.state

    if db_path.exists() and not args.force:
        print(f"DB already exists: {db_path}")
    else:
        save_json(db_path, {"companies": []})
        print(f"Created: {db_path}")

    if state_path.exists() and not args.force:
        print(f"State already exists: {state_path}")
    else:
        save_json(state_path, {"targets": {}})
        print(f"Created: {state_path}")

    return 0


def cmd_list(args: argparse.Namespace) -> int:
    db = normalize_db(load_json(args.db, {"companies": []}))
    companies = sorted(db["companies"], key=lambda c: c["name"].casefold())

    if not companies:
        print("No companies configured.")
        return 0

    print(f"Companies: {len(companies)}")

    for idx, company in enumerate(companies, start=1):
        print()
        print(f"{idx:>2}. {company['name']}")

        targets = sorted(
            company["targets"],
            key=lambda t: (
                (t.get("label") or "").casefold(),
                t["url"].casefold(),
            ),
        )
        if not targets:
            print("    (no targets)")
        else:
            for t_idx, target in enumerate(targets, start=1):
                label = f" [{target['label']}]" if target["label"] else ""
                hash_mode = target.get("hash_mode", "auto")
                mode_text = "" if hash_mode == "auto" else f" (mode={hash_mode})"
                print(f"    {t_idx:>2}) {target['url']}{label}{mode_text}")

        status_updates = sorted(
            company.get("status_updates", []),
            key=lambda s: (s.get("created_at", ""), s.get("date", "")),
            reverse=True,
        )
        if status_updates:
            print("    Status updates:")
            for update in status_updates:
                print(f"      - {update['date']}: {update['text']}")
    return 0


def cmd_add_company(args: argparse.Namespace) -> int:
    db = normalize_db(load_json(args.db, {"companies": []}))
    name = args.name.strip()
    company = get_company(db, name)

    if company is None:
        company = {"name": name, "targets": [], "status_updates": []}
        db["companies"].append(company)
    elif not args.urls and not getattr(args, "github_org", None):
        print(f"Company already exists: {name}")
        return 0

    if getattr(args, "github_org", None):
        company["github_org"] = args.github_org.lstrip("@")

    added_targets = 0
    for url in args.urls:
        if target_exists(company, url):
            continue
        company["targets"].append(
            {"url": url, "label": None, "hash_mode": args.hash_mode}
        )
        added_targets += 1

    save_json(args.db, db)
    if added_targets:
        print(
            f"Added/updated company: {name} ({added_targets} new target(s), mode={args.hash_mode})"
        )
    else:
        print(f"Added company: {name}")

    return 0


def cmd_remove_company(args: argparse.Namespace) -> int:
    db = normalize_db(load_json(args.db, {"companies": []}))
    before = len(db["companies"])
    db["companies"] = [
        c for c in db["companies"] if c["name"].lower() != args.name.lower()
    ]
    if len(db["companies"]) == before:
        print(f"Company not found: {args.name}")
        return 1
    save_json(args.db, db)
    print(f"Removed company: {args.name}")
    return 0


def target_exists(company: dict[str, Any], url: str) -> bool:
    return any(t["url"] == url for t in company["targets"])


def cmd_add_status(args: argparse.Namespace) -> int:
    db = normalize_db(load_json(args.db, {"companies": []}))
    company = get_company(db, args.company)
    if not company:
        print(f"Company not found: {args.company}")
        return 1

    text = " ".join(args.text).strip()
    if not text:
        print("Status text cannot be empty.")
        return 1

    company["status_updates"].append(
        {"date": utc_today(), "text": text, "created_at": utc_now()}
    )
    save_json(args.db, db)
    print(f"Added status update for {company['name']} ({utc_today()}).")
    return 0


def cmd_add_target(args: argparse.Namespace) -> int:
    db = normalize_db(load_json(args.db, {"companies": []}))
    company = get_company(db, args.company)
    if not company:
        company = {"name": args.company, "targets": [], "status_updates": []}
        db["companies"].append(company)

    if target_exists(company, args.url):
        print(f"Target already exists for {company['name']}: {args.url}")
        return 0

    company["targets"].append(
        {"url": args.url, "label": args.label, "hash_mode": args.hash_mode}
    )
    save_json(args.db, db)
    print(
        f"Added target for {company['name']}: {args.url} (mode={args.hash_mode})"
    )
    return 0


def cmd_remove_target(args: argparse.Namespace) -> int:
    db = normalize_db(load_json(args.db, {"companies": []}))
    company = get_company(db, args.company)
    if not company:
        print(f"Company not found: {args.company}")
        return 1

    before = len(company["targets"])
    company["targets"] = [t for t in company["targets"] if t["url"] != args.url]
    if len(company["targets"]) == before:
        print(f"Target not found for {company['name']}: {args.url}")
        return 1

    save_json(args.db, db)
    print(f"Removed target for {company['name']}: {args.url}")
    return 0


def cmd_set_targets(args: argparse.Namespace) -> int:
    db = normalize_db(load_json(args.db, {"companies": []}))
    company = get_company(db, args.company)
    if not company:
        company = {"name": args.company, "targets": [], "status_updates": []}
        db["companies"].append(company)

    company["targets"] = [
        {"url": u, "label": None, "hash_mode": args.hash_mode} for u in args.urls
    ]
    save_json(args.db, db)
    print(
        f"Updated targets for {company['name']} ({len(args.urls)} target(s), mode={args.hash_mode})."
    )
    return 0


def state_key(company: str, url: str) -> str:
    return f"{company}::{url}"


def _accept_encoding_header() -> str:
    if _brotli is None:
        return "gzip, deflate"
    return "gzip, deflate, br"


def _decode_response_body(raw_body: bytes, content_encoding: str | None) -> bytes:
    encoding = (content_encoding or "").split(",", 1)[0].strip().lower()
    if not encoding:
        return raw_body
    if encoding == "gzip":
        try:
            return gzip.decompress(raw_body)
        except Exception:
            return raw_body
    if encoding in {"deflate", "zlib"}:
        try:
            return zlib.decompress(raw_body)
        except zlib.error:
            try:
                return zlib.decompress(raw_body, -zlib.MAX_WBITS)
            except Exception:
                return raw_body
    if encoding == "br" and _brotli is not None:
        try:
            return _brotli.decompress(raw_body)
        except Exception:
            return raw_body
    return raw_body


def _header_get_insensitive(headers: dict[str, str], key: str) -> str | None:
    needle = key.lower()
    for header_key, value in headers.items():
        if header_key.lower() == needle:
            return value
    return None


def _is_timeout_exception(exc: Exception) -> bool:
    return "timed out" in str(exc).lower()


def _parse_retry_after(headers: Any) -> int:
    if headers is None:
        return 5
    retry_after: str | None = None
    try:
        retry_after = headers.get("Retry-After")
    except Exception:
        retry_after = None
    if not retry_after:
        return 5
    try:
        value = int(str(retry_after).strip())
        return max(value, 1)
    except Exception:
        return 5


def _build_conditional_headers(prev_record: dict[str, Any] | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if not prev_record:
        return headers
    etag = prev_record.get("etag")
    last_modified = prev_record.get("last_modified")
    if etag:
        headers["If-None-Match"] = str(etag)
    if last_modified:
        headers["If-Modified-Since"] = str(last_modified)
    return headers


def _origin_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _canonical_url_candidates(url: str) -> list[str]:
    candidates = [url]
    parts = urlsplit(url)
    path = parts.path
    if path and not path.endswith("/"):
        tail = path.rsplit("/", 1)[-1]
        if "." not in tail:
            candidate = urlunsplit(
                (parts.scheme, parts.netloc, f"{path}/", parts.query, parts.fragment)
            )
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _build_browser_headers(
    url: str,
    accept_encoding: str,
    *,
    referer: str | None = None,
) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": accept_encoding,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
    }
    origin = _origin_url(url)
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = origin
        headers["Sec-Fetch-Site"] = "same-origin"
    else:
        headers["Sec-Fetch-Site"] = "none"
    return headers


def _fetch_urllib(
    *,
    url: str,
    timeout: float,
    headers: dict[str, str],
    conditional_headers: dict[str, str],
    opener: Any = None,
    fetch_path: str = "urllib",
) -> FetchPayload:
    req_headers = dict(headers)
    req_headers.update(conditional_headers)
    req = request.Request(url, headers=req_headers, method="GET")
    open_fn = opener.open if opener is not None else request.urlopen
    with open_fn(req, timeout=timeout) as response:
        status_code_raw = getattr(response, "status", None)
        status_code = int(status_code_raw) if isinstance(status_code_raw, int) else 200
        raw_body = response.read()
        body = _decode_response_body(raw_body, response.headers.get("Content-Encoding", ""))
        response_url = response.geturl() if hasattr(response, "geturl") else url
        return FetchPayload(
            url=response_url,
            status_code=status_code,
            body=body,
            content_type=response.headers.get("Content-Type"),
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            fetch_path=fetch_path,
        )


def _fetch_with_session_bootstrap(
    *,
    url: str,
    timeout: float,
    accept_encoding: str,
    conditional_headers: dict[str, str],
) -> FetchPayload:
    origin = _origin_url(url)
    referer = f"{origin}/"
    jar = cookiejar.CookieJar()
    opener = request.build_opener(request.HTTPCookieProcessor(jar))

    parts = urlsplit(url)
    warmup_urls: list[str] = [referer]
    page_without_query = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))
    if page_without_query not in warmup_urls:
        warmup_urls.append(page_without_query)
    if parts.path and not parts.path.endswith("/"):
        tail = parts.path.rsplit("/", 1)[-1]
        if "." not in tail:
            warmup_with_slash = urlunsplit(
                (parts.scheme, parts.netloc, f"{parts.path}/", "", "")
            )
            if warmup_with_slash not in warmup_urls:
                warmup_urls.append(warmup_with_slash)

    warmup_timeout = max(3.0, min(timeout, 10.0))
    warmup_headers = _build_browser_headers(url, accept_encoding, referer=referer)
    for warmup_url in warmup_urls:
        try:
            _fetch_urllib(
                url=warmup_url,
                timeout=warmup_timeout,
                headers=warmup_headers,
                conditional_headers={},
                opener=opener,
                fetch_path="session-warmup",
            )
        except Exception:
            continue

    return _fetch_urllib(
        url=url,
        timeout=timeout,
        headers=_build_browser_headers(url, accept_encoding, referer=referer),
        conditional_headers=conditional_headers,
        opener=opener,
        fetch_path="urllib+session",
    )


def _parse_curl_header_dump(raw_headers: str) -> tuple[int, dict[str, str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in raw_headers.splitlines():
        stripped = line.rstrip("\r")
        if not stripped:
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(stripped)
    if current:
        blocks.append(current)

    for block in reversed(blocks):
        if not block:
            continue
        status_line = block[0].strip()
        match = re.match(r"HTTP/\S+\s+(\d{3})\b", status_line, flags=re.IGNORECASE)
        if not match:
            continue
        status_code = int(match.group(1))
        headers: dict[str, str] = {}
        for line in block[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip()] = value.strip()
        return status_code, headers
    raise RuntimeError("Unable to parse response headers from curl output")


def _message_from_headers(headers: dict[str, str]) -> Message:
    msg = Message()
    for key, value in headers.items():
        msg[key] = value
    return msg


def _fetch_with_curl(
    *,
    url: str,
    timeout: float,
    headers: dict[str, str],
    conditional_headers: dict[str, str],
) -> FetchPayload:
    timeout_value = max(1.0, float(timeout))
    with tempfile.TemporaryDirectory(prefix="career-watch-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        headers_path = tmp_root / "headers.txt"
        body_path = tmp_root / "body.bin"

        cmd = [
            "curl",
            "--silent",
            "--show-error",
            "--location",
            "--compressed",
            "--max-time",
            f"{timeout_value:.1f}",
            "--connect-timeout",
            f"{min(timeout_value, 15.0):.1f}",
            "--dump-header",
            str(headers_path),
            "--output",
            str(body_path),
        ]
        for key, value in headers.items():
            cmd.extend(["--header", f"{key}: {value}"])
        for key, value in conditional_headers.items():
            cmd.extend(["--header", f"{key}: {value}"])
        cmd.append(url)

        completed = subprocess.run(cmd, capture_output=True, text=True)
        if completed.returncode != 0:
            detail = (
                completed.stderr.strip()
                or completed.stdout.strip()
                or f"curl exited with code {completed.returncode}"
            )
            raise RuntimeError(detail)

        raw_headers = headers_path.read_text(encoding="latin-1", errors="replace")
        status_code, parsed_headers = _parse_curl_header_dump(raw_headers)
        body = body_path.read_bytes()
        body = _decode_response_body(body, _header_get_insensitive(parsed_headers, "Content-Encoding"))

    if status_code == 304:
        raise error.HTTPError(
            url,
            304,
            "Not Modified",
            _message_from_headers(parsed_headers),
            None,
        )
    if status_code >= 400:
        raise error.HTTPError(
            url,
            status_code,
            f"HTTP error {status_code}",
            _message_from_headers(parsed_headers),
            None,
        )
    return FetchPayload(
        url=url,
        status_code=status_code,
        body=body,
        content_type=_header_get_insensitive(parsed_headers, "Content-Type"),
        etag=_header_get_insensitive(parsed_headers, "ETag"),
        last_modified=_header_get_insensitive(parsed_headers, "Last-Modified"),
        fetch_path="curl-fallback",
    )


def _build_not_modified_result(
    *,
    company: str,
    url: str,
    label: str | None,
    prev_record: dict[str, Any],
) -> tuple[TargetResult, dict[str, Any]]:
    record = dict(prev_record)
    record["checked_at"] = utc_now()
    result = TargetResult(
        company=company,
        url=url,
        label=label,
        status="ok",
        reason="HTTP 304 Not Modified",
        changed=False,
        hash_value=prev_record.get("hash"),
        added_words=None,
    )
    return result, record


def _build_success_result(
    *,
    company: str,
    url: str,
    label: str | None,
    hash_mode: str,
    prev_record: dict[str, Any] | None,
    added_words_limit: int,
    payload: FetchPayload,
) -> tuple[TargetResult, dict[str, Any]]:
    digest, raw_digest, hash_basis, text_for_diff = hash_payload(
        body=payload.body,
        content_type=payload.content_type,
        hash_mode=hash_mode,
    )

    changed = True
    if prev_record:
        prev_hash = prev_record.get("hash")
        prev_basis = prev_record.get("hash_basis")
        # One-time migration: old state used raw hashing only.
        if prev_basis is None and hash_basis != "raw-bytes":
            changed = False
        elif (
            isinstance(prev_basis, str)
            and prev_basis.startswith("html-")
            and hash_basis.startswith("html-")
            and prev_basis != hash_basis
        ):
            changed = False
        elif prev_hash == digest:
            changed = False
        elif prev_hash == raw_digest:
            changed = False

    prev_tokens = token_set_from_record(prev_record)
    added_words: list[str] | None = None
    token_snapshot: list[str] | None = None
    if text_for_diff:
        words, token_snapshot = compute_added_words(
            text_for_diff=text_for_diff,
            prev_tokens=prev_tokens,
            limit=max(0, added_words_limit),
        )
        if prev_tokens is not None:
            added_words = words

    record: dict[str, Any] = {
        "company": company,
        "url": url,
        "fetched_url": payload.url,
        "label": label,
        "hash": digest,
        "raw_hash": raw_digest,
        "hash_basis": hash_basis,
        "hash_mode": hash_mode,
        "etag": payload.etag,
        "last_modified": payload.last_modified,
        "content_type": payload.content_type,
        "status_code": int(payload.status_code),
        "fetch_path": payload.fetch_path,
        "checked_at": utc_now(),
    }
    if token_snapshot is not None:
        record["tokens"] = token_snapshot

    reason = f"HTTP {payload.status_code} ({hash_basis}, via {payload.fetch_path})"
    result = TargetResult(
        company=company,
        url=url,
        label=label,
        status="ok",
        reason=reason,
        changed=changed,
        hash_value=digest,
        added_words=added_words,
    )
    return result, record


def fetch_target(
    company: str,
    target: dict[str, Any],
    prev_record: dict[str, Any] | None,
    timeout: float,
    added_words_limit: int,
    *,
    enable_session_bootstrap: bool,
    enable_curl_fallback: bool,
) -> tuple[TargetResult, dict[str, Any] | None]:
    url = target["url"]
    label = target.get("label")
    hash_mode = target.get("hash_mode", "auto")
    accept_encoding = _accept_encoding_header()
    conditional_headers = _build_conditional_headers(prev_record)
    url_candidates = _canonical_url_candidates(url)
    current_timeout = timeout
    last_http_error: error.HTTPError | None = None
    last_exception: Exception | None = None

    for attempt in range(2):
        retry_due_to_timeout = False
        retry_due_to_backoff = False

        for candidate_url in url_candidates:
            base_headers = _build_browser_headers(candidate_url, accept_encoding)
            try:
                payload = _fetch_urllib(
                    url=candidate_url,
                    timeout=current_timeout,
                    headers=base_headers,
                    conditional_headers=conditional_headers,
                    fetch_path="urllib",
                )
                return _build_success_result(
                    company=company,
                    url=url,
                    label=label,
                    hash_mode=hash_mode,
                    prev_record=prev_record,
                    added_words_limit=added_words_limit,
                    payload=payload,
                )
            except error.HTTPError as e:
                if e.code == 304 and prev_record:
                    return _build_not_modified_result(
                        company=company,
                        url=url,
                        label=label,
                        prev_record=prev_record,
                    )
                if e.code == 429 and attempt == 0:
                    time.sleep(min(_parse_retry_after(e.headers), 30))
                    retry_due_to_backoff = True
                    break
                last_http_error = e
                if e.code not in PROTECTION_RETRY_HTTP_CODES:
                    continue

                if enable_session_bootstrap:
                    try:
                        payload = _fetch_with_session_bootstrap(
                            url=candidate_url,
                            timeout=current_timeout,
                            accept_encoding=accept_encoding,
                            conditional_headers=conditional_headers,
                        )
                        return _build_success_result(
                            company=company,
                            url=url,
                            label=label,
                            hash_mode=hash_mode,
                            prev_record=prev_record,
                            added_words_limit=added_words_limit,
                            payload=payload,
                        )
                    except error.HTTPError as session_error:
                        if session_error.code == 304 and prev_record:
                            return _build_not_modified_result(
                                company=company,
                                url=url,
                                label=label,
                                prev_record=prev_record,
                            )
                        if session_error.code == 429 and attempt == 0:
                            time.sleep(min(_parse_retry_after(session_error.headers), 30))
                            retry_due_to_backoff = True
                            break
                        last_http_error = session_error
                    except Exception as session_exc:
                        if _is_timeout_exception(session_exc) and attempt == 0:
                            retry_due_to_timeout = True
                            last_exception = session_exc
                            break
                        last_exception = session_exc

                if retry_due_to_backoff or retry_due_to_timeout:
                    break

                if enable_curl_fallback:
                    try:
                        payload = _fetch_with_curl(
                            url=candidate_url,
                            timeout=current_timeout,
                            headers=base_headers,
                            conditional_headers=conditional_headers,
                        )
                        return _build_success_result(
                            company=company,
                            url=url,
                            label=label,
                            hash_mode=hash_mode,
                            prev_record=prev_record,
                            added_words_limit=added_words_limit,
                            payload=payload,
                        )
                    except error.HTTPError as curl_error:
                        if curl_error.code == 304 and prev_record:
                            return _build_not_modified_result(
                                company=company,
                                url=url,
                                label=label,
                                prev_record=prev_record,
                            )
                        if curl_error.code == 429 and attempt == 0:
                            time.sleep(min(_parse_retry_after(curl_error.headers), 30))
                            retry_due_to_backoff = True
                            break
                        last_http_error = curl_error
                    except Exception as curl_exc:
                        if _is_timeout_exception(curl_exc) and attempt == 0:
                            retry_due_to_timeout = True
                            last_exception = curl_exc
                            break
                        last_exception = curl_exc
            except Exception as e:
                if _is_timeout_exception(e) and attempt == 0:
                    retry_due_to_timeout = True
                    last_exception = e
                    break
                last_exception = e

        if retry_due_to_backoff:
            continue
        if retry_due_to_timeout and attempt == 0:
            current_timeout = timeout * 2
            time.sleep(2)
            continue
        break

    if last_http_error is not None:
        return (
            TargetResult(
                company=company,
                url=url,
                label=label,
                status="error",
                reason=f"HTTP error {last_http_error.code}",
                changed=None,
                hash_value=None,
                added_words=None,
            ),
            None,
        )
    if last_exception is not None:
        return (
            TargetResult(
                company=company,
                url=url,
                label=label,
                status="error",
                reason=str(last_exception),
                changed=None,
                hash_value=None,
                added_words=None,
            ),
            None,
        )
    return (
        TargetResult(
            company=company,
            url=url,
            label=label,
            status="error",
            reason="Unknown fetch error",
            changed=None,
            hash_value=None,
            added_words=None,
        ),
        None,
    )


def cmd_watch(args: argparse.Namespace) -> int:
    db = normalize_db(load_json(args.db, {"companies": []}))
    state = load_json(args.state, {"targets": {}})
    if "targets" not in state or not isinstance(state["targets"], dict):
        state = {"targets": {}}

    companies = db["companies"]
    if args.company:
        filtered = [c for c in companies if c["name"].lower() == args.company.lower()]
        if not filtered:
            print(f"Company not found: {args.company}")
            return 1
        companies = filtered

    all_targets: list[tuple[str, dict[str, Any]]] = []
    for c in companies:
        for t in c["targets"]:
            all_targets.append((c["name"], t))

    if not all_targets:
        print("No targets configured.")
        return 0

    results: list[TargetResult] = []
    new_targets_state: dict[str, Any] = dict(state["targets"])

    for company, target in all_targets:
        key = state_key(company, target["url"])
        prev_record = new_targets_state.get(key)
        result, record = fetch_target(
            company=company,
            target=target,
            prev_record=prev_record,
            timeout=args.timeout,
            added_words_limit=args.added_words,
            enable_session_bootstrap=not args.no_session_bootstrap,
            enable_curl_fallback=not args.no_curl_fallback,
        )
        results.append(result)
        if record is not None:
            new_targets_state[key] = record

    state["targets"] = new_targets_state
    save_json(args.state, state)

    changed_count = 0
    unchanged_count = 0
    error_count = 0

    sorted_results = sorted(
        results,
        key=lambda r: (r.company.casefold(), (r.label or "").casefold(), r.url.casefold()),
    )

    unchanged_results = [
        r for r in sorted_results if r.status != "error" and not bool(r.changed)
    ]
    changed_results = [r for r in sorted_results if r.status != "error" and bool(r.changed)]
    error_results = [r for r in sorted_results if r.status == "error"]

    unchanged_count = len(unchanged_results)
    changed_count = len(changed_results)
    error_count = len(error_results)

    print(f"Watch results: {len(results)} target(s)")

    if unchanged_results:
        print()
        print(f"UNCHANGED ({unchanged_count})")
        print("-" * 80)
        for idx, r in enumerate(unchanged_results, start=1):
            label = f" [{r.label}]" if r.label else ""
            print(f"{idx:>2}. {r.company}{label}")
            print(f"    {r.url}")
            print(f"    {r.reason}")

    if error_results:
        print()
        print(f"ERRORS ({error_count})")
        print("-" * 80)
        for idx, r in enumerate(error_results, start=1):
            label = f" [{r.label}]" if r.label else ""
            print(f"{idx:>2}. {r.company}{label}")
            print(f"    {r.url}")
            print(f"    {r.reason}")

    if changed_results:
        print()
        print(f"CHANGED ({changed_count})")
        print("-" * 80)
        for idx, r in enumerate(changed_results, start=1):
            label = f" [{r.label}]" if r.label else ""
            print(f"{idx:>2}. {r.company}{label}")
            print(f"    {r.url}")
            print(f"    {r.reason}")
            if args.added_words > 0 and r.added_words:
                print(f"    + words: {', '.join(r.added_words)}")

    print()
    print(f"Done. changed={changed_count} unchanged={unchanged_count} errors={error_count}")

    if args.fail_on_error and error_count > 0:
        return 2
    return 0


def cmd_find_inspiration(args: argparse.Namespace) -> int:
    from inspiration import find_inspiration

    return find_inspiration(args)


def cmd_list_inspiration(args: argparse.Namespace) -> int:
    from inspiration import list_inspiration_people

    return list_inspiration_people(args)


def cmd_set_inspiration_status(args: argparse.Namespace) -> int:
    from inspiration import set_inspiration_status

    return set_inspiration_status(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="career",
        description="Track webpage changes for a company list using content hashing.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to company target DB (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"Path to state hash file (default: {DEFAULT_STATE_PATH})",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Create DB and state files")
    p_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing DB/state files",
    )
    p_init.set_defaults(func=cmd_init)

    p_list = sub.add_parser("list", help="List companies and targets")
    p_list.set_defaults(func=cmd_list)

    p_add_company = sub.add_parser(
        "add-company",
        help="Add a company, optionally with target URLs",
    )
    p_add_company.add_argument("name")
    p_add_company.add_argument(
        "urls",
        nargs="*",
        help="Optional target URLs to add immediately",
    )
    p_add_company.add_argument(
        "--hash-mode",
        choices=sorted(ALLOWED_HASH_MODES),
        default="auto",
        help="Hashing mode for any URLs provided (default: auto)",
    )
    p_add_company.add_argument(
        "--github-org",
        default=None,
        help="GitHub org slug for direct member lookup (e.g. cloudflare)",
    )
    p_add_company.set_defaults(func=cmd_add_company)

    p_remove_company = sub.add_parser("remove-company", help="Remove a company")
    p_remove_company.add_argument("name")
    p_remove_company.set_defaults(func=cmd_remove_company)

    p_add_status = sub.add_parser("add-status", help="Add a dated status update")
    p_add_status.add_argument("company")
    p_add_status.add_argument(
        "text",
        nargs="+",
        help="Status text. The update date is added automatically.",
    )
    p_add_status.set_defaults(func=cmd_add_status)

    p_add_target = sub.add_parser("add-target", help="Add target URL to a company")
    p_add_target.add_argument("company")
    p_add_target.add_argument("url")
    p_add_target.add_argument("--label", default=None, help="Optional label")
    p_add_target.add_argument(
        "--hash-mode",
        choices=sorted(ALLOWED_HASH_MODES),
        default="auto",
        help="Hashing mode for this target (default: auto)",
    )
    p_add_target.set_defaults(func=cmd_add_target)

    p_remove_target = sub.add_parser(
        "remove-target", help="Remove target URL from a company"
    )
    p_remove_target.add_argument("company")
    p_remove_target.add_argument("url")
    p_remove_target.set_defaults(func=cmd_remove_target)

    p_set_targets = sub.add_parser(
        "set-targets", help="Replace all targets for a company"
    )
    p_set_targets.add_argument("company")
    p_set_targets.add_argument("urls", nargs="+")
    p_set_targets.add_argument(
        "--hash-mode",
        choices=sorted(ALLOWED_HASH_MODES),
        default="auto",
        help="Hashing mode for these targets (default: auto)",
    )
    p_set_targets.set_defaults(func=cmd_set_targets)

    p_watch = sub.add_parser("watch", help="Fetch targets and report changes")
    p_watch.add_argument(
        "--company",
        default=None,
        help="Only watch this company name",
    )
    p_watch.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds",
    )
    p_watch.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Exit with non-zero code if any target fails",
    )
    p_watch.add_argument(
        "--added-words",
        type=int,
        default=8,
        help="Number of newly added words to show for changed pages (0 disables, default: 8)",
    )
    p_watch.add_argument(
        "--no-session-bootstrap",
        action="store_true",
        help="Disable cookie/session bootstrap retry for protected pages (403/406/429/503)",
    )
    p_watch.add_argument(
        "--no-curl-fallback",
        action="store_true",
        help="Disable curl fallback retry for protected pages (403/406/429/503)",
    )
    p_watch.set_defaults(func=cmd_watch)

    p_find_inspiration = sub.add_parser(
        "find-inspiration",
        help="Find high-confidence people relevant to your target roles from company seeds",
    )
    p_find_inspiration.add_argument(
        "--people-db",
        type=Path,
        default=DEFAULT_INSPIRATION_DB_PATH,
        help=f"Path to inspiration people DB (default: {DEFAULT_INSPIRATION_DB_PATH})",
    )
    p_find_inspiration.add_argument(
        "--inspiration-state",
        type=Path,
        default=DEFAULT_INSPIRATION_STATE_PATH,
        help=f"Path to inspiration state file (default: {DEFAULT_INSPIRATION_STATE_PATH})",
    )
    p_find_inspiration.add_argument(
        "--resume",
        type=Path,
        default=DEFAULT_RESUME_PATH,
        help=f"Resume PDF path for keyword inference (default: {DEFAULT_RESUME_PATH})",
    )
    p_find_inspiration.add_argument(
        "--company",
        default=None,
        help="Optional company filter (exact name match)",
    )
    p_find_inspiration.add_argument(
        "--query",
        default=None,
        help="Optional manual query override (still runs one query only)",
    )
    p_find_inspiration.add_argument(
        "--target",
        type=int,
        default=5,
        help="Keep running queries until this many people are added (default: 5)",
    )
    p_find_inspiration.add_argument(
        "--max-results",
        type=int,
        default=20,
        help="Maximum search results to evaluate per query (default: 20)",
    )
    p_find_inspiration.add_argument(
        "--search-timeout",
        type=float,
        default=20.0,
        help="Search request timeout in seconds (default: 20)",
    )
    p_find_inspiration.add_argument(
        "--page-timeout",
        type=float,
        default=20.0,
        help="Candidate page fetch timeout in seconds (default: 20)",
    )
    p_find_inspiration.add_argument(
        "--dry-run",
        action="store_true",
        help="Run evaluation without writing any files",
    )
    p_find_inspiration.add_argument(
        "--debug-skips",
        action="store_true",
        help="Print aggregated reasons for skipped candidates (for threshold tuning)",
    )
    p_find_inspiration.set_defaults(func=cmd_find_inspiration)

    p_inspiration_list = sub.add_parser(
        "inspiration-list",
        help="List people found by find-inspiration",
    )
    p_inspiration_list.add_argument(
        "--people-db",
        type=Path,
        default=DEFAULT_INSPIRATION_DB_PATH,
        help=f"Path to inspiration people DB (default: {DEFAULT_INSPIRATION_DB_PATH})",
    )
    p_inspiration_list.add_argument(
        "--company",
        default=None,
        help="Filter list to a specific company (exact name match)",
    )
    p_inspiration_list.set_defaults(func=cmd_list_inspiration)

    p_inspiration_status = sub.add_parser(
        "inspiration-status",
        help="Set review status for an inspiration person",
    )
    p_inspiration_status.add_argument("person", help="Person id (preferred) or exact full name")
    p_inspiration_status.add_argument(
        "status",
        help="One of: new, relevant, maybe, not_relevant, contacted, ignore",
    )
    p_inspiration_status.add_argument(
        "--people-db",
        type=Path,
        default=DEFAULT_INSPIRATION_DB_PATH,
        help=f"Path to inspiration people DB (default: {DEFAULT_INSPIRATION_DB_PATH})",
    )
    p_inspiration_status.set_defaults(func=cmd_set_inspiration_status)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    args.db = args.db.resolve()
    args.state = args.state.resolve()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    args.state.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(args, "people_db"):
        args.people_db = args.people_db.resolve()
        args.people_db.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(args, "inspiration_state"):
        args.inspiration_state = args.inspiration_state.resolve()
        args.inspiration_state.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(args, "resume"):
        args.resume = args.resume.resolve()
    ensure_initialized(args.db, args.state)

    try:
        return args.func(args)
    except ValueError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
