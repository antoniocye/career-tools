#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib import parse, request

from .resume import ResumeProfile, build_resume_profile
from .search import SearchResult, search_web


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DEFAULT_COMPANY_DB_PATH = DATA_DIR / "companies_watch_db.json"
DEFAULT_PEOPLE_DB_PATH = DATA_DIR / "inspiration_people_db.json"
DEFAULT_STATE_PATH = DATA_DIR / "inspiration_state.json"
DEFAULT_RESUME_PATH = ROOT_DIR.parent / "resume_latestV0.pdf"
USER_AGENT = "career-tools-inspiration/1.0"
REVIEW_STATUSES = {"new", "relevant", "maybe", "not_relevant", "contacted", "ignore"}

BLOCKED_DOMAINS = {
    "linkedin.com",
    "www.linkedin.com",
    "m.linkedin.com",
}

LOW_SIGNAL_PAGE_TERMS = {
    "jobs",
    "careers",
    "job board",
    "hiring",
    "apply",
}
COMPANY_TOKEN_STOPWORDS = {"of", "the", "inc", "labs", "lab", "ai", "co", "llc"}
NON_PERSON_NAME_TOKENS = {
    # Tech/domain terms
    "security", "code", "blog", "jobs", "careers", "openai", "google",
    "microsoft", "internship", "research", "engineer", "engineering",
    "systems", "cryptography", "vulnerability", "scanning", "powered",
    "tool", "guide", "tutorial", "best", "practices",
    # Common English words that appear title-cased in web UI / error text
    "default", "not", "allowed", "required", "enabled", "disabled",
    "available", "unavailable", "active", "inactive", "loading",
    "please", "click", "view", "error", "warning", "notice", "invalid",
    "this", "that", "these", "those", "with", "without", "from", "into",
    "onto", "upon", "about", "above", "below", "under", "over",
    "after", "before", "public", "private", "open", "closed",
    "new", "all", "more", "back", "next", "home", "read", "write",
}

SENIORITY_RULES = {
    "junior": ["intern", "student", "undergraduate", "junior", "new grad", "entry"],
    "senior": [
        "senior",
        "staff",
        "principal",
        "lead",
        "director",
        "head of",
        "professor",
        "founder",
    ],
    "mid": ["engineer", "researcher", "developer", "scientist"],
}


@dataclass
class QuerySpec:
    query_id: str
    company: str
    company_domain: str
    query: str
    github_org: str = ""


@dataclass
class CandidateEvaluation:
    accepted: bool
    score: int
    reasons: list[str]
    person: dict[str, Any] | None
    dedupe_key: str | None
    debug_label: str | None = None
    debug_url: str | None = None


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


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
        f.write("\n")


def ensure_db_files(people_db_path: Path, state_path: Path) -> None:
    if not people_db_path.exists():
        save_json(people_db_path, {"meta": {"version": 1}, "people": []})
    if not state_path.exists():
        save_json(
            state_path,
            {
                "query_cursor": 0,
                "query_history": [],
                "resume_cache": {},
                "last_run": None,
            },
        )


def normalize_company_db(raw_db: dict[str, Any]) -> list[dict[str, Any]]:
    companies = raw_db.get("companies", [])
    if not isinstance(companies, list):
        return []
    normalized: list[dict[str, Any]] = []
    for row in companies:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        targets = row.get("targets", [])
        clean_targets: list[dict[str, str]] = []
        if isinstance(targets, list):
            for t in targets:
                if isinstance(t, dict):
                    url = str(t.get("url", "")).strip()
                else:
                    url = str(t).strip()
                if url:
                    clean_targets.append({"url": url})
        normalized.append({"name": name, "targets": clean_targets})
    return normalized


def base_domain(url: str) -> str:
    try:
        netloc = parse.urlparse(url).netloc.lower()
    except Exception:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def company_domain(company: dict[str, Any]) -> str:
    targets = company.get("targets", [])
    for t in targets:
        d = base_domain(str(t.get("url", "")))
        if d:
            return d
    return ""


def build_query_specs(
    companies: list[dict[str, Any]],
    resume_profile: ResumeProfile,
    company_filter: str | None,
) -> list[QuerySpec]:
    preferred = [
        "cryptography",
        "security",
        "zero-knowledge",
        "systems",
        "rust",
        "research",
        "verification",
        "protocol",
        "blockchain",
        "python",
        "c++",
    ]
    seen: set[str] = set()
    keywords: list[str] = []
    for kw in preferred + list(resume_profile.keywords):
        if kw in seen:
            continue
        if kw in resume_profile.keywords or kw in {"cryptography", "security", "systems"}:
            keywords.append(kw)
            seen.add(kw)
    if not keywords:
        keywords = ["security", "cryptography", "systems"]
    keywords = keywords[:5]
    role_hint = "researcher OR engineer"
    specs: list[QuerySpec] = []

    selected = sorted(companies, key=lambda c: c["name"].casefold())
    if company_filter:
        selected = [c for c in selected if c["name"].lower() == company_filter.lower()]

    idx = 0
    for c in selected:
        name = c["name"]
        domain = company_domain(c)
        org = str(c.get("github_org", "") or "").strip()
        k1 = keywords[0] if len(keywords) > 0 else "security"
        k2 = keywords[1] if len(keywords) > 1 else "cryptography"
        k3 = keywords[2] if len(keywords) > 2 else "security"

        templates = [
            f"\"{name}\" {k1} {k2} ({role_hint}) (github OR blog OR talk OR paper)",
            f"\"{name}\" {k1} {k3} ({role_hint}) (author OR team OR research)",
        ]
        if domain:
            templates.insert(1, f"site:{domain} ({k1} OR {k2}) (team OR author OR research)")

        for q in templates:
            specs.append(
                QuerySpec(
                    query_id=f"q_{idx:04d}",
                    company=name,
                    company_domain=domain,
                    query=q,
                    github_org=org,
                )
            )
            idx += 1
    return specs


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def canonicalize_url(url: str) -> str:
    try:
        p = parse.urlparse(url)
    except Exception:
        return url.strip()
    scheme = p.scheme.lower() or "https"
    netloc = p.netloc.lower()
    path = p.path or "/"
    if netloc == "github.com":
        parts = [x for x in path.split("/") if x]
        if parts:
            path = "/" + parts[0]
    clean = parse.urlunparse((scheme, netloc, path.rstrip("/") or "/", "", "", ""))
    return clean


def dedupe_key(name: str, canonical_url: str, company: str) -> str:
    s = f"{normalize_name(name)}::{canonicalize_url(canonical_url)}::{company.lower()}"
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def infer_seniority(text: str) -> str:
    t = text.lower()
    for marker in SENIORITY_RULES["junior"]:
        if marker in t:
            return "junior"
    for marker in SENIORITY_RULES["senior"]:
        if marker in t:
            return "senior"
    for marker in SENIORITY_RULES["mid"]:
        if marker in t:
            return "mid"
    return "unknown"


def classify_candidate(seniority: str, resume_level: str) -> str:
    if resume_level == "early-career":
        if seniority == "junior":
            return "peer"
        if seniority == "mid":
            return "near-peer"
        if seniority == "senior":
            return "aspirational"
        return "unclear"
    if seniority == "senior":
        return "near-peer"
    return "unclear"


def likely_person_name(text: str, company: str) -> str | None:
    # Prefer the first title segment.
    base = re.split(r"\s+[|\-:•]\s+", text.strip())[0].strip()
    if len(base) < 4:
        return None
    # Find two-to-three token name-like segments (4+ words are not real names).
    m = re.search(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b", base)
    if not m:
        return None
    candidate = m.group(1).strip()
    low = candidate.lower()
    if company.lower() in low:
        return None
    if any(term in low for term in LOW_SIGNAL_PAGE_TERMS):
        return None
    parts = candidate.split()
    if len(parts) < 2:
        return None
    if any(p.lower() in NON_PERSON_NAME_TOKENS for p in parts):
        return None
    return candidate


def likely_name_from_byline(text: str, company: str) -> str | None:
    if not text:
        return None
    patterns = [
        r"\bby\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b",
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\s+is\s+(?:a|an)\s+(?:researcher|engineer|scientist|intern)\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
            # Re-capitalize simple lowercase matches from IGNORECASE mode.
            candidate = " ".join(p[:1].upper() + p[1:].lower() for p in raw.split())
            if likely_person_name(candidate, company):
                return candidate
    return None


def extract_page_text(url: str, timeout: float) -> tuple[str, str, str, str | None]:
    req = request.Request(url, headers={"User-Agent": USER_AGENT})
    with request.urlopen(req, timeout=timeout) as resp:
        final_url = resp.geturl()
        body = resp.read()
        ctype = resp.headers.get("Content-Type", "")
    text = body.decode("utf-8", errors="replace")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
    author_match = re.search(
        r'<meta[^>]+name=["\']author["\'][^>]+content=["\']([^"\']+)["\']',
        text,
        flags=re.IGNORECASE,
    )
    author_name = author_match.group(1).strip() if author_match else None
    if "html" in ctype.lower() or "<html" in text[:800].lower():
        parser = _VisibleTextExtractor()
        parser.feed(text)
        parser.close()
        visible = parser.text()
    else:
        visible = re.sub(r"\s+", " ", text).strip()
    return final_url, title, visible[:20000], author_name


def keyword_matches(text: str, keywords: list[str]) -> list[str]:
    t = text.lower()
    hits: list[str] = []
    for kw in keywords:
        if kw.lower() in t:
            hits.append(kw)
    return sorted(set(hits))


def has_strong_company_affiliation(
    text: str,
    company: str,
    company_domain: str,
    result_url: str,
) -> tuple[bool, str]:
    lower_text = text.lower()
    lower_company = company.lower()
    lower_url = result_url.lower()
    if company_domain and company_domain in lower_url:
        return True, "company-domain"

    # Require explicit phrasing, not just incidental mention.
    escaped = re.escape(lower_company)
    patterns = [
        rf"\b(at|from|with)\s+{escaped}\b",
        rf"@\s*{escaped}\b",
        rf"\b{escaped}\s+(researcher|engineer|scientist|intern|staff|team)\b",
        rf"\bworks?\s+at\s+{escaped}\b",
    ]
    for pat in patterns:
        if re.search(pat, lower_text):
            return True, "explicit-affiliation-phrase"
    return False, "weak-or-missing-affiliation"


def should_skip_result(url: str) -> bool:
    domain = base_domain(url)
    if not domain:
        return True
    if domain in BLOCKED_DOMAINS:
        return True
    return False


def company_tokens(name: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    return [t for t in tokens if t not in COMPANY_TOKEN_STOPWORDS and len(t) >= 3]


def result_matches_company(result: SearchResult, company: str) -> bool:
    # GitHub results are fetched via a company-scoped query or org membership;
    # the company name needn't appear in the profile URL, title, or bio.
    if result.source in {"github-users", "github-org-members"}:
        return True
    text = f"{result.title} {result.snippet} {result.url}".lower()
    company_lower = company.lower()
    if company_lower in text:
        return True
    tokens = company_tokens(company)
    if not tokens:
        return False
    matched = sum(1 for t in tokens if t in text)
    # Require multiple company tokens when available to avoid ambiguous matches.
    needed = 2 if len(tokens) >= 2 else 1
    return matched >= needed


def merge_unique_str(existing: list[str], new_items: list[str]) -> list[str]:
    out = list(existing)
    seen = {x for x in out}
    for item in new_items:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


def evaluate_candidate(
    result: SearchResult,
    query_spec: QuerySpec,
    resume_profile: ResumeProfile,
    page_timeout: float,
) -> CandidateEvaluation:
    reasons: list[str] = []
    domain = base_domain(result.url)
    if should_skip_result(result.url):
        return CandidateEvaluation(False, 0, ["blocked-domain"], None, None, None, result.url)
    if not result_matches_company(result, query_spec.company):
        return CandidateEvaluation(False, 0, ["company-query-mismatch"], None, None, result.title, result.url)

    if result.source in {"github-users", "github-org-members"}:
        final_url = result.url
        page_title = ""
        page_text = result.snippet or ""
        author_name = None
    else:
        try:
            final_url, page_title, page_text, author_name = extract_page_text(result.url, timeout=page_timeout)
        except Exception as exc:  # pragma: no cover
            return CandidateEvaluation(False, 0, [f"fetch-failed:{exc}"], None, None, result.title, result.url)

    if result.source in {"github-users", "github-org-members"}:
        combined_text = " ".join([result.title or "", result.snippet or ""])
    else:
        combined_text = " ".join(
            [
                result.title or "",
                result.snippet or "",
                page_title or "",
                page_text[:10000] or "",
            ]
        )

    github_name = None
    if result.source in {"github-users", "github-org-members"}:
        m = re.match(r"^(.*?)\s*\(([^()]+)\)\s*$", result.title.strip())
        if m:
            github_display_name = m.group(1).strip()
            github_name = likely_person_name(github_display_name, query_spec.company)
            if not github_name:
                return CandidateEvaluation(
                    False,
                    0,
                    ["github-non-person-account"],
                    None,
                    None,
                    result.title,
                    final_url,
                )

    name = (
        github_name
        or likely_person_name(result.title, query_spec.company)
        or likely_person_name(page_title, query_spec.company)
        or (author_name if author_name and likely_person_name(author_name, query_spec.company) else None)
        or likely_name_from_byline(page_text[:2500], query_spec.company)
    )
    if not name:
        return CandidateEvaluation(False, 0, ["no-person-name"], None, None, result.title, final_url)

    score = 0
    evidence: list[dict[str, str]] = []

    # Identity signal.
    score += 25
    reasons.append("name-detected")
    evidence.append(
        {
            "type": "identity",
            "text": f"Detected likely full name: {name}",
            "source_url": final_url,
        }
    )

    company_hit_text = query_spec.company.lower() in combined_text.lower()
    company_hit_domain = bool(query_spec.company_domain and query_spec.company_domain in domain)
    strong_company_hit, company_hit_reason = has_strong_company_affiliation(
        text=combined_text,
        company=query_spec.company,
        company_domain=query_spec.company_domain,
        result_url=final_url,
    )
    if strong_company_hit and company_hit_text:
        score += 30
        reasons.append("company-mentioned")
        evidence.append(
            {
                "type": "company_affiliation",
                "text": f"Page content references {query_spec.company}.",
                "source_url": final_url,
            }
        )
    elif company_hit_domain and any(k in final_url.lower() for k in ("team", "author", "people", "about", "blog")):
        score += 20
        reasons.append("company-domain-author-page")
        evidence.append(
            {
                "type": "company_affiliation",
                "text": f"URL is on company domain and appears to be a people/author page.",
                "source_url": final_url,
            }
        )

    # GitHub results are found via a company-scoped query or direct org membership;
    # treat that as implicit affiliation evidence when no explicit signal was found.
    if result.source in {"github-users", "github-org-members"} and not strong_company_hit:
        strong_company_hit = True
        company_hit_reason = "github-company-query"
        score += 20
        reasons.append("github-company-query")
        evidence.append(
            {
                "type": "company_affiliation",
                "text": f"Found via GitHub user search scoped to {query_spec.company}.",
                "source_url": final_url,
            }
        )

    kw_hits = keyword_matches(combined_text, resume_profile.keywords)
    if kw_hits:
        kw_score = min(30, 8 * len(kw_hits))
        score += kw_score
        reasons.append(f"keyword-overlap:{','.join(kw_hits[:5])}")
        evidence.append(
            {
                "type": "relevance_topic",
                "text": f"Matched resume keywords: {', '.join(kw_hits[:8])}",
                "source_url": final_url,
            }
        )

    # Source quality bonus.
    if domain in {"github.com", "arxiv.org", "iacr.org"}:
        score += 10
        reasons.append("high-signal-source")

    title_observed = ""
    m = re.search(
        r"\b(intern|researcher|engineer|scientist|developer|lead|staff|principal|director|founder|professor)\b",
        combined_text,
        flags=re.IGNORECASE,
    )
    if m:
        title_observed = m.group(1)
        evidence.append(
            {
                "type": "role_hint",
                "text": f"Observed role keyword: {title_observed}",
                "source_url": final_url,
            }
        )

    seniority = infer_seniority(" ".join([title_observed, combined_text[:1500]]))
    classification = classify_candidate(seniority, resume_profile.inferred_level)

    if not strong_company_hit:
        return CandidateEvaluation(
            False,
            score,
            reasons + [f"missing-strong-company-signal:{company_hit_reason}"],
            None,
            None,
            name,
            final_url,
        )
    # Org members have definitive company affiliation; accept even with no keyword
    # signal since their bio may be sparse — the user can review them manually.
    if len(kw_hits) == 0 and result.source != "github-org-members":
        return CandidateEvaluation(
            False, score, reasons + ["missing-keyword-signal"], None, None, name, final_url
        )
    if len(evidence) < 2:
        return CandidateEvaluation(
            False, score, reasons + ["insufficient-evidence"], None, None, name, final_url
        )

    canonical = canonicalize_url(final_url)
    person = {
        "person_id": "p_" + hashlib.sha256(f"{name}|{canonical}".encode("utf-8")).hexdigest()[:12],
        "full_name": name,
        "normalized_name": normalize_name(name),
        "canonical_url": canonical,
        "profile_urls": [canonicalize_url(result.url), canonical],
        "companies_matched": [query_spec.company],
        "title_observed": title_observed,
        "seniority": seniority,
        "classification": classification,
        "review_status": "new",
        "score": int(score),
        "resume_keyword_matches": kw_hits,
        "evidence": evidence,
        "reasons": reasons,
        "first_seen_at": utc_now(),
        "last_seen_at": utc_now(),
        "added_by_query_id": query_spec.query_id,
    }
    return CandidateEvaluation(
        accepted=True,
        score=score,
        reasons=reasons,
        person=person,
        dedupe_key=dedupe_key(name, canonical, query_spec.company),
        debug_label=name,
        debug_url=canonical,
    )


def upsert_person(people_db: dict[str, Any], person: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    people = people_db.setdefault("people", [])
    canonical = person["canonical_url"]
    normalized_name = person["normalized_name"]
    for existing in people:
        if existing.get("canonical_url") == canonical or existing.get("normalized_name") == normalized_name:
            existing["last_seen_at"] = utc_now()
            existing["score"] = max(int(existing.get("score", 0)), int(person.get("score", 0)))
            existing["profile_urls"] = merge_unique_str(existing.get("profile_urls", []), person.get("profile_urls", []))
            existing["companies_matched"] = merge_unique_str(existing.get("companies_matched", []), person.get("companies_matched", []))
            existing["resume_keyword_matches"] = merge_unique_str(
                existing.get("resume_keyword_matches", []), person.get("resume_keyword_matches", [])
            )
            existing["reasons"] = merge_unique_str(existing.get("reasons", []), person.get("reasons", []))
            existing_evidence = existing.get("evidence", [])
            existing_urls = {(e.get("type"), e.get("text"), e.get("source_url")) for e in existing_evidence if isinstance(e, dict)}
            for e in person.get("evidence", []):
                key = (e.get("type"), e.get("text"), e.get("source_url"))
                if key not in existing_urls:
                    existing_evidence.append(e)
            existing["evidence"] = existing_evidence
            return "updated", existing

    people.append(person)
    return "added", person


def find_inspiration(args: Any) -> int:
    company_db_path: Path = args.db
    people_db_path: Path = args.people_db
    state_path: Path = args.inspiration_state
    resume_path: Path = args.resume

    ensure_db_files(people_db_path=people_db_path, state_path=state_path)

    raw_company_db = load_json(company_db_path, {"companies": []})
    companies = normalize_company_db(raw_company_db)
    if not companies:
        print("No companies found in company DB.")
        return 1

    people_db = load_json(people_db_path, {"meta": {"version": 1}, "people": []})
    state = load_json(
        state_path,
        {"query_cursor": 0, "query_history": [], "resume_cache": {}, "last_run": None},
    )

    resume_profile = build_resume_profile(resume_path)
    state["resume_cache"] = resume_profile.to_dict()

    specs = build_query_specs(
        companies=companies,
        resume_profile=resume_profile,
        company_filter=args.company,
    )
    if not specs:
        print("No query specs available. Check company filter.")
        return 1

    target: int = getattr(args, "target", 5)
    manual_query = bool(args.query)
    max_queries = 1 if manual_query else len(specs)
    cursor = int(state.get("query_cursor", 0))

    print(
        f"Resume profile: level={resume_profile.inferred_level}, keywords={', '.join(resume_profile.keywords[:6])}"
    )
    print(f"Target: {target} people | Queries available: {max_queries}")

    added: list[dict[str, Any]] = []
    total_updated = 0
    total_skipped = 0
    total_seen = 0
    skip_reason_counts: dict[str, int] = {}
    scored_rejects: list[CandidateEvaluation] = []
    queries_run = 0

    while len(added) < target and queries_run < max_queries:
        if manual_query:
            chosen = QuerySpec(
                query_id=f"manual_{int(datetime.now(tz=timezone.utc).timestamp())}",
                company=args.company or specs[0].company,
                company_domain=specs[0].company_domain,
                query=args.query,
            )
        else:
            chosen = specs[cursor % len(specs)]
            cursor += 1
        queries_run += 1

        org_label = f" [org:{chosen.github_org}]" if chosen.github_org else ""
        print(f"\nQuery {chosen.query_id} ({queries_run}/{max_queries}){org_label}: {chosen.query}")

        try:
            search_results = search_web(
                query=chosen.query,
                max_results=args.max_results,
                timeout=args.search_timeout,
                github_org=chosen.github_org,
            )
        except Exception as exc:
            print(f"  Search failed: {exc}")
            continue

        if not search_results:
            print("  No search results.")
            continue

        total_seen += len(search_results)
        query_added = 0
        for result in search_results:
            evaluation = evaluate_candidate(
                result=result,
                query_spec=chosen,
                resume_profile=resume_profile,
                page_timeout=args.page_timeout,
            )
            if not evaluation.accepted or not evaluation.person:
                total_skipped += 1
                for reason in evaluation.reasons:
                    skip_reason_counts[reason] = skip_reason_counts.get(reason, 0) + 1
                if evaluation.score > 0:
                    scored_rejects.append(evaluation)
                continue
            action, person = upsert_person(people_db, evaluation.person)
            if action == "added":
                added.append(person)
                query_added += 1
                print(f"  + {person['full_name']} ({person['canonical_url']})")
            else:
                total_updated += 1

        if not args.dry_run:
            state.setdefault("query_history", []).append(
                {
                    "query_id": chosen.query_id,
                    "query": chosen.query,
                    "company": chosen.company,
                    "executed_at": utc_now(),
                    "results_seen": len(search_results),
                    "added_count": query_added,
                    "updated_count": total_updated,
                    "skipped_count": total_skipped,
                }
            )

        print(f"  Query done: added={query_added} total_so_far={len(added)}/{target}")

    if not args.dry_run:
        save_json(people_db_path, people_db)
        state["query_cursor"] = cursor
        state["last_run"] = utc_now()
        save_json(state_path, state)

    if added:
        print()
        print(f"Added {len(added)} people:")
        for idx, p in enumerate(sorted(added, key=lambda x: (-int(x.get("score", 0)), x.get("full_name", ""))), start=1):
            print(
                f"{idx:>2}. {p['full_name']} | {p['classification']} | score={p['score']} | status={p['review_status']}"
            )
            print(f"    {p['canonical_url']}")
            if p.get("reasons"):
                print(f"    reasons: {', '.join(p['reasons'][:3])}")
    else:
        print("\nAdded 0 people.")

    print()
    print(
        f"Done. queries_run={queries_run} seen={total_seen} added={len(added)} updated={total_updated} skipped={total_skipped}"
    )
    if getattr(args, "debug_skips", False) and skip_reason_counts:
        print("Skip reasons:")
        for reason, count in sorted(skip_reason_counts.items(), key=lambda x: (-x[1], x[0])):
            print(f"  - {reason}: {count}")
        if scored_rejects:
            print("Top scored rejects:")
            ranked = sorted(scored_rejects, key=lambda x: -x.score)[:5]
            for item in ranked:
                label = item.debug_label or "(unknown)"
                why = ", ".join(item.reasons[:3])
                print(f"  - score={item.score} | {label} | {why}")
                if item.debug_url:
                    print(f"    {item.debug_url}")
    if args.dry_run:
        print("Dry run: no files were modified.")
    return 0


def list_inspiration_people(args: Any) -> int:
    people_db_path: Path = args.people_db
    company_filter: str | None = getattr(args, "company", None)
    db = load_json(people_db_path, {"meta": {"version": 1}, "people": []})
    people = db.get("people", [])
    if not isinstance(people, list) or not people:
        print("No people in inspiration DB.")
        return 0

    people_sorted = sorted(
        people,
        key=lambda p: (
            str(p.get("review_status", "new")),
            -int(p.get("score", 0)),
            str(p.get("full_name", "")).casefold(),
        ),
    )

    if company_filter:
        cf = company_filter.lower()
        people_sorted = [
            p for p in people_sorted
            if cf in [c.lower() for c in p.get("companies_matched", [])]
        ]

    print(f"People: {len(people_sorted)}" + (f" (filtered: {company_filter})" if company_filter else ""))
    for idx, p in enumerate(people_sorted, start=1):
        name = p.get("full_name", "(unknown)")
        pid = p.get("person_id", "")
        status = p.get("review_status", "new")
        score = p.get("score", 0)
        companies = p.get("companies_matched", [])
        company_text = ", ".join(companies[:2]) if isinstance(companies, list) else ""
        kw = p.get("resume_keyword_matches", [])
        print(f"{idx:>2}. {name} | id={pid} | status={status} | score={score}")
        if company_text:
            print(f"    companies: {company_text}")
        if p.get("canonical_url"):
            print(f"    {p['canonical_url']}")
        if kw:
            print(f"    keywords: {', '.join(kw[:6])}")
    return 0


def set_inspiration_status(args: Any) -> int:
    status = str(args.status).strip().lower()
    if status not in REVIEW_STATUSES:
        print(f"Invalid status '{status}'. Allowed: {', '.join(sorted(REVIEW_STATUSES))}")
        return 1

    people_db_path: Path = args.people_db
    db = load_json(people_db_path, {"meta": {"version": 1}, "people": []})
    people = db.get("people", [])
    if not isinstance(people, list) or not people:
        print("No people in inspiration DB.")
        return 1

    key = str(args.person).strip()
    matches = []
    for p in people:
        if p.get("person_id") == key:
            matches = [p]
            break
        name = str(p.get("full_name", ""))
        if name.lower() == key.lower():
            matches.append(p)

    if not matches:
        print(f"No person matched: {key}")
        return 1
    if len(matches) > 1:
        print(f"Multiple people matched '{key}'. Use person_id instead.")
        for p in matches:
            print(f"- {p.get('person_id','')} | {p.get('full_name','')}")
        return 1

    person = matches[0]
    person["review_status"] = status
    person["last_seen_at"] = utc_now()
    save_json(people_db_path, db)
    print(f"Updated status: {person.get('full_name', key)} -> {status}")
    return 0
