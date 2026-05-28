# Daily Paper Digest Workflow

Automated morning workflow that fetches new papers from multiple sources and delivers a curated digest based on topics, authors, and journals.

## Sources

| Source | API | Auth Required |
|--------|-----|---------------|
| PubMed | MCP (`mcp__claude_ai_PubMed`) | No |
| bioRxiv | `https://api.biorxiv.org/details/biorxiv/{interval}/{cursor}/json` | No |
| medRxiv | `https://api.biorxiv.org/details/medrxiv/{interval}/{cursor}/json` | No |
| arXiv | `https://export.arxiv.org/api/query?search_query=...` | No |

## Configuration

### Topics / Keywords
`parameters/topics.py`

### Authors to Follow
`parameters/authors.py`

### Journals to Follow (PubMed only)
`parameters/pubmed_journals.py`

### arXiv, medRxiv, bioRxiv Categories
`parameters/arxiv.py`

## Delivery

- **Format:** Email digest (grouped by source, title + authors + abstract snippet + link)
- **Recipient:** <email-to-receive>
- **Sender:** <email-to-send-summary> (credentials stored as `GMAIL_PASSWORD` in `.env`)
- **Schedule:** Monday–Friday at 7:11AM PT (Pacific Time)

## Relevance Scoring

Use Anthropic semantic search, fall back to heuristics.
Heuristics: Papers are ranked by score. Top `TOP_N` (default: 20) get full cards with abstract; the rest appear as a compact list.

| Signal | Points |
|--------|--------|
| Matched a watched author | +3 per author |
| Matched a topic keyword | +2 × (n − index) where index = position in `QUERY_TOPICS` (first = highest) |
| From PubMed (peer-reviewed) | +1 |
| Came in only via journal/category | +0 |

## Workflow Steps

1. Query PubMed for papers published in the last 24h matching criteria
2. Query bioRxiv API for new preprints matching criteria
3. Query medRxiv API for new preprints matching criteria
4. Query arXiv API for new papers in specified categories matching criteria
5. Deduplicate results (DOI / title matching)
6. Format and deliver digest to configured destination
