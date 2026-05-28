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

# from utils.fetchers import fetch_pubmed, fetch_rxiv, fetch_arxiv, fetch_semantic_scholar
# ── Configuration ─────────────────────────────────────────────────────────────

LOOKBACK_DAYS = 1  # how many days back to search across all sources
TOP_N = 20  # number of full cards to show

EMAIL_FROM = "bionewsdigest@gmail.com"
EMAIL_TO = ["bionewsdigest@gmail.com"] #, "ychen124@uw.edu"




# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class Paper:
    title: str
    authors: str
    abstract: str
    url: str
    source: str
    date: str
    category: str = ""
    matched_topics: list[str] = field(default_factory=list)
    matched_authors: list[str] = field(default_factory=list)

# ── Fetchers ──────────────────────────────────────────────────────────────────
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
        abstract = "".join(abstract_el.itertext())[:500] if abstract_el is not None else ""

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


#####  fetch_rxiv for 1-day lookback (single page, no pagination needed)
def fetch_rxiv(server: str, categories: set[str]) -> list[Paper]:
    yesterday = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    today = date.today().isoformat()
    r = requests.get(
        f"https://api.biorxiv.org/details/{server}/{yesterday}/{today}/1/json",
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

# def fetch_rxiv(server: str, categories: set[str]) -> list[Paper]:
#     yesterday = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
#     today = date.today().isoformat()

#     papers = []
#     offset = 0
#     while True:
#         r = requests.get(
#             f"https://api.biorxiv.org/details/{server}/{yesterday}/{today}/{offset}/json",
#             timeout=60,
#         )
#         r.raise_for_status()
#         data = r.json()
#         total = int(data.get("messages", [{}])[0].get("total", 0))
#         collection = data.get("collection") or []
#         if not collection:
#             break

#         for item in collection:
#             if item.get("category", "").lower() not in categories:
#                 continue
#             doi = item.get("doi", "")
#             papers.append(Paper(
#                 title=item.get("title", ""),
#                 authors=item.get("authors", ""),
#                 abstract=item.get("abstract", "")[:500],
#                 url=f"https://doi.org/{doi}" if doi else "",
#                 source="bioRxiv" if server == "biorxiv" else "medRxiv",
#                 date=item.get("date", ""),
#                 category=item.get("category", "").title(),
#             ))

#         offset += len(collection)
#         if offset >= total:
#             break
#         time.sleep(0.5)

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
        abstract = entry.findtext("atom:summary", "", ns).strip()[:500]
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
            abstract=(item.get("abstract") or "")[:500],
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


# ── Tagging ───────────────────────────────────────────────────────────────────
def tag_papers(papers: list[Paper]) -> None:
    """Detect matched topics and watched authors for each paper."""
    import re

    # Build per-author regex patterns using full display names:
    #   "Xiangdong Zhang"  (First Last — bioRxiv, arXiv, Semantic Scholar)
    #   "Zhang Xiangdong"  (Last First — PubMed fetch format)
    author_patterns: list[tuple[str, list[str]]] = []
    for display in AUTHORS_DISPLAY:
        parts = display.split()
        first, last = parts[0], parts[-1]
        patterns = [
            r'\b' + re.escape(display) + r'\b',                              # full name: "Kao Jung Chang"
            r'\b' + re.escape(last) + r'\s+' + re.escape(first) + r'\b',    # Last First: "Chang Kao"
        ]
        author_patterns.append((display, patterns))

    for p in papers:
        text = (p.title + " " + p.abstract).lower()
        p.matched_topics = [t for t in ALL_TOPICS if t in text]
        authors_lower = p.authors.lower()
        p.matched_authors = [
            display_name
            for display_name, patterns in author_patterns
            if any(re.search(pat, authors_lower, re.IGNORECASE) for pat in patterns)
        ]


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_paper(p: Paper) -> int:
    score = 0
    score += len(p.matched_authors) * 3
    # Topics scored by priority: first in QUERY_TOPICS = highest points
    n = len(QUERY_TOPICS)
    for topic in p.matched_topics:
        if topic in QUERY_TOPICS:
            score += (n - QUERY_TOPICS.index(topic)) * 2
        else:
            score += 1  # broader ALL_TOPICS terms (e.g. "machine learning", "transformers")
    if p.source == "PubMed":
        score += 1
    return score


# ── Claude ranking ────────────────────────────────────────────────────────────

INTEREST_DESCRIPTION = """
I am a researcher focused on:
- Multimodal research in biomedicine, especially using routine laboratory tests. For example, machine learning applied to hematologic disorders and blood cell analysis
- Any research related to cell population data
- Computational methods for flow cytometry and cell population data (my primary interest)
- Geometric deep learning: point cloud methods, permutation-invariant architectures
- Self-supervised learning: masked autoencoders, contrastive learning
- Large language models and foundation models applied to clinical/genomic data
- High-impact biomedical research in genomics, bioinformatics, precision medicine
- Work by my tracked authors in ML for health/biology

Rank 1: papers directly relevant to machine learning, multimodal models in biology and medicine. 
Rank 2: hematology, cell population data, flow cytometry;
Rank 3: papers introducing novel ML architectures applicable to set/point-cloud data;
Rank 4: pure clinical trials without computational novelty, papers only tangentially
related to my areas, but overall it gives me a better understanding about the research in biotech.
"""


def rank_with_claude(papers: list[Paper]) -> list[Paper]:
    """Use Claude to rank papers by relevance to user interests.
    Must be called after tag_papers() so matched_authors is populated.
    Returns papers sorted from most to least relevant.
    Falls back to heuristic scoring on any error.
    """
    if not papers:
        return papers

    client = anthropic.Anthropic()

    # Build a compact numbered list: title + abstract + matched authors/topics
    lines = []
    for i, p in enumerate(papers): #QQQ 200: take 200 characters from each abstract
        abstract = p.abstract[:200].replace("\n", " ") if p.abstract else ""
        meta_parts = []
        if p.matched_authors:
            meta_parts.append("watched authors: " + ", ".join(p.matched_authors))
        if p.matched_topics:
            meta_parts.append("topics: " + ", ".join(p.matched_topics))
        meta = " | " + "; ".join(meta_parts) if meta_parts else ""
        lines.append(f"[{i}] {p.title}{meta} | {abstract}")
    papers_text = "\n".join(lines)

    prompt = f"""You are helping me rank today's paper digest by relevance to my research interests.

MY INTERESTS:
{INTEREST_DESCRIPTION.strip()}

PAPERS (index | title | matched signals | abstract excerpt):
{papers_text}

Return ONLY a JSON array of indices ordered from most to least relevant.
Example: [3, 17, 0, 5, ...]
Include every index exactly once. No explanation, just the JSON array."""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text.strip()
        # Extract JSON array (may be wrapped in markdown code block)
        if "```" in response_text:
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
        ranked_indices = json.loads(response_text)
        # Validate: must be a permutation of range(len(papers))
        if sorted(ranked_indices) != list(range(len(papers))):
            raise ValueError("Returned indices don't match paper list")
        print(f"  Claude ranked {len(ranked_indices)} papers")
        return [papers[i] for i in ranked_indices]
    except Exception as e:
        print(f"  Claude ranking failed ({e}), falling back to heuristic scoring")
        return sorted(papers, key=score_paper, reverse=True)


# ── Deduplication ─────────────────────────────────────────────────────────────

def dedup(papers: list[Paper]) -> list[Paper]:
    seen: set[str] = set()
    result = []
    for p in papers:
        key = p.title.lower().strip()[:80]
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


# ── Email ─────────────────────────────────────────────────────────────────────




def make_badges(p: Paper) -> str:
    badges = f'<span style="background:#2c5f9e;color:#fff;font-size:11px;padding:2px 7px;border-radius:10px;margin-right:4px">{p.source}</span>'
    if p.category:
        badges += f'<span style="background:#c1670c;color:#fff;font-size:11px;padding:2px 7px;border-radius:10px;margin-right:4px">{p.category}</span>'
    for t in p.matched_topics:
        badges += f'<span style="background:#2a7a4b;color:#fff;font-size:11px;padding:2px 7px;border-radius:10px;margin-right:4px">{t}</span>'
    for a in p.matched_authors:
        badges += f'<span style="background:#7b4ea0;color:#fff;font-size:11px;padding:2px 7px;border-radius:10px;margin-right:4px">{a}</span>'
    return badges


def build_html(papers: list[Paper]) -> str:
    top = papers[:TOP_N]
    rest = papers[TOP_N:]

    today_str = date.today().strftime("%A, %B %d, %Y")
    html = f"""
<html><body style="font-family:Georgia,serif;max-width:780px;margin:32px auto;color:#222;line-height:1.5">
<h1 style="font-size:24px;border-bottom:3px solid #2c5f9e;padding-bottom:10px;color:#2c5f9e">
    Daily Paper Digest &mdash; {today_str}
</h1>
<p style="color:#666;font-size:14px">{len(papers)} papers total &mdash; look-back {LOOKBACK_DAYS} days; showing top {len(top)}, plus {len(rest)} additional</p>

<h2 style="font-size:18px;color:#2c5f9e;margin-top:24px;border-bottom:1px solid #ddd;padding-bottom:4px">
    Top Papers ({len(top)})
</h2>
"""
    for p in top:
        abstract_snippet = (p.abstract + "…") if p.abstract else ""
        html += f"""
<div style="margin-bottom:24px;padding-left:12px;border-left:3px solid #e0e8f5">
    <a href="{p.url}" style="font-size:15px;font-weight:bold;color:#1a1a1a;text-decoration:none">
        {p.title}
    </a><br>
    <div style="margin:4px 0">{make_badges(p)}</div>
    <span style="color:#777;font-size:12px">{p.authors}</span><br>
    <span style="color:#444;font-size:13px">{abstract_snippet}</span>
</div>"""

    if rest:
        html += f"""
<h2 style="font-size:16px;color:#888;margin-top:36px;border-bottom:1px solid #eee;padding-bottom:4px">
    Also Published ({len(rest)})
</h2>
<table style="width:100%;border-collapse:collapse;font-size:13px">"""
        for p in rest:
            source_badge = f'<span style="background:#2c5f9e;color:#fff;font-size:10px;padding:1px 6px;border-radius:8px">{p.source}</span>'
            html += f"""
<tr style="border-bottom:1px solid #f0f0f0">
    <td style="padding:6px 8px 6px 0;width:80%">
        <a href="{p.url}" style="color:#333;text-decoration:none">{p.title}</a>
    </td>
    <td style="padding:6px 0;white-space:nowrap">{source_badge}</td>
</tr>"""
        html += "</table>"

    start_str = (date.today() - timedelta(days=LOOKBACK_DAYS)).strftime("%b %d, %Y")
    end_str = date.today().strftime("%b %d, %Y")
    html += f"""
<p style="margin-top:40px;padding-top:12px;border-top:1px solid #eee;color:#aaa;font-size:12px">
    Papers extracted from {start_str} to {end_str} ({LOOKBACK_DAYS}-day window) &mdash;
    Sources: PubMed, bioRxiv, medRxiv, arXiv, Semantic Scholar
</p>
</body></html>"""
    return html


def send_email(html: str, paper_count: int) -> None:
    password = os.environ["GMAIL_PASSWORD"]
    today_str = date.today().strftime("%b %d, %Y")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Paper Digest {today_str} — {paper_count} new papers"
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(EMAIL_TO)
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_FROM, password)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    print(f"Email sent: {paper_count} papers")


# ── Main ──────────────────────────────────────────────────────────────────────

def fetch_safe(label: str, fn, *args) -> list[Paper]:
    """Call a fetch function, returning [] and logging on any error."""
    try:
        results = fn(*args)
        print(f"  {len(results)} papers")
        return results
    except Exception as e:
        print(f"  ERROR: {e}")
        return []


def main() -> None:
    print("Fetching PubMed...")
    pubmed = fetch_safe("PubMed", fetch_pubmed)

    print("Fetching bioRxiv...")
    biorxiv = fetch_safe("bioRxiv", fetch_rxiv, "biorxiv", BIORXIV_CATS)

    print("Fetching medRxiv...")
    medrxiv = fetch_safe("medRxiv", fetch_rxiv, "medrxiv", MEDRXIV_CATS)

    print("Fetching arXiv...")
    arxiv = fetch_safe("arXiv", fetch_arxiv)

    print("Fetching Semantic Scholar...")
    semantic = fetch_safe("Semantic Scholar", fetch_semantic_scholar)

    all_papers = dedup(pubmed + biorxiv + medrxiv + arxiv + semantic)
    print(f"Total after dedup: {len(all_papers)}")
    tag_papers(all_papers)

    print("Ranking with Claude...")
    ranked_papers = rank_with_claude(all_papers)

    html = build_html(ranked_papers)
    send_email(html, len(ranked_papers))


if __name__ == "__main__":
    from datetime import datetime
    print(f"\n{'='*50}")
    print(f"Run started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")
    main()
    print(f"Run finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
