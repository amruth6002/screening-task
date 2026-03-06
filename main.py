#!/usr/bin/env python3
"""
FOA Ingestion Pipeline — GSoC 2026 Screening Task

Ingests a single Funding Opportunity Announcement (FOA) from a public source
(NSF or Grants.gov), extracts structured fields, applies deterministic
rule-based semantic tags, and exports the result as JSON and CSV.

Supports:
  - NSF solicitations   → HTML scraping (requests + BeautifulSoup)
  - Grants.gov (Simpler) → REST API (requires free API key)

Usage:
    python main.py --url "<FOA_URL>" --out_dir ./out

    # For Grants.gov, provide an API key via flag or env var:
    python main.py --url "https://simpler.grants.gov/opportunity/<UUID>" \\
                   --api_key "YOUR_KEY" --out_dir ./out

    # Or via environment variable:
    export GRANTS_GOV_API_KEY="YOUR_KEY"
    python main.py --url "https://simpler.grants.gov/opportunity/<UUID>" --out_dir ./out

Author: Amruth — GSoC 2026 Applicant (HumanAI Foundation / ISSR)
"""

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

#  SCHEMA

@dataclass
class FOATags:
    """Semantic tags applied to a funding opportunity."""
    research_domains: list[str] = field(default_factory=list)
    methods_approaches: list[str] = field(default_factory=list)
    populations: list[str] = field(default_factory=list)
    sponsor_themes: list[str] = field(default_factory=list)


@dataclass
class FOA:
    """Standardized Funding Opportunity Announcement schema."""
    foa_id: str
    title: str
    agency: str
    open_date: str            # ISO 8601 (YYYY-MM-DD)
    close_date: str           # ISO 8601 (YYYY-MM-DD)
    eligibility: str
    description: str
    award_floor: Optional[str] = None
    award_ceiling: Optional[str] = None
    source_url: str = ""
    tags: FOATags = field(default_factory=FOATags)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def csv_headers(self) -> list[str]:
        return [
            "foa_id", "title", "agency", "open_date", "close_date",
            "eligibility", "description", "award_floor", "award_ceiling",
            "source_url", "tags_research_domains", "tags_methods_approaches",
            "tags_populations", "tags_sponsor_themes",
        ]

    def csv_values(self) -> list[str]:
        return [
            self.foa_id, self.title, self.agency, self.open_date,
            self.close_date, self.eligibility, self.description,
            self.award_floor or "", self.award_ceiling or "", self.source_url,
            "; ".join(self.tags.research_domains),
            "; ".join(self.tags.methods_approaches),
            "; ".join(self.tags.populations),
            "; ".join(self.tags.sponsor_themes),
        ]


#  SEMANTIC TAGGER — Rule-based, deterministic


RESEARCH_DOMAINS: dict[str, list[str]] = {
    "artificial intelligence": [
        "artificial intelligence", "machine learning", "deep learning",
        "neural network", "natural language processing", "nlp",
        "computer vision", "reinforcement learning", "generative ai",
        "large language model", "llm",
    ],
    "cybersecurity": [
        "cybersecurity", "cyber security", "information security",
        "network security", "vulnerability", "threat detection",
        "encryption", "malware", "intrusion detection",
    ],
    "healthcare & biomedical": [
        "healthcare", "health care", "biomedical", "clinical",
        "pharmaceutical", "drug discovery", "genomics", "epidemiology",
        "mental health", "public health", "disease", "patient",
        "medical device",
    ],
    "education": [
        "education", "stem education", "curriculum", "k-12",
        "undergraduate", "graduate student", "workforce development",
        "literacy", "teaching", "training program",
    ],
    "energy & environment": [
        "renewable energy", "solar", "wind energy", "battery",
        "clean energy", "climate change", "environmental",
        "sustainability", "carbon", "greenhouse gas", "decarbonization",
        "energy storage", "grid modernization",
    ],
    "agriculture & food": [
        "agriculture", "farming", "crop", "soil", "food security",
        "food safety", "livestock", "aquaculture", "irrigation",
    ],
    "transportation & mobility": [
        "transportation", "aviation", "autonomous vehicle",
        "traffic", "mobility", "transit", "railroad",
        "unmanned aircraft", "drone",
    ],
    "advanced manufacturing": [
        "manufacturing", "3d printing", "additive manufacturing",
        "robotics", "automation", "semiconductor", "microelectronics",
    ],
    "open source & software": [
        "open source", "open-source", "software ecosystem",
        "open source software", "oss", "software supply chain",
    ],
    "space & geosciences": [
        "space", "satellite", "geoscience", "earth observation",
        "remote sensing", "planetary", "astronomy",
    ],
    "social & behavioral sciences": [
        "social science", "behavioral", "psychology", "sociology",
        "economics", "political science", "anthropology",
    ],
    "national security & defense": [
        "national security", "defense", "homeland security",
        "military", "intelligence", "counterterrorism",
    ],
}

METHODS_APPROACHES: dict[str, list[str]] = {
    "machine learning": [
        "machine learning", "deep learning", "neural network",
        "classification", "regression model", "clustering",
        "supervised learning", "unsupervised learning",
    ],
    "simulation & modeling": [
        "simulation", "computational model", "numerical model",
        "finite element", "agent-based model", "digital twin",
    ],
    "data science & analytics": [
        "data science", "data analytics", "big data", "data mining",
        "statistical analysis", "data pipeline",
    ],
    "clinical trial": [
        "clinical trial", "randomized controlled", "rct",
        "phase i", "phase ii", "phase iii",
    ],
    "survey & assessment": [
        "survey", "assessment", "evaluation", "benchmark",
        "questionnaire", "interview",
    ],
    "hardware & systems": [
        "hardware", "prototype", "testbed", "sensor",
        "embedded system", "iot", "fpga",
    ],
    "community engagement": [
        "community engagement", "stakeholder", "participatory",
        "citizen science", "outreach", "co-design",
    ],
}

POPULATIONS: dict[str, list[str]] = {
    "students & early-career": [
        "student", "undergraduate", "graduate student", "postdoc",
        "early-career", "early career", "fellow", "trainee",
    ],
    "rural communities": [
        "rural", "underserved community", "remote community",
    ],
    "small businesses & startups": [
        "small business", "startup", "start-up", "entrepreneur",
        "sbir", "sttr",
    ],
    "minority & underrepresented": [
        "minority", "underrepresented", "hbcu", "tribal",
        "hispanic-serving", "women in stem", "dei",
        "diversity", "equity", "inclusion",
    ],
    "industry & government": [
        "industry partner", "government agency", "federal agency",
        "state government", "local government",
    ],
    "general public": [
        "general public", "society", "national need", "societal",
    ],
}

AGENCY_THEME_MAP: dict[str, list[str]] = {
    "nsf": ["science & engineering", "fundamental research", "stem education"],
    "national science foundation": ["science & engineering", "fundamental research", "stem education"],
    "nih": ["health & biomedical research"],
    "national institutes of health": ["health & biomedical research"],
    "doe": ["energy", "national laboratories"],
    "department of energy": ["energy", "national laboratories"],
    "dod": ["defense", "national security"],
    "department of defense": ["defense", "national security"],
    "darpa": ["defense", "advanced research"],
    "faa": ["aviation", "transportation safety"],
    "federal aviation administration": ["aviation", "transportation safety"],
    "dot": ["transportation", "infrastructure"],
    "department of transportation": ["transportation", "infrastructure"],
    "epa": ["environment", "public health"],
    "usda": ["agriculture", "food security", "rural development"],
    "nasa": ["space", "aeronautics", "earth science"],
    "noaa": ["ocean & atmosphere", "climate", "weather"],
    "ed": ["education"],
    "department of education": ["education"],
    "hhs": ["health & human services"],
    "acf": ["children & families", "social services"],
}


def _match_keywords(text: str, ontology: dict[str, list[str]]) -> list[str]:
    """Return tag names whose keywords appear in the text."""
    text_lower = text.lower()
    matched = []
    for tag_name, keywords in ontology.items():
        for kw in keywords:
            if kw in text_lower:
                matched.append(tag_name)
                break
    return sorted(matched)


def _match_sponsor_themes(agency: str) -> list[str]:
    """Map agency name to sponsor themes."""
    agency_lower = agency.lower().strip()
    themes: set[str] = set()
    for key, theme_list in AGENCY_THEME_MAP.items():
        if key in agency_lower:
            themes.update(theme_list)
    return sorted(themes)


def apply_tags(foa: FOA) -> FOA:
    """Apply semantic tags to a FOA object and return it."""
    combined_text = " ".join([foa.title or "", foa.description or "", foa.eligibility or ""])
    foa.tags = FOATags(
        research_domains=_match_keywords(combined_text, RESEARCH_DOMAINS),
        methods_approaches=_match_keywords(combined_text, METHODS_APPROACHES),
        populations=_match_keywords(combined_text, POPULATIONS),
        sponsor_themes=_match_sponsor_themes(foa.agency),
    )
    return foa


# NSF SCRAPER — HTML-based (requests + BeautifulSoup)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _parse_date_safe(text: str) -> str:
    """Try to parse a date string into ISO 8601 (YYYY-MM-DD)."""
    if not text:
        return ""
    try:
        dt = dateparser.parse(text, fuzzy=True)
        if dt:
            return dt.strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        pass
    return text.strip()


def _extract_section_by_roman(soup: BeautifulSoup, numeral: str) -> str:
    """Extract an NSF section by its Roman numeral prefix (e.g. 'II.', 'III.')."""
    for heading in soup.find_all(["h2", "h3"]):
        text = heading.get_text(strip=True)
        if text.startswith(numeral) or numeral in text:
            parts = []
            for sibling in heading.find_next_siblings():
                if sibling.name in ("h2", "h3"):
                    next_text = sibling.get_text(strip=True)
                    if re.match(r"^[IVX]+\.", next_text):
                        break
                content = sibling.get_text(separator=" ", strip=True)
                if content:
                    parts.append(content)
            return " ".join(parts)[:2000]
    return ""


def scrape_nsf(url: str) -> FOA:
    """Scrape an NSF solicitation page and return a structured FOA."""

    print(f"[NSF] Fetching: {url}")
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    # Title & Solicitation Number
    h1 = soup.find("h1")
    full_title = h1.get_text(strip=True) if h1 else ""
    foa_id, title = "", full_title

    id_match = re.match(r"(NSF\s+\d{2}-\d{3}):\s*(.*)", full_title)
    if id_match:
        foa_id = id_match.group(1).strip()
        title = id_match.group(2).strip()
    else:
        url_match = re.search(r"/(nsf\d{2}-\d{3})/", url, re.IGNORECASE)
        if url_match:
            foa_id = url_match.group(1).upper().replace("NSF", "NSF ")
    if not foa_id:
        foa_id = f"NSF-{hash(url) % 100000:05d}"

    # Agency 
    agency = "National Science Foundation (NSF)"

    # Posted Date
    open_date = ""
    posted_match = soup.find(string=re.compile(r"Posted:", re.IGNORECASE))
    if posted_match:
        parent = posted_match.find_parent("li") or posted_match.find_parent()
        if parent:
            date_text = parent.get_text(strip=True).replace("Posted:", "").strip()
            open_date = _parse_date_safe(date_text)

    # Close Date / Deadlines
    close_date = ""
    deadline_heading = soup.find(string=re.compile(r"Full Proposal Deadline", re.IGNORECASE))
    if deadline_heading:
        parent = deadline_heading.find_parent()
        if parent:
            for sibling in parent.find_next_siblings():
                text = sibling.get_text(strip=True)
                if text:
                    close_date = _parse_date_safe(text)
                    break

    if not close_date:
        for el in soup.find_all(string=re.compile(r"deadline|due date", re.IGNORECASE)):
            parent = el.find_parent()
            if parent:
                text = parent.get_text(strip=True)
                date_match = re.search(r"(\w+ \d{1,2}, \d{4})", text)
                if date_match:
                    close_date = _parse_date_safe(date_match.group(1))
                    break

    # Description 
    description = ""
    synopsis = soup.find(string=re.compile(r"Synopsis of Program", re.IGNORECASE))
    if synopsis:
        parent = synopsis.find_parent()
        if parent:
            parts = []
            for sibling in parent.find_next_siblings():
                if sibling.name in ("h3", "h4") and sibling != parent:
                    break
                text = sibling.get_text(separator=" ", strip=True)
                if text:
                    parts.append(text)
            description = " ".join(parts)

    if not description or len(description) < 50:
        description = _extract_section_by_roman(soup, "II.") or description
    if not description or len(description) < 50:
        description = _extract_section_by_roman(soup, "I.") or description

    # Award Information 
    award_floor, award_ceiling = None, None
    award_text = _extract_section_by_roman(soup, "III.")
    if award_text:
        amounts = re.findall(r"\$[\d,]+(?:\.\d{2})?", award_text)
        if len(amounts) >= 2:
            award_floor, award_ceiling = amounts[0], amounts[-1]
        elif len(amounts) == 1:
            if "maximum" in award_text.lower():
                award_ceiling = amounts[0]
            else:
                award_ceiling = amounts[0]

    # Eligibility
    eligibility = _extract_section_by_roman(soup, "IV.")
    if not eligibility:
        who = soup.find(string=re.compile(r"Who May Submit", re.IGNORECASE))
        if who:
            parent = who.find_parent()
            if parent:
                parts = []
                for sibling in parent.find_next_siblings():
                    if sibling.name in ("h3", "h4"):
                        break
                    text = sibling.get_text(separator=" ", strip=True)
                    if text:
                        parts.append(text)
                eligibility = " ".join(parts)[:2000]

    if len(description) > 3000:
        description = description[:3000] + "..."

    print(f"  [NSF] Extracted: {foa_id} — {title[:60]}...")
    return FOA(
        foa_id=foa_id, title=title, agency=agency,
        open_date=open_date, close_date=close_date,
        eligibility=eligibility, description=description,
        award_floor=award_floor, award_ceiling=award_ceiling,
        source_url=url,
    )


GRANTS_API_BASE = "https://api.simpler.grants.gov"


def _extract_grants_opp_id(url: str) -> str:
    """Extract the opportunity UUID or numeric ID from a Grants.gov URL."""
    # Pattern: /opportunity/<uuid>
    match = re.search(r"/opportunity/([\w-]+)", url)
    if match:
        return match.group(1)
    # Pattern: /search-results-detail/<id>
    match = re.search(r"/search-results-detail/(\d+)", url)
    if match:
        return match.group(1)
    return ""


def scrape_grants_gov(url: str, api_key: str) -> FOA:
    """Fetch a Grants.gov opportunity via the Simpler Grants REST API.

    Args:
        url: Grants.gov or Simpler Grants opportunity URL.
        api_key: Free API key from simpler.grants.gov.

    Returns:
        FOA object with extracted fields.
    """
    opp_id = _extract_grants_opp_id(url)
    if not opp_id:
        raise ValueError(f"Could not extract opportunity ID from URL: {url}")

    api_url = f"{GRANTS_API_BASE}/v1/opportunities/{opp_id}"
    print(f"[Grants.gov] Calling API: {api_url}")

    resp = requests.get(
        api_url,
        headers={
            "X-API-Key": api_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        timeout=30,
    )

    if resp.status_code == 401:
        print("  [Grants.gov] ERROR: API key is invalid or missing.", file=sys.stderr)
        print("  [Grants.gov] Get a free key at: https://simpler.grants.gov", file=sys.stderr)
        sys.exit(1)
    elif resp.status_code == 404:
        raise ValueError(
            f"Opportunity '{opp_id}' not found. If using a legacy numeric ID, "
            f"try finding the UUID on simpler.grants.gov/search instead."
        )

    resp.raise_for_status()
    data = resp.json()

    # Navigate API response structure
    opp = data.get("data", data)
    summary = opp.get("summary", opp)

    # Extract eligibility — may be a list of applicant types
    eligibility = summary.get("applicant_types", summary.get("eligibility", ""))
    if isinstance(eligibility, list):
        eligibility = "; ".join(str(e) for e in eligibility)

    # Extract award amounts
    award_floor = summary.get("award_floor")
    award_ceiling = summary.get("award_ceiling")

    # IDs and names are typically at the root level 'opp', sometimes in 'summary'
    foa_id = (
        opp.get("opportunity_number")
        or summary.get("opportunity_number")
        or summary.get("funding_opportunity_number")
        or f"GRANTS-{opp_id[:12]}"
    )

    foa = FOA(
        foa_id=foa_id,
        title=opp.get("opportunity_title", summary.get("title", "")),
        agency=opp.get("agency_name", summary.get("agency", "")),
        open_date=_parse_date_safe(str(summary.get("post_date", summary.get("open_date", "")))),
        close_date=_parse_date_safe(str(summary.get("close_date", ""))),
        eligibility=eligibility,
        description=summary.get("summary_description", summary.get("description", "")),
        award_floor=f"${award_floor:,.0f}" if award_floor else None,
        award_ceiling=f"${award_ceiling:,.0f}" if award_ceiling else None,
        source_url=url,
    )

    print(f"  [Grants.gov] Extracted: {foa.foa_id} — {foa.title[:60]}...")
    return foa


# Exporter — JSON + CSV

def export(foa: FOA, out_dir: str) -> tuple[str, str]:
    """Export FOA to foa.json and foa.csv in the given directory."""
    os.makedirs(out_dir, exist_ok=True)

    # JSON
    json_path = os.path.join(out_dir, "foa.json")
    with open(json_path, "w", encoding="utf-8") as f:
        f.write(foa.to_json(indent=2))

    # CSV
    csv_path = os.path.join(out_dir, "foa.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(foa.csv_headers())
        writer.writerow(foa.csv_values())

    return json_path, csv_path


# CLI Entry Point

def detect_source(url: str) -> str:

    """Detect FOA source from URL domain."""

    domain = urlparse(url).netloc.lower()
    if "nsf.gov" in domain:
        return "nsf"
    elif "grants.gov" in domain:
        return "grants_gov"
    raise ValueError(
        f"Unsupported source: {domain}\n"
        f"Supported: nsf.gov, grants.gov, simpler.grants.gov"
    )


def main():

    from dotenv import load_dotenv
    load_dotenv() 

    parser = argparse.ArgumentParser(
        description="FOA Ingestion Pipeline — Scrape, extract, tag, and export funding opportunities.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # NSF (no API key needed):
  python main.py --url "https://www.nsf.gov/funding/opportunities/.../solicitation" --out_dir ./out

  # Grants.gov (free API key required):
  python main.py --url "https://simpler.grants.gov/opportunity/<UUID>" --api_key "YOUR_KEY" --out_dir ./out
        """,
    )
    parser.add_argument("--url", required=True, help="FOA URL to ingest.")
    parser.add_argument("--out_dir", default="./out", help="Output directory (default: ./out).")
    parser.add_argument(
        "--api_key",
        default=os.environ.get("GRANTS_GOV_API_KEY", ""),
        help="Grants.gov API key (or set GRANTS_GOV_API_KEY env var).",
    )
    args = parser.parse_args()

    print("FOA Ingestion Pipeline")

    print()

    # Detect source
    source = detect_source(args.url)
    print(f"Source detected: {source.upper().replace('_', '.')}")

    #Scrape & extract
    print("Scraping FOA...")
    if source == "nsf":
        foa = scrape_nsf(args.url)
    elif source == "grants_gov":
        if not args.api_key:
            print("ERROR: Grants.gov requires an API key.", file=sys.stderr)
            sys.exit(1)         
        foa = scrape_grants_gov(args.url, args.api_key)

    # Apply semantic tags
    print("Applying semantic tags...")
    foa = apply_tags(foa)
    print(f"  Tags applied:")
    print(f"    Research domains:    {foa.tags.research_domains}")
    print(f"    Methods/approaches:  {foa.tags.methods_approaches}")
    print(f"    Populations:         {foa.tags.populations}")
    print(f"    Sponsor themes:      {foa.tags.sponsor_themes}")

    # Export
    print(f"Exporting to {args.out_dir}/...")
    json_path, csv_path = export(foa, args.out_dir)
    print(f"  ✓ {json_path}")
    print(f"  ✓ {csv_path}")

    print()
    print("  Done! FOA successfully ingested and exported.")

if __name__ == "__main__":
    main()
