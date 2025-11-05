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
    "Nature","Nature Medicine","Nature Biotechnology","Nature Methods","Nature Genetics","Nature Chemical Biology",
    "Nature Machine Intelligence","Nature Communications","Nature Aging","Nature Computational Science",
    "Cell","Cell Reports","Cell Systems","Immunity","Cancer Cell","Molecular Cell","Cell Genomics","Cell Host & Microbe"
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
    q = '("machine learning" OR "deep learning" OR "artificial intelligence" OR "neural network") (biology OR biomedical OR genomics OR proteomics OR immunology OR cancer)'
    url = (
        "https://api.crossref.org/works"
        f"?filter=from-pub-date:{since},until-pub-date:{until},type:journal-article"
        f"&query.bibliographic={requests.utils.quote(q)}"
        "&select=DOI,title,container-title,author,abstract,URL,created,issued"
        "&rows=200"
    )
    j = fetch_json(url)
    items = []
    for x in j.get("message", {}).get("items", []):
        journal = ((x.get("container-title") or [])[:1] or [""])[0]
        if journal not in TOP_VENUES:
            continue
        title = ((x.get("title") or [])[:1] or [""])[0]
        issued = x.get("issued",{}).get("date-parts", [[]])[0]
        pubdate = "-".join(str(p) for p in issued) if issued else (x.get("created",{}).get("date-time","")[:10])
        authors = [(" ".join(filter(None, [a.get("given"), a.get("family")]))).strip() for a in (x.get("author") or [])]
        abstract = (x.get("abstract") or "").replace("\n"," ")
        abstract = re.sub(r"<[^>]+>", " ", abstract).strip()
        rec = {
            "source": "Crossref",
            "journal": journal,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "published": pubdate,
            "url": x.get("URL"),
            "doi": x.get("DOI")
        }
        if rec["doi"]:
            rec.update(altmetric_by_doi(rec["doi"]))
        rec["rank_score"] = score_item(rec)
        items.append(rec)
    # sort by altmetric score primarily
    items.sort(key=lambda r: (r.get("altmetric_score") or 0.0), reverse=True)
    # keep top 5
    return items[:5]

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
    query = '((ti:"biology" OR ti:"biomedical" OR ti:"genomics" OR ti:"proteomics" OR ti:"protein" OR ti:"immunology" OR ti:"cancer" OR abs:"biology" OR abs:"genomics" OR abs:"proteomics")) AND (cat:cs.LG OR cat:stat.ML OR cat:q-bio.BM OR cat:q-bio.QM OR ti:"machine learning" OR ti:"deep learning")'
    url = f"https://export.arxiv.org/api/query?search_query={requests.utils.quote(query)}&sortBy=submittedDate&sortOrder=descending&max_results=100"
    xml = fetch_text(url)
    items = parse_arxiv_xml(xml)
    # filter last 30 days
    cutoff = dt.datetime.utcnow().date() - dt.timedelta(days=30)
    items = [it for it in items if it.get("published") and dt.date.fromisoformat(it["published"]) >= cutoff]
    # enrich with Altmetric if available
    for it in items:
        if it.get("arxiv_id"):
            it.update(altmetric_by_arxiv(it["arxiv_id"]))
        it["rank_score"] = score_item(it)
    # rank by altmetric score primarily then recency
    items.sort(key=lambda r: ((r.get("altmetric_score") or 0.0), r.get("published","")), reverse=True)
    return items[:2]

def format_item(it: Dict[str,Any]) -> Dict[str,str]:
    # produce fields: title, date, url, summary (2 sentences)
    title = it.get("title","(no title)").strip()
    date = it.get("published") or it.get("created","")[:10]
    url = it.get("url") or (("https://doi.org/"+it["doi"]) if it.get("doi") else "")
    abstract = it.get("abstract","").strip()
    summary = first_two_sentences(abstract) or "Summary unavailable."
    return {"title": title, "date": date, "url": url, "summary": summary}

def build_digest() -> List[Dict[str,str]]:
    cross = fetch_crossref()  # top 5 by Altmetric score; Nature/Cell family only
    arx = fetch_arxiv_top2()  # top 2 arXiv (Altmetric if available)
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

def post_to_slack(webhook_url: str, payload: Dict[str,Any]):
    resp = requests.post(webhook_url, json=payload, timeout=20)
    if resp.status_code >= 300:
        raise RuntimeError(f"Slack webhook failed: HTTP {resp.status_code} {resp.text[:2000]}")

def main():
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        print("ERROR: SLACK_WEBHOOK_URL environment variable is not set.", file=sys.stderr)
        sys.exit(2)
    items = build_digest()
    payload = slack_blocks(items)
    post_to_slack(webhook, payload)
    print(f"Posted {len(items)} items to Slack.")

if __name__ == "__main__":
    main()
