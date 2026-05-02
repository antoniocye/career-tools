#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


KEYWORD_CANDIDATES = {
    "cryptography",
    "zero-knowledge",
    "zk",
    "proofs",
    "security",
    "systems",
    "distributed",
    "protocol",
    "blockchain",
    "rust",
    "c++",
    "python",
    "verification",
    "research",
    "audits",
    "embedded",
    "linux",
    "docker",
}

ROLE_CANDIDATES = {
    "researcher",
    "research engineer",
    "security engineer",
    "cryptography engineer",
    "software engineer",
    "systems engineer",
    "protocol engineer",
    "intern",
}

STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "you",
    "your",
    "are",
    "was",
    "were",
    "have",
    "has",
    "had",
    "not",
    "but",
    "all",
    "any",
    "more",
    "than",
    "into",
    "about",
    "over",
    "under",
    "each",
    "through",
    "using",
    "built",
    "led",
}


@dataclass
class ResumeProfile:
    resume_path: str
    extracted_at: str
    inferred_level: str
    keywords: list[str]
    role_terms: list[str]
    summary_terms: list[str]

    def to_dict(self) -> dict:
        return {
            "resume_path": self.resume_path,
            "extracted_at": self.extracted_at,
            "inferred_level": self.inferred_level,
            "keywords": list(self.keywords),
            "role_terms": list(self.role_terms),
            "summary_terms": list(self.summary_terms),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ResumeProfile":
        return cls(
            resume_path=str(data.get("resume_path", "")),
            extracted_at=str(data.get("extracted_at", "")),
            inferred_level=str(data.get("inferred_level", "unclear")),
            keywords=[str(x) for x in data.get("keywords", [])],
            role_terms=[str(x) for x in data.get("role_terms", [])],
            summary_terms=[str(x) for x in data.get("summary_terms", [])],
        )


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def extract_resume_text(resume_path: Path) -> str:
    if not resume_path.exists():
        raise ValueError(f"Resume not found: {resume_path}")
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", str(resume_path), "-"],
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout
    except FileNotFoundError as exc:
        raise ValueError("pdftotext is required but not installed.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else "unknown error"
        raise ValueError(f"Failed to parse resume PDF: {stderr}") from exc


def infer_level(text: str) -> str:
    t = text.lower()
    early_markers = ["incoming", "intern", "student", "b.s.", "undergraduate"]
    if any(m in t for m in early_markers):
        return "early-career"
    if "senior" in t or "staff" in t or "lead" in t:
        return "experienced"
    return "unclear"


def token_counts(text: str) -> Counter[str]:
    tokens = re.findall(r"[a-z][a-z0-9+\-]{2,}", text.lower())
    return Counter(t for t in tokens if t not in STOPWORDS and not any(ch.isdigit() for ch in t))


def infer_keywords(text: str) -> tuple[list[str], list[str]]:
    counts = token_counts(text)

    scored: list[tuple[int, str]] = []
    for kw in KEYWORD_CANDIDATES:
        score = counts.get(kw, 0)
        if kw == "zk":
            score += counts.get("zero-knowledge", 0)
        if score > 0:
            scored.append((score, kw))
    scored.sort(key=lambda x: (-x[0], x[1]))

    keywords = [kw for _, kw in scored[:10]]
    if "cryptography" not in keywords and counts.get("cryptography", 0):
        keywords.insert(0, "cryptography")

    summary_terms = [w for w, c in counts.most_common(30) if c >= 2][:12]
    return keywords, summary_terms


def infer_role_terms(text: str) -> list[str]:
    t = text.lower()
    roles = [r for r in ROLE_CANDIDATES if r in t]
    if not roles:
        roles = ["researcher", "security engineer", "software engineer", "intern"]
    return sorted(set(roles))


def build_resume_profile(resume_path: Path) -> ResumeProfile:
    text = extract_resume_text(resume_path)
    keywords, summary_terms = infer_keywords(text)
    role_terms = infer_role_terms(text)
    level = infer_level(text)
    return ResumeProfile(
        resume_path=str(resume_path),
        extracted_at=utc_now(),
        inferred_level=level,
        keywords=keywords,
        role_terms=role_terms,
        summary_terms=summary_terms,
    )
