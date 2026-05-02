#!/usr/bin/env python3
from __future__ import annotations

import html
import base64
import json
import re
from dataclasses import dataclass
from urllib import parse, request


USER_AGENT = "career-tools-inspiration/1.0"
GITHUB_HINT_KEYWORDS = [
    "cryptography",
    "security",
    "research",
    "engineer",
    "systems",
    "rust",
    "zero-knowledge",
]


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str


def _clean_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _decode_result_url(href: str) -> str:
    href = html.unescape(href)
    parsed = parse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        q = parse.parse_qs(parsed.query)
        uddg = q.get("uddg")
        if uddg:
            return parse.unquote(uddg[0])
    return href


def _maybe_decode_bing_redirect(href: str) -> str:
    parsed = parse.urlparse(href)
    if "bing.com" not in parsed.netloc:
        return href
    if not parsed.path.startswith("/ck/"):
        return href

    params = parse.parse_qs(parsed.query)
    encoded = params.get("u", [None])[0]
    if not encoded:
        return href
    # Observed Bing pattern: u=a1<base64url("https://...")>
    if encoded.startswith("a1"):
        payload = encoded[2:]
    else:
        payload = encoded
    payload = payload.strip()
    pad = "=" * ((4 - len(payload) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode((payload + pad).encode("utf-8")).decode(
            "utf-8", errors="replace"
        )
        if decoded.startswith(("http://", "https://")):
            return decoded
    except Exception:
        pass
    return href


def _search_duckduckgo(query: str, max_results: int, timeout: float) -> list[SearchResult]:
    url = "https://html.duckduckgo.com/html/?" + parse.urlencode({"q": query})
    req = request.Request(url, headers={"User-Agent": USER_AGENT})
    with request.urlopen(req, timeout=timeout) as resp:
        content = resp.read().decode("utf-8", errors="replace")

    results: list[SearchResult] = []

    # Extract titles + links from classic HTML endpoint.
    for m in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        content,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        href = _decode_result_url(m.group(1))
        title = _clean_text(m.group(2))
        if not href or not title:
            continue
        if href.startswith("/"):
            continue
        results.append(
            SearchResult(
                title=title,
                url=href,
                snippet="",
                source="duckduckgo-html",
            )
        )
        if len(results) >= max_results:
            break
    return results


def _search_bing(query: str, max_results: int, timeout: float) -> list[SearchResult]:
    url = "https://www.bing.com/search?" + parse.urlencode({"q": query, "count": str(max_results)})
    req = request.Request(url, headers={"User-Agent": USER_AGENT})
    with request.urlopen(req, timeout=timeout) as resp:
        content = resp.read().decode("utf-8", errors="replace")

    results: list[SearchResult] = []
    for m in re.finditer(
        r"<li[^>]+class=\"b_algo\"[^>]*>.*?<h2[^>]*><a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a></h2>(.*?)</li>",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        href = _maybe_decode_bing_redirect(html.unescape(m.group(1)).strip())
        title = _clean_text(m.group(2))
        snippet_block = m.group(3)
        s_match = re.search(r"<p>(.*?)</p>", snippet_block, flags=re.IGNORECASE | re.DOTALL)
        snippet = _clean_text(s_match.group(1)) if s_match else ""
        if not href or not title:
            continue
        results.append(
            SearchResult(
                title=title,
                url=href,
                snippet=snippet,
                source="bing-html",
            )
        )
        if len(results) >= max_results:
            break
    return results


def _search_github_users(query: str, max_results: int, timeout: float) -> list[SearchResult]:
    quoted = re.findall(r'"([^"]+)"', query)
    phrase = quoted[0].strip() if quoted else ""
    lower_q = query.lower()
    hint = next((k for k in GITHUB_HINT_KEYWORDS if k in lower_q), "")

    attempts: list[str] = []
    if phrase and hint:
        attempts.append(f'"{phrase}" {hint}')
    if phrase:
        attempts.append(f'"{phrase}"')
    if hint:
        attempts.append(hint)
    attempts.append(query)

    logins: list[str] = []
    for candidate_query in attempts:
        candidate_query = candidate_query.strip()
        if not candidate_query:
            continue
        url = "https://api.github.com/search/users?" + parse.urlencode(
            {"q": candidate_query, "per_page": str(min(max_results, 10))}
        )
        req = request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
        )
        with request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        logins = [
            item["login"]
            for item in data.get("items", [])
            if item.get("type") == "User" and item.get("login")
        ]
        if logins:
            break

    results: list[SearchResult] = []
    # Fetch individual profiles for display name, bio, and company.
    # Limit to 5 requests to stay within GitHub's unauthenticated rate limit.
    for login in logins[: min(max_results, 5)]:
        try:
            profile_req = request.Request(
                f"https://api.github.com/users/{login}",
                headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
            )
            with request.urlopen(profile_req, timeout=timeout) as resp:
                profile = json.loads(resp.read().decode("utf-8", errors="replace"))
            display_name = (profile.get("name") or "").strip()
            bio = (profile.get("bio") or "").strip()
            company = (profile.get("company") or "").strip().lstrip("@")
            snippet = " | ".join(p for p in [bio, company] if p)
        except Exception:
            display_name = ""
            snippet = ""
        title = f"{display_name} ({login})" if display_name else login
        results.append(
            SearchResult(
                title=title,
                url=f"https://github.com/{login}",
                snippet=snippet,
                source="github-users",
            )
        )
    return results


def _search_github_org_members(org: str, max_results: int, timeout: float) -> list[SearchResult]:
    url = "https://api.github.com/orgs/" + parse.quote(org) + "/members?" + parse.urlencode(
        {"per_page": str(min(max_results, 30)), "filter": "all"}
    )
    req = request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with request.urlopen(req, timeout=timeout) as resp:
        members = json.loads(resp.read().decode("utf-8", errors="replace"))

    results: list[SearchResult] = []
    for member in members[: min(max_results, 5)]:
        login = member.get("login", "")
        if not login or member.get("type") != "User":
            continue
        try:
            profile_req = request.Request(
                f"https://api.github.com/users/{login}",
                headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
            )
            with request.urlopen(profile_req, timeout=timeout) as resp:
                profile = json.loads(resp.read().decode("utf-8", errors="replace"))
            display_name = (profile.get("name") or "").strip()
            bio = (profile.get("bio") or "").strip()
            company = (profile.get("company") or "").strip().lstrip("@")
            snippet = " | ".join(p for p in [bio, company] if p)
        except Exception:
            display_name = ""
            snippet = ""
        title = f"{display_name} ({login})" if display_name else login
        results.append(
            SearchResult(
                title=title,
                url=f"https://github.com/{login}",
                snippet=snippet,
                source="github-org-members",
            )
        )
    return results


def search_web(query: str, max_results: int, timeout: float, github_org: str = "") -> list[SearchResult]:
    errors: list[str] = []
    if github_org:
        try:
            results = _search_github_org_members(org=github_org, max_results=max_results, timeout=timeout)
            if results:
                return results
        except Exception as exc:
            errors.append(f"github-org:{exc}")
    for fn in (_search_github_users, _search_duckduckgo, _search_bing):
        try:
            results = fn(query=query, max_results=max_results, timeout=timeout)
            if results:
                return results
        except Exception as exc:  # pragma: no cover
            errors.append(str(exc))
            continue
    if errors:
        raise RuntimeError("Search failed: " + " | ".join(errors))
    return []
