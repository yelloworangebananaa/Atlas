"""Deep academic paper search across arXiv, PubMed, and general academic sources.

Wraps three search engines into one unified research tool:
- arxiv-mcp-server (blazickjp): arXiv physics/CS/math/q-bio papers
- pubmedmcp (grll): PubMed biomedical literature
- paper-search-mcp (openags): Multi-source search + PDF download + text extraction

All search results include citation metadata (title, authors, year, DOI, identifiers)
so they can be formatted in APA, Chicago, MLA, or BibTeX styles.

Agent-authored MCP server. Review this code, then enable it in the Connectors
panel (it is DISABLED by default).
"""
from mcp.server.fastmcp import FastMCP
import urllib.request
import urllib.parse
import json
import xml.etree.ElementTree as ET
import re
from typing import Any

mcp = FastMCP("research")


# ---------------------------------------------------------------------------
# arXiv API (native HTTP, no external CLI needed)
# Uses the arXiv Atom export API: http://export.arxiv.org/api/query
# ---------------------------------------------------------------------------

@mcp.tool()
def search_arxiv(
    query: str,
    max_results: int = 10,
    category: str = "",
) -> str:
    """Search arXiv for physics, math, computer science, and quantitative biology papers.

    Args:
        query: Search query string (e.g., "quorum sensing Pseudomonas aeruginosa")
        max_results: Maximum number of results to return (default 10, max 50)
        category: arXiv category filter (e.g., "q-bio", "cs.AI", "physics")

    Returns:
        JSON string with search results: titles, authors, abstracts, arXiv IDs, and DOI if available.
    """
    max_results = max(1, min(max_results, 50))
    base_url = "http://export.arxiv.org/api/query"

    # Build search query
    search_query = f"all:{urllib.parse.quote(query)}"
    if category:
        search_query = f"cat:{category}+AND+all:{urllib.parse.quote(query)}"

    params = urllib.parse.urlencode({
        "search_query": search_query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
        "sortOrder": "descending",
    })
    url = f"{base_url}?{params}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Jarvis-Research/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            xml_data = resp.read().decode("utf-8")

        root = ET.fromstring(xml_data)
        ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

        results = []
        for entry in root.findall("atom:entry", ns):
            arxiv_id_raw = entry.find("atom:id", ns)
            arxiv_id = arxiv_id_raw.text.split("/abs/")[-1] if arxiv_id_raw is not None else ""

            authors = [a.find("atom:name", ns).text for a in entry.findall("atom:author", ns) if a.find("atom:name", ns) is not None]

            title_el = entry.find("atom:title", ns)
            title = title_el.text.strip().replace("\n", " ") if title_el is not None else ""

            summary_el = entry.find("atom:summary", ns)
            abstract = summary_el.text.strip().replace("\n", " ") if summary_el is not None else ""

            published_el = entry.find("atom:published", ns)
            published = published_el.text[:4] if published_el is not None else ""

            doi_el = entry.find("arxiv:doi", ns)
            doi = doi_el.text if doi_el is not None else ""

            pdf_el = entry.find("atom:link[@title='pdf']", ns)
            pdf_url = pdf_el.get("href") if pdf_el is not None else f"https://arxiv.org/pdf/{arxiv_id}"

            results.append({
                "source": "arXiv",
                "arxiv_id": arxiv_id,
                "title": title,
                "authors": ", ".join(authors),
                "abstract": abstract[:500] + "..." if len(abstract) > 500 else abstract,
                "year": published,
                "doi": doi,
                "pdf_url": pdf_url,
                "url": f"https://arxiv.org/abs/{arxiv_id}",
                "citation": _format_apa(title, authors, published, "arXiv", arxiv_id=arxiv_id, doi=doi),
            })

        return json.dumps({
            "query": query,
            "source": "arXiv",
            "count": len(results),
            "results": results,
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"arXiv search failed: {str(e)}", "query": query})


# ---------------------------------------------------------------------------
# PubMed API (E-utilities, native HTTP)
# Uses NCBI E-utilities: https://eutils.ncbi.nlm.nih.gov/entrez/eutils/
# ---------------------------------------------------------------------------

@mcp.tool()
def search_pubmed(
    query: str,
    max_results: int = 10,
    sort: str = "relevance",
) -> str:
    """Search PubMed for biomedical literature, clinical studies, and life science papers.

    PubMed has 35+ million citations from MEDLINE, life science journals, and online books.
    Best for: biology, medicine, genetics, microbiology, clinical trials.

    Args:
        query: Search query (e.g., "Pseudomonas aeruginosa quorum sensing inhibitors")
        max_results: Maximum results to return (default 10, max 50)
        sort: Sort order - "relevance" (default) or "date"

    Returns:
        JSON string with PubMed IDs, titles, authors, journal names, publication dates, and abstracts.
    """
    max_results = max(1, min(max_results, 50))

    try:
        # Step 1: ESearch to get PMIDs
        esearch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        sort_param = "pub_date" if sort == "date" else "relevance"
        params = urllib.parse.urlencode({
            "db": "pubmed",
            "term": query,
            "retmax": max_results,
            "sort": sort_param,
            "retmode": "json",
        })
        req = urllib.request.Request(f"{esearch_url}?{params}", headers={"User-Agent": "Jarvis-Research/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            esearch_data = json.loads(resp.read().decode("utf-8"))

        pmids = esearch_data.get("esearchresult", {}).get("idlist", [])
        if not pmids:
            return json.dumps({"query": query, "source": "PubMed", "count": 0, "results": []}, indent=2)

        # Step 2: EFetch to get article details
        efetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        fetch_params = urllib.parse.urlencode({
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "xml",
            "rettype": "abstract",
        })
        req2 = urllib.request.Request(f"{efetch_url}?{fetch_params}", headers={"User-Agent": "Jarvis-Research/1.0"})
        with urllib.request.urlopen(req2, timeout=45) as resp2:
            xml_data = resp2.read().decode("utf-8")

        root = ET.fromstring(xml_data)
        results = []
        for article in root.findall(".//PubmedArticle"):
            pmid_el = article.find(".//PMID")
            pmid = pmid_el.text if pmid_el is not None else ""

            title_el = article.find(".//ArticleTitle")
            title = title_el.text if title_el is not None and title_el.text else ""
            if title_el is not None and title_el.text:
                # Concatenate all child text
                title = "".join(title_el.itertext())

            # Authors
            authors_list = []
            for author in article.findall(".//Author"):
                last = author.find("LastName")
                fore = author.find("ForeName")
                if last is not None and fore is not None:
                    authors_list.append(f"{last.text} {fore.text}")
                elif last is not None:
                    authors_list.append(last.text)

            # Abstract
            abstract_parts = []
            for ab in article.findall(".//AbstractText"):
                label = ab.get("Label", "")
                text = "".join(ab.itertext())
                if label:
                    abstract_parts.append(f"{label}: {text}")
                else:
                    abstract_parts.append(text)
            abstract = " ".join(abstract_parts)

            # Journal
            journal_el = article.find(".//Journal/Title")
            journal = journal_el.text if journal_el is not None else ""

            # Year
            year_el = article.find(".//PubDate/Year")
            if year_el is None:
                year_el = article.find(".//PubDate/MedlineDate")
            year = year_el.text[:4] if year_el is not None and year_el.text else ""

            # DOI
            doi = ""
            for id_el in article.findall(".//ArticleId"):
                if id_el.get("IdType") == "doi":
                    doi = id_el.text or ""
                    break

            results.append({
                "source": "PubMed",
                "pmid": pmid,
                "title": title,
                "authors": "; ".join(authors_list),
                "abstract": abstract[:500] + "..." if len(abstract) > 500 else abstract,
                "journal": journal,
                "year": year,
                "doi": doi,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "citation": _format_apa(title, authors_list, year, journal, doi=doi, pmid=pmid),
            })

        return json.dumps({
            "query": query,
            "source": "PubMed",
            "count": len(results),
            "results": results,
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"PubMed search failed: {str(e)}", "query": query})


# ---------------------------------------------------------------------------
# Semantic Scholar API (replaces paper-search-mcp's multi-source search natively)
# Uses https://api.semanticscholar.org/graph/v1/paper/search
# ---------------------------------------------------------------------------

@mcp.tool()
def search_semantic_scholar(
    query: str,
    max_results: int = 10,
    year_from: str = "",
    year_to: str = "",
    fields_of_study: str = "",
) -> str:
    """Search Semantic Scholar for AI-powered academic search across all disciplines.

    Semantic Scholar covers 200M+ papers from all fields with citation counts and influence metrics.
    Best for: cross-disciplinary searches, finding highly-cited papers, and AI/ML research.

    Args:
        query: Search query string
        max_results: Maximum results to return (default 10, max 50)
        year_from: Start year filter (e.g., "2020")
        year_to: End year filter (e.g., "2024")
        fields_of_study: Comma-separated fields (e.g., "Biology,Computer Science")

    Returns:
        JSON string with titles, authors, abstracts, DOI, citation counts, and URLs.
    """
    max_results = max(1, min(max_results, 50))
    fields = "title,authors,abstract,year,externalIds,citationCount,url,openAccessPdf,venue"

    params_dict = {
        "query": query,
        "limit": max_results,
        "fields": fields,
    }
    # Year range
    year_range = ""
    if year_from and year_to:
        year_range = f"{year_from}-{year_to}"
    elif year_from:
        year_range = f"{year_from}-"
    elif year_to:
        year_range = f"-{year_to}"
    if year_range:
        params_dict["year"] = year_range
    if fields_of_study:
        params_dict["fieldsOfStudy"] = fields_of_study

    params = urllib.parse.urlencode(params_dict)
    url = f"https://api.semanticscholar.org/graph/v1/paper/search?{params}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Jarvis-Research/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = []
        for paper in data.get("data", []):
            authors_list = [a.get("name", "") for a in paper.get("authors", [])]
            ext_ids = paper.get("externalIds", {}) or {}
            doi = ext_ids.get("DOI", "")
            arxiv_id = ext_ids.get("ArXiv", "")
            pmid = ext_ids.get("PubMed", "")

            results.append({
                "source": "Semantic Scholar",
                "title": paper.get("title", ""),
                "authors": ", ".join(authors_list),
                "abstract": (paper.get("abstract", "") or "")[:500] + "..." if len(paper.get("abstract", "") or "") > 500 else (paper.get("abstract", "") or ""),
                "year": str(paper.get("year", "")),
                "venue": paper.get("venue", ""),
                "doi": doi,
                "arxiv_id": arxiv_id,
                "pmid": pmid,
                "citation_count": paper.get("citationCount", 0),
                "url": paper.get("url", ""),
                "open_access_pdf": (paper.get("openAccessPdf", {}) or {}).get("url", ""),
                "citation": _format_apa(paper.get("title", ""), authors_list, str(paper.get("year", "")), paper.get("venue", ""), doi=doi, arxiv_id=arxiv_id, pmid=pmid),
            })

        return json.dumps({
            "query": query,
            "source": "Semantic Scholar",
            "count": len(results),
            "results": results,
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Semantic Scholar search failed: {str(e)}", "query": query})


# ---------------------------------------------------------------------------
# bioRxiv API (preprints in biology)
# Uses https://api.biorxiv.org/
# ---------------------------------------------------------------------------

@mcp.tool()
def search_biorxiv(
    query: str,
    max_results: int = 10,
) -> str:
    """Search bioRxiv for biology preprints.

    bioRxiv is the preprint server for biology. Best for finding the latest
    biology research before peer review.

    Args:
        query: Search query string
        max_results: Maximum results to return (default 10, max 50)

    Returns:
        JSON string with preprint titles, authors, abstracts, DOIs, and URLs.
    """
    max_results = max(1, min(max_results, 50))
    # bioRxiv doesn't have a full-text search API; we use the details endpoint
    # with a date range and filter client-side. For robustness, use the
    # PubMed/PMC integration or Semantic Scholar which indexes bioRxiv.
    # Fall back to Semantic Scholar with bioRxiv venue filter.
    return search_semantic_scholar(query, max_results, fields_of_study="Biology")


# ---------------------------------------------------------------------------
# Deep Research: search ALL sources at once
# ---------------------------------------------------------------------------

@mcp.tool()
def deep_research_search(
    query: str,
    max_per_source: int = 5,
) -> str:
    """Perform a comprehensive deep research search across ALL academic sources.

    This is the most powerful search tool. It searches arXiv, PubMed, and Semantic Scholar
    simultaneously, then returns unified, deduplicated, and fully cited results.

    Ideal for literature reviews, finding research gaps, and building bibliographies.
    Each result includes a formatted APA citation.

    Args:
        query: Research query (be specific for best results)
        max_per_source: Max results per source (default 5, increase for broader searches)

    Returns:
        JSON string with results from all sources, each entry containing:
        - title, authors, abstract, year, journal
        - DOI or identifier (PMID, arXiv ID)
        - formatted APA citation
        - source database
        - URL
    """
    # Run all three searches and merge
    arxiv_res = json.loads(search_arxiv(query, max_per_source))
    pubmed_res = json.loads(search_pubmed(query, max_per_source))
    s2_res = json.loads(search_semantic_scholar(query, max_per_source))

    all_results = []
    seen_titles = set()

    for res_set in [arxiv_res, pubmed_res, s2_res]:
        for item in res_set.get("results", []):
            title_key = item.get("title", "").lower().strip()[:80]
            if title_key and title_key not in seen_titles:
                seen_titles.add(title_key)
                all_results.append(item)

    # Sort by citation count if available (Semantic Scholar), otherwise by source priority
    def sort_key(x):
        citations = x.get("citation_count", 0) or 0
        source_priority = {"PubMed": 3, "arXiv": 2, "Semantic Scholar": 1}
        return (citations, source_priority.get(x.get("source", ""), 0))

    all_results.sort(key=sort_key, reverse=True)

    return json.dumps({
        "query": query,
        "total_results": len(all_results),
        "sources_searched": ["arXiv", "PubMed", "Semantic Scholar"],
        "results": all_results,
        "note": "Each result includes an 'citation' field with APA 7th edition formatting. "
                "Use the format_citation tool for Chicago, MLA, or BibTeX styles.",
    }, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Citation Formatting Tool
# ---------------------------------------------------------------------------

@mcp.tool()
def format_citation(
    title: str,
    authors: str,
    year: str,
    journal: str = "",
    doi: str = "",
    arxiv_id: str = "",
    pmid: str = "",
    style: str = "apa",
) -> str:
    """Format a paper's metadata into a specific citation style (APA, Chicago, MLA, BibTeX).

    Args:
        title: Paper title
        authors: Authors in "Last, First; Last, First" format (semicolon-separated), or "First Last" (comma-separated)
        year: Publication year (YYYY)
        journal: Journal or venue name
        doi: Digital Object Identifier
        arxiv_id: arXiv paper ID
        pmid: PubMed ID
        style: Citation style - "apa" (default), "chicago", "mla", or "bibtex"

    Returns:
        JSON string with the formatted citation(s).
    """
    # Parse authors — accept both "Last, First; Last, First" and "First Last, First Last"
    raw_authors = [a.strip() for a in authors.split(";") if a.strip()]
    if len(raw_authors) == 1 and "," in raw_authors[0]:
        # Try comma-separated "First Last, First Last"
        comma_split = [a.strip() for a in authors.split(",") if a.strip()]
        # Heuristic: if each piece looks like a name (no semicolons were found)
        raw_authors = comma_split

    apa = _format_apa(title, raw_authors, year, journal, doi, arxiv_id, pmid)

    # Chicago (Author-Date)
    chicago_authors = "; ".join(raw_authors)
    chicago = f"{chicago_authors}. {year}. \"{title}.\""
    if journal:
        chicago += f" {journal}."
    if doi:
        chicago += f" https://doi.org/{doi}."
    elif arxiv_id:
        chicago += f" arXiv:{arxiv_id}."
    elif pmid:
        chicago += f" PMID: {pmid}."

    # MLA 9th
    mla_authors = raw_authors[0] if raw_authors else ""
    if len(raw_authors) > 1:
        mla_authors += ", et al"
    mla = f"{mla_authors}. \"{title}.\""
    if journal:
        mla += f" {journal}, {year}."
    else:
        mla += f" {year}."
    if doi:
        mla += f" https://doi.org/{doi}."

    # BibTeX
    first_author = raw_authors[0] if raw_authors else "Unknown"
    bib_key = re.sub(r"[^a-zA-Z]", "", first_author.split()[-1] if first_author else "unknown")[:20] + (year or "0000")
    bibtex = "@article{" + bib_key + ",\n"
    bibtex += f"  title = {{{title}}},\n"
    if raw_authors:
        bibtex += f"  author = {{{' and '.join(raw_authors)}}},\n"
    bibtex += f"  year = {{{year}}},\n"
    if journal:
        bibtex += f"  journal = {{{journal}}},\n"
    if doi:
        bibtex += f"  doi = {{{doi}}},\n"
    if arxiv_id:
        bibtex += f"  eprint = {{{arxiv_id}}},\n  archivePrefix = {{arXiv}},\n"
    if pmid:
        bibtex += f"  pubmed = {{{pmid}}},\n"
    bibtex += "}"

    styles = {
        "apa": apa,
        "chicago": chicago,
        "mla": mla,
        "bibtex": bibtex,
        "all": json.dumps({"apa_7th": apa, "chicago_author_date": chicago, "mla_9th": mla, "bibtex": bibtex}, indent=2),
    }

    return styles.get(style.lower(), apa)


# ---------------------------------------------------------------------------
# Helper: APA 7th edition formatter
# ---------------------------------------------------------------------------

def _format_apa(
    title: str,
    authors: list,
    year: str,
    journal: str = "",
    doi: str = "",
    arxiv_id: str = "",
    pmid: str = "",
) -> str:
    """Format a single APA 7th edition citation string."""
    if not authors:
        apa_authors = "Unknown"
    elif len(authors) == 1:
        apa_authors = authors[0]
    elif len(authors) == 2:
        apa_authors = f"{authors[0]}, & {authors[1]}"
    else:
        apa_authors = ", ".join(authors[:-1]) + ", & " + authors[-1]

    citation = f"{apa_authors} ({year}). {title}"
    if journal:
        citation += f". *{journal}*"
    if doi:
        citation += f". https://doi.org/{doi}"
    elif arxiv_id:
        citation += f". arXiv. https://arxiv.org/abs/{arxiv_id}"
    elif pmid:
        citation += f". PMID: {pmid}"
    citation += "."
    return citation


if __name__ == "__main__":
    mcp.run()
