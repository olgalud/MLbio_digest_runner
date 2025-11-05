# ML↔Biology Digest (Last 30 Days) → Slack

A single Python script that pulls:
- **Top 5** Nature/Cell-family ML+biology papers (Crossref; ranked by Altmetric)
- **Top 2** arXiv ML+biology preprints (ranked by Altmetric when available, else recency)

…and posts a formatted summary to Slack via an **Incoming Webhook**.

## Local run
```bash
python -m pip install requests
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/xxx/yyy/zzz"
python mlbio_digest.py
```

## GitHub Actions (serverless)
1. Create a new GitHub repo containing `mlbio_digest.py` and `.github/workflows/weekly.yml`.
2. In the repo, add a secret: **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `SLACK_WEBHOOK_URL`
   - Value: your Slack Incoming Webhook URL (configure in Slack).
3. The workflow runs **weekly** (Wednesdays 09:00 ET) and can be started on demand via **Run workflow**.

## Notes
- Crossref queries the last 30 days and filters to the Nature/Cell family list.
- Altmetric is looked up by DOI (journals) or arXiv ID (preprints); if unavailable, items are scored without it.
- Summaries are extracted as the first two sentences of the abstract (fallback to a single sentence or placeholder).
- Ties and missing Altmetric are handled gracefully.
