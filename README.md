# Cold Outreach Engine

Find local businesses on Google, detect who has no website, and generate personalized AI cold emails in bulk — powered by Gemini. Built for service providers targeting local businesses: web design, AI automation, SEO, and more.

---

## What It Does

### Tab 1 — Find Leads (NEW)
1. **Search** — Enter a keyword (e.g. `plumbers`) + city (e.g. `Halifax`) to pull local businesses from Google Places
2. **Detect** — Automatically flags businesses with **no website** as 🔥 **Hot Leads** — your best prospects
3. **Filter** — View all leads, hot only, or warm only
4. **Bulk select** — Check the leads you want to target
5. **Generate** — AI writes a personalized cold email for each selected business:
   - **No website** → pitches web presence / digital setup
   - **Has website** → optionally scrapes it for deeper personalization
6. **Export** — Download all leads + generated emails as a CSV

### Tab 2 — Single URL Email
- Paste any LinkedIn profile or company website URL
- AI analyzes the page and writes a personalized cold email

---

## Hot Lead Logic

A business is flagged as a **Hot Lead** (no website) if:
- Their Google listing has no website field, OR
- Their website is a placeholder/social page (Facebook, Instagram, Linktree, Wix default page, etc.)

These businesses are your best targets — they clearly need digital help and aren't being reached by competitors doing online outreach.

---

## API Keys You Need

| Key | What for | Where to get it | Cost |
|-----|----------|-----------------|------|
| `GEMINI_API_KEY` | AI email generation | [aistudio.google.com](https://aistudio.google.com) | Free tier available |
| `GOOGLE_PLACES_API_KEY` | Business search | [console.cloud.google.com](https://console.cloud.google.com) | ~$17/1000 searches; $200/mo free credit for new accounts |

---

## Quick Start

### 1. Install dependencies

```bash
cd code/cold-outreach-engine
pip install -r requirements.txt
```

### 2. Set up environment

```bash
cp .env.example .env
```

Edit `.env`:
```
GEMINI_API_KEY=your-gemini-api-key-here
GOOGLE_PLACES_API_KEY=your-google-places-api-key-here
PORT=5000
FLASK_ENV=development
```

### 3. Get a Google Places API Key

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (or use existing)
3. Go to **APIs & Services → Library**
4. Search for and enable **Places API**
5. Go to **APIs & Services → Credentials → Create Credentials → API Key**
6. Copy the key into your `.env`

> New Google Cloud accounts get $200/month free credit — enough for thousands of searches.

### 4. Run

```bash
python app.py
# Visit http://localhost:5000
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Web UI |
| `POST` | `/api/search-leads` | Search Google Places for businesses |
| `POST` | `/api/bulk-generate` | Generate cold emails for a list of leads |
| `POST` | `/api/export-csv` | Download leads + emails as CSV |
| `POST` | `/api/generate` | Generate email from a single URL (original flow) |
| `GET` | `/health` | Health check |

### POST /api/search-leads
```json
{
  "keyword": "plumbers",
  "city": "Halifax",
  "max_results": 20
}
```

Returns:
```json
{
  "success": true,
  "total": 18,
  "hot_leads": 7,
  "warm_leads": 11,
  "leads": [
    {
      "name": "Joe's Plumbing",
      "address": "123 Main St, Halifax, NS",
      "phone": "(902) 555-1234",
      "website": "",
      "has_website": false,
      "is_hot_lead": true,
      "category": "Plumber",
      "rating": 4.3,
      "review_count": 28
    }
  ]
}
```

### POST /api/bulk-generate
```json
{
  "leads": [...],
  "sender_context": "I build websites for local trades businesses. Packages from $500.",
  "scrape_websites": false
}
```

### POST /api/export-csv
```json
{ "results": [...] }
```
Returns a downloadable `cold_outreach_leads.csv` file.

---

## Deploy to Railway / Render

### Railway
```bash
railway init
railway up
```
Set env vars in Railway dashboard.

### Render
Uses the included `render.yaml`. Connect your repo, add env vars, deploy.

---

## Requirements

```
flask
flask-cors
requests
beautifulsoup4
google-generativeai
```

Install: `pip install -r requirements.txt`
