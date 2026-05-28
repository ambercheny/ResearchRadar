#!/usr/bin/env python3
"""Daily paper digest: PubMed, bioRxiv, medRxiv, arXiv → email."""

import os
import smtplib
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import json

import anthropic
import requests
from dotenv import load_dotenv
from typing import Optional

load_dotenv()
from parameters.arxiv import ARXIV_CATS, BIORXIV_CATS, MEDRXIV_CATS
from parameters.authors import AUTHORS_DISPLAY
from parameters.query_topics import QUERY_TOPICS
from parameters.pubmed_journals import PUBMED_JOURNALS
from parameters.query_topics import QUERY_TOPICS
from parameters.topics import ALL_TOPICS


def fetch_pubmed() -> list[Paper]:
    """Query PubMed for recent papers matching topics, authors, or journals."""
    author_q = " OR ".join(f'"{a}"[Author]' for a in AUTHORS_DISPLAY)
    topic_q = " OR ".join(f'"{t}"[Title/Abstract]' for t in QUERY_TOPICS)
    journal_q = " OR ".join(PUBMED_JOURNALS)
    query = f"(({topic_q}) AND ({journal_q})) OR ({author_q})"
    print(f"pubmed query: {query}")

    r = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={"db": "pubmed", "term": query, "retmax": 100,
                "sort": "date", "retmode": "json",
                "reldate": LOOKBACK_DAYS, "datetype": "pdat"},
        timeout=30,
    )
    r.raise_for_status()
    ids = r.json()["esearchresult"]["idlist"]
    if not ids:
        return []

    time.sleep(0.4)  # NCBI rate limit: max 3 requests/sec without API key

    r = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params={"db": "pubmed", "id": ",".join(ids), "retmode": "xml"},
        timeout=30,
    )
    r.raise_for_status()

    papers = []
    root = ET.fromstring(r.content)
    for article in root.findall(".//PubmedArticle"):
        title_el = article.find(".//ArticleTitle")
        title = "".join(title_el.itertext()) if title_el is not None else "No title"

        authors_list = []
        for author in article.findall(".//Author"):
            ln = author.findtext("LastName", "")
            fn = author.findtext("ForeName", "")
            if ln:
                authors_list.append(f"{ln} {fn}".strip())
        authors_str = ", ".join(authors_list[:5])
        if len(authors_list) > 5:
            authors_str += " et al."

        abstract_el = article.find(".//AbstractText")
        abstract = "".join(abstract_el.itertext())[:300] if abstract_el is not None else ""

        pmid = article.findtext(".//PMID", "")
        pub_date = article.find(".//PubDate")
        date_str = ""
        if pub_date is not None:
            parts = [pub_date.findtext(f, "") for f in ("Year", "Month", "Day")]
            date_str = " ".join(p for p in parts if p)

        papers.append(Paper(
            title=title, authors=authors_str, abstract=abstract,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            source="PubMed", date=date_str,
        ))
    return papers


def fetch_rxiv(server: str, categories: set[str]) -> list[Paper]:
    """Fetch recent preprints from bioRxiv or medRxiv, filtered by category."""
    yesterday = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    today = date.today().isoformat()
    r = requests.get(
        f"https://api.biorxiv.org/details/{server}/{yesterday}/{today}/0/json",
        timeout=60,
    )
    r.raise_for_status()

    papers = []
    for item in r.json().get("collection", []):
        if item.get("category", "").lower() not in categories:
            continue
        doi = item.get("doi", "")
        papers.append(Paper(
            title=item.get("title", ""),
            authors=item.get("authors", ""),
            abstract=item.get("abstract", "")[:300],
            url=f"https://doi.org/{doi}" if doi else "",
            source="bioRxiv" if server == "biorxiv" else "medRxiv",
            date=item.get("date", ""),
            category=item.get("category", "").title(),
        ))
    return papers


def fetch_arxiv() -> list[Paper]:
    """Fetch recent papers from arXiv in specified categories matching topics or authors."""
    start_date = (date.today() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    cutoff_iso = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()  # YYYY-MM-DD for comparison
    cat_q = " OR ".join(f"cat:{c}" for c in ARXIV_CATS)
    topic_q = " OR ".join(f'abs:"{t}"' for t in QUERY_TOPICS)
    author_q = " OR ".join(f"au:{a}" for a in AUTHORS_DISPLAY)
    date_filter = f"submittedDate:[{start_date}0000 TO {date.today().strftime('%Y%m%d')}2359]"
    search_query = f"({cat_q}) AND (({topic_q}) OR ({author_q})) AND {date_filter}"
     

    for attempt in range(3):
        r = requests.get(
            "https://export.arxiv.org/api/query",
            params={
                "search_query": search_query,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": 100,
            },
            timeout=30,
        )
        if r.status_code == 429:
            wait = 30 * (attempt + 1)
            print(f"  arXiv rate limited, retrying in {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        break
    else:
        raise RuntimeError("arXiv rate limit: gave up after 3 attempts")

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(r.content)
    papers = []
    for entry in root.findall("atom:entry", ns):
        published = entry.findtext("atom:published", "", ns)[:10]  # YYYY-MM-DD
        if published < cutoff_iso:
            continue
        title = entry.findtext("atom:title", "", ns).strip().replace("\n", " ")
        authors_list = [a.findtext("atom:name", "", ns)
                        for a in entry.findall("atom:author", ns)]
        authors_str = ", ".join(authors_list[:5])
        if len(authors_list) > 5:
            authors_str += " et al."
        abstract = entry.findtext("atom:summary", "", ns).strip()[:300]
        url = entry.findtext("atom:id", "", ns).strip()
        cat_el = entry.find("atom:category", ns)
        category = cat_el.get("term", "") if cat_el is not None else ""
        papers.append(Paper(title=title, authors=authors_str, abstract=abstract,
                            url=url, source="arXiv", date=published, category=category))
    return papers


# ── Semantic Scholar ──────────────────────────────────────────────────────────

def fetch_semantic_scholar() -> list[Paper]:
    # Optional — get a free key at https://www.semanticscholar.org/product/api
    SEMANTIC_SCHOLAR_API_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
    """Fetch recent papers from Semantic Scholar by watched authors and topics."""
    yesterday = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    headers = {"x-api-key": SEMANTIC_SCHOLAR_API_KEY} if SEMANTIC_SCHOLAR_API_KEY else {}
    base = "https://api.semanticscholar.org/graph/v1"
    fields = "title,authors,abstract,year,publicationDate,externalIds,openAccessPdf"
    seen_ids: set[str] = set()
    papers = []

    def make_paper(item: dict, matched_author: str = "") -> Optional[Paper]:
        paper_id = item.get("paperId", "")
        if not paper_id or paper_id in seen_ids:
            return None
        seen_ids.add(paper_id)
        pub_date = item.get("publicationDate") or ""
        if not pub_date or pub_date < yesterday:
            return None
        authors_list = [a.get("name", "") for a in item.get("authors", [])]
        authors_str = ", ".join(authors_list[:5]) + (" et al." if len(authors_list) > 5 else "")
        ext_ids = item.get("externalIds") or {}
        pdf = item.get("openAccessPdf") or {}
        url = pdf.get("url") or ""
        if not url and ext_ids.get("DOI"):
            url = f"https://doi.org/{ext_ids['DOI']}"
        elif not url and ext_ids.get("PubMed"):
            url = f"https://pubmed.ncbi.nlm.nih.gov/{ext_ids['PubMed']}/"
        return Paper(
            title=item.get("title") or "",
            authors=authors_str,
            abstract=(item.get("abstract") or "")[:300],
            url=url,
            source="Semantic Scholar",
            date=pub_date,
        )

    # ── Fetch by author ────────────────────────────────────────────────────────
    for author_name in AUTHORS_DISPLAY:
        # Step 1: resolve author ID
        r = requests.get(f"{base}/author/search",
                         params={"query": author_name, "fields": "authorId,name"},
                         headers=headers, timeout=30)
        time.sleep(1.2)
        if not r.ok or not r.json().get("data"):
            continue
        author_id = r.json()["data"][0]["authorId"]

        # Step 2: get their recent papers
        r = requests.get(f"{base}/author/{author_id}/papers",
                         params={"fields": fields, "limit": 10},
                         headers=headers, timeout=30)
        time.sleep(1.2)
        if not r.ok:
            continue
        for item in r.json().get("data", []):
            p = make_paper(item, matched_author=author_name)
            if p:
                papers.append(p)

    # ── Fetch by topic ─────────────────────────────────────────────────────────
    for topic in QUERY_TOPICS:
        r = requests.get(f"{base}/paper/search",
                         params={"query": topic, "fields": fields,
                                 "publicationDateOrYear": f"{yesterday}:", "limit": 20},
                         headers=headers, timeout=30)
        time.sleep(1.2)
        if not r.ok:
            continue
        for item in r.json().get("data", []):
            p = make_paper(item)
            if p:
                papers.append(p)

    return papers

