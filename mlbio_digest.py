#!/usr/bin/env python3
"""
mlbio_digest.py
Fetch ML↔biology papers from the last 30 days:
- High-impact journals (Nature/Cell family) via Crossref, ranked by Altmetric (tweets when available)
- Top 2 arXiv ML/biology preprints (by Altmetric when available, else recency)
Post a 7-item digest to Slack via Incoming Webhook.
"""
import os, sys, time, json, math, re
import datetime as dt
from typing import List, Dict, Any, Optional
import requests

DAY = 86400
TOP_VENUES = [
    # --- Core Nature/Cell family ---
    "Nature","Nature Medicine","Nature Biotechnology","Nature Methods","Nature Genetics",
    "Nature Chemical Biology","Nature Machine Intelligence","Nature Communications",
    "Nature Cancer","Nature Immunology","Nature Reviews Cancer","Nature Reviews Immunology",
    "Cell","Cell Reports","Cell Reports Medicine","Cell Systems","Cell Genomics",
    "Immunity","Cancer Cell","Molecular Cell","Cell Host & Microbe",
    # --- Other high-impact oncology / immunology journals ---
    "Science","Science Translational Medicine","Science Immunology",
    "The Lancet Oncology","Cancer Discovery","Cancer Immunology Research",
    "Clinical Cancer Research","JCO Precision Oncology","Nature Reviews Drug Discovery",
    "npj Precision Oncology","npj Systems Biology and Applications",
    "Frontiers in Immunology","Frontiers in Oncology"
]

def iso_date(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

def last_30_window():
    today = dt.datetime.utcnow().date()
    since = today - dt.timedelta(days=30)
    return iso_date(since), iso_date(today)

def fetch_json(url: str, headers=None, tries=3, timeout=25) -> Any:
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers or {"Accept":"application/json"}, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)
        time.sleep(1.2 * (i+1))
    raise RuntimeError(f"Failed GET {url}: {last}")

def fetch_text(url: str, tries=3, timeout=25) -> str:
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)
        time.sleep(1.2 * (i+1))
    raise RuntimeError(f"Failed GET {url}: {last}")

def altmetric_by_doi(doi: str) -> Dict[str, Any]:
    url = f"https://api.altmetric.com/v1/doi/{requests.utils.quote(doi, safe='')}"
    try:
        j = fetch_json(url)
        return {
            "tweets": j.get("cited_by_tweeters_count"),
            "altmetric_score": j.get("score"),
            "altmetric_url": j.get("details_url"),
        }
    except Exception:
        return {}

def altmetric_by_arxiv(arxivid: str) -> Dict[str, Any]:
    url = f"https://api.altmetric.com/v1/arxiv/{requests.utils.quote(arxivid, safe='')}"
    try:
        j = fetch_json(url)
        return {
            "tweets": j.get("cited_by_tweeters_count"),
            "altmetric_score": j.get("score"),
            "altmetric_url": j.get("details_url"),
        }
    except Exception:
        return {}

def score_item(it: Dict[str,Any]) -> float:
    venue_bonus = 10.0 if (it.get("journal") in TOP_VENUES or it.get("source") in TOP_VENUES) else 0.0
    tweets = float(it.get("tweets") or 0.0)
    alt = float(it.get("altmetric_score") or 0.0)
    return venue_bonus + math.log2(1.0 + tweets) + 0.25*math.log2(1.0 + alt)

def first_two_sentences(text: str) -> str:
    if not text:
        return ""
    # naive split on sentence enders; keep up to two
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    if len(parts) >= 2:
        return ' '.join(parts[:2])
    return parts[0]

def fetch_crossref() -> List[Dict[str,Any]]:
    since, until = last_30_window()
    # Query per journal with simpler keyword filter to avoid Crossref parser quirks
    KEYWORDS = [
        "machine learning", "deep learning", "artificial intelligence", "foundation model",
        "neural network", "graph learning", "transformer", "representation learning",
        "immunology", "t cell", "t-cell", "tcr", "neoantigen", "immune checkpoint",
        "tumor microenvironment", "immunotherapy", "checkpoint blockade",
        "cancer", "oncology", "tumor", "antigen", "peptide presentation", "hla",
        "mhc", "tumor-infiltrating lymphocyte", "b cell receptor", "antibody repertoire",
        "spatial omics", "proteomics", "immunopeptidomics", "cytometry", "cell phenotype"
    ]
    EXCLUDE_KEYWORDS = [
        "embryology", "embryonic development", "morphogenesis", "developmental biology"
    ]
    JOURNALS = [
        "Nature","Nature Medicine","Nature Biotechnology","Nature Methods","Nature Genetics",
        "Nature Chemical Biology","Nature Machine Intelligence","Nature Communications",
        "Cell","Cell Reports","Cell Systems","Immunity","Cancer Cell","Molecular Cell",
        "Cell Genomics","Cell Host & Microbe"
    ]

    results = []
    headers = {"Accept":"application/json","User-Agent":"mlbio-digest/1.0 (mailto:you@example.com)"}

    for jname in JOURNALS:
        # Start with broad date filter; keep rows modest to reduce latency
        base = ( "https://api.crossref.org/works"
                 f"?filter=from-pub-date:{since},until-pub-date:{until},type:journal-article,container-title:{requests.utils.quote(jname, safe='')}"
                 "&select=DOI,title,container-title,author,abstract,URL,created,issued"
                 "&rows=20" )
        # Pull once without keywords (fast), then keyword-filter locally
        try:
            j = fetch_json(base, headers=headers)
        except Exception as e:
            print(f"[Crossref] Skipped {jname}: {e}")
            continue

        for x in j.get("message", {}).get("items", []):
            title = ((x.get("title") or [])[:1] or [""])[0]
            abstr = (x.get("abstract") or "")
            # quick in-memory keyword match
            hay = " ".join([title, re.sub(r"<[^>]+>", " ", abstr or "")]).lower()
            if not any(k in hay for k in [kw.lower() for kw in KEYWORDS]):
                continue
            if any(x in hay for x in EXCLUDE_KEYWORDS):
                continue

            issued = x.get("issued",{}).get("date-parts", [[]])[0]
            pubdate = "-".join(str(p) for p in issued) if issued else (x.get("created",{}).get("date-time","")[:10])
            authors = [(" ".join(filter(None, [a.get("given"), a.get("family")]))).strip() for a in (x.get("author") or [])]
            clean_abs = re.sub(r"<[^>]+>", " ", abstr).strip()

            rec = {
                "source": "Crossref",
                "journal": ((x.get("container-title") or [])[:1] or [""])[0],
                "title": title,
                "abstract": clean_abs,
                "authors": authors,
                "published": pubdate,
                "url": x.get("URL"),
                "doi": x.get("DOI")
            }
            results.append(rec)

    # De-dupe by DOI
    seen = set()
    uniq = []
    for it in results:
        if it.get("doi") and it["doi"] in seen:
            continue
        if it.get("doi"): seen.add(it["doi"])
        uniq.append(it)

    # Altmetric enrichment with small parallel fan-out
    from concurrent.futures import ThreadPoolExecutor, as_completed
    def enrich(it):
        if it.get("doi"):
            it.update(altmetric_by_doi(it["doi"]))
        it["rank_score"] = score_item(it)
        return it

    enriched = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(enrich, it) for it in uniq[:30]]  # cap to 30
        for f in as_completed(futs):
            enriched.append(f.result())

    # Sort by Altmetric score desc, keep top 5
    enriched.sort(key=lambda r: (r.get("altmetric_score") or 0.0), reverse=True)
    top5 = enriched[:5]
    print(f"Crossref candidates: {len(uniq)}, after Altmetric keep: {len(top5)}")
    return top5

def parse_arxiv_xml(xml: str) -> List[Dict[str,Any]]:
    # minimal parse: not robust XML library to avoid extra deps
    entries = xml.split("<entry>")[1:]
    out = []
    for e in entries:
        try:
            def tag(t):
                m = re.search(fr"<{t}>(.*?)</{t}>", e, flags=re.S)
                return (m.group(1).strip() if m else "")
            def taga(t, attr):
                m = re.search(fr"<{t}[^>]*\s{attr}=['\"]([^'\"]+)['\"][^>]*>", e)
                return (m.group(1) if m else "")
            _id = tag("id")
            title = re.sub(r"\s+", " ", tag("title")).strip()
            summary = re.sub(r"\s+", " ", tag("summary")).strip()
            published = tag("published")[:10] or tag("updated")[:10]
            # extract arxiv id
            arxiv_id = ""
            if "/abs/" in _id:
                arxiv_id = _id.split("/abs/")[1]
            else:
                parts = _id.split("/")
                arxiv_id = parts[-1]
            # link
            url = taga("link", "href") or _id
            # primary category
            m = re.search(r'<arxiv:primary_category[^>]*term=[\'"]([^\'"]+)[\'"]', e)
            cat = m.group(1) if m else ""
            # authors (naive)
            authors = re.findall(r"<name>(.*?)</name>", e)
            out.append({
                "source": "arXiv",
                "arxiv_id": arxiv_id,
                "title": title,
                "abstract": summary,
                "authors": authors,
                "published": published,
                "url": url,
                "category": cat
            })
        except Exception:
            continue
    return out

def fetch_arxiv_top2() -> List[Dict[str,Any]]:
    since, until = last_30_window()
    query = (
        '((ti:"cancer" OR ti:"oncology" OR ti:"tumor" OR ti:"immunotherapy" OR '
        'ti:"t-cell" OR ti:"tcr" OR ti:"neoantigen" OR ti:"immune checkpoint" OR '
        'abs:"cancer" OR abs:"tumor" OR abs:"immunotherapy" OR abs:"neoantigen" OR '
        'abs:"t-cell" OR abs:"tcr" OR abs:"immune checkpoint") '
        'AND (ti:"machine learning" OR ti:"deep learning" OR ti:"artificial intelligence" OR '
        'abs:"machine learning" OR abs:"deep learning" OR abs:"artificial intelligence")) '
        'AND NOT (abs:"embryology" OR abs:"developmental biology" OR ti:"embryology")'
    )   
    url = f"https://export.arxiv.org/api/query?search_query={requests.utils.quote(query)}&sortBy=submittedDate&sortOrder=descending&max_results=60"
    xml = fetch_text(url)
    items = parse_arxiv_xml(xml)

    cutoff = dt.datetime.utcnow().date() - dt.timedelta(days=30)
    items = [it for it in items if it.get("published") and dt.date.fromisoformat(it["published"]) >= cutoff]

    # Only the 10 most recent get Altmetric calls
    recent = sorted(items, key=lambda r: r.get("published",""), reverse=True)[:10]

    from concurrent.futures import ThreadPoolExecutor, as_completed
    def enrich(it):
        if it.get("arxiv_id"):
            it.update(altmetric_by_arxiv(it["arxiv_id"]))
        it["rank_score"] = score_item(it)
        return it

    enriched = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(enrich, it) for it in recent]
        for f in as_completed(futs):
            enriched.append(f.result())

    enriched.sort(key=lambda r: ((r.get("altmetric_score") or 0.0), r.get("published","")), reverse=True)
    return enriched[:2]


def format_item(it: Dict[str,Any]) -> Dict[str,str]:
    # produce fields: title, date, url, summary (2 sentences)
    title = it.get("title","(no title)").strip()
    date = it.get("published") or it.get("created","")[:10]
    url = it.get("url") or (("https://doi.org/"+it["doi"]) if it.get("doi") else "")
    abstract = it.get("abstract","").strip()
    summary = first_two_sentences(abstract) or "Summary unavailable."
    return {"title": title, "date": date, "url": url, "summary": summary}

def build_digest() -> List[Dict[str,str]]:
    print("Fetching Crossref (Nature/Cell)…")
    cross = fetch_crossref()
    print(f"Crossref done: {len(cross)} items")

    print("Fetching arXiv…")
    arx = fetch_arxiv_top2()
    print(f"arXiv done: {len(arx)} items")

    items = [format_item(x) for x in cross] + [format_item(x) for x in arx]
    return items

def slack_blocks(items: List[Dict[str,str]]) -> Dict[str,Any]:
    header = {
        "type": "header",
        "text": {"type":"plain_text","text":"ML ↔ Biology: Last 30 Days (Top 5 + 2 arXiv)","emoji":True}
    }
    divider = {"type":"divider"}
    sections = []
    for i, it in enumerate(items, start=1):
        sections.append({
            "type":"section",
            "text": {
                "type":"mrkdwn",
                "text": f"*{i}. <{it['url']}|{it['title']}>*\n_{it['date']}_ – {it['summary']}"
            }
        })
    return {"blocks": [header, divider, *sections]}

def post_to_slack(webhook_url, payload):
    import requests
    r = requests.post(webhook_url, json=payload, timeout=20)
    print(f"Slack status={r.status_code} body={r.text[:200]}")
    r.raise_for_status()

def main():
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        print("ERROR: SLACK_WEBHOOK_URL environment variable is not set.", file=sys.stderr)
        sys.exit(2)

    print("Starting digest…")
    items = build_digest()
    print(f"Built payload with {len(items)} items")
    payload = slack_blocks(items)
    print("Posting to Slack…")
    post_to_slack(webhook, payload)
    print("Posted to Slack OK.")

if __name__ == "__main__":
    main()
