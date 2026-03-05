import os
import re
import csv
import io
import json
import logging
import time
import urllib.parse
from flask import Flask, request, jsonify, render_template, make_response
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
from urllib.parse import urlparse

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Lazy Gemini client
_gemini_client = None

def get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            raise ValueError("GEMINI_API_KEY environment variable not set")
        genai.configure(api_key=api_key)
        _gemini_client = genai.GenerativeModel('gemini-1.5-flash')
    return _gemini_client


# ---------------------------------------------------------------------------
# PLACEHOLDER / JUNK DOMAIN DETECTION
# ---------------------------------------------------------------------------

PLACEHOLDER_DOMAINS = {
    'godaddy.com', 'wix.com', 'squarespace.com', 'weebly.com',
    'wordpress.com', 'sites.google.com', 'business.site', 'myshopify.com',
    'blogspot.com', 'tumblr.com', 'facebook.com', 'instagram.com',
    'linktr.ee', 'linkinbio.com', 'carrd.co', 'notion.site',
}

def is_real_website(url: str) -> bool:
    """Return True if URL looks like a real dedicated business website."""
    if not url:
        return False
    try:
        parsed = urlparse(url if url.startswith('http') else 'https://' + url)
        domain = parsed.netloc.lower().replace('www.', '')
        for placeholder in PLACEHOLDER_DOMAINS:
            if domain == placeholder or domain.endswith('.' + placeholder):
                return False
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# REQUESTS-BASED GOOGLE MAPS SCRAPER  (no Playwright, no API key)
# ---------------------------------------------------------------------------

class PlaywrightLeadFinder:
    """
    Scrapes Google Maps search results using plain HTTP requests + BeautifulSoup.
    Works on any server — no Playwright/Chromium required.
    Falls back to Google Places Text Search API if GOOGLE_PLACES_API_KEY is set.
    """

    HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        ),
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    }

    def search(self, keyword: str, city: str, max_results: int = 20) -> list:
        # If Google Places API key is available, use it (most reliable)
        api_key = os.getenv('GOOGLE_PLACES_API_KEY')
        if api_key:
            return self._search_places_api(keyword, city, max_results, api_key)
        # Otherwise fall back to scraping
        return self._search_scrape(keyword, city, max_results)

    def _search_places_api(self, keyword: str, city: str, max_results: int, api_key: str) -> list:
        """Use Google Places Text Search API — most reliable path."""
        query = f"{keyword} in {city}"
        url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
        results = []
        page_token = None

        while len(results) < max_results:
            params = {'query': query, 'key': api_key}
            if page_token:
                params['pagetoken'] = page_token
                time.sleep(2)  # Required delay for next_page_token

            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()

            if data.get('status') not in ('OK', 'ZERO_RESULTS'):
                logger.warning(f"Places API error: {data.get('status')} — {data.get('error_message','')}")
                break

            for place in data.get('results', []):
                if len(results) >= max_results:
                    break
                lead = self._place_to_lead(place, api_key)
                results.append(lead)

            page_token = data.get('next_page_token')
            if not page_token:
                break

        logger.info(f"Google Places API returned {len(results)} leads")
        return results

    def _place_to_lead(self, place: dict, api_key: str) -> dict:
        """Convert a Places API result to our lead format, enriching with details if possible."""
        place_id = place.get('place_id')
        name = place.get('name', '')
        address = place.get('formatted_address', '')
        rating = place.get('rating')
        review_count = place.get('user_ratings_total', 0)
        category = place.get('types', [''])[0].replace('_', ' ').title() if place.get('types') else ''

        # Fetch place details to get phone + website
        phone = ''
        website = ''
        if place_id:
            try:
                det_url = "https://maps.googleapis.com/maps/api/place/details/json"
                det = requests.get(det_url, params={
                    'place_id': place_id,
                    'fields': 'formatted_phone_number,website',
                    'key': api_key
                }, timeout=8).json()
                result = det.get('result', {})
                phone = result.get('formatted_phone_number', '')
                website = result.get('website', '')
            except Exception as e:
                logger.warning(f"Could not fetch details for {name}: {e}")

        has_real_site = is_real_website(website)
        return {
            'name': name,
            'address': address,
            'phone': phone,
            'website': website,
            'has_website': has_real_site,
            'is_hot_lead': not has_real_site,
            'category': category,
            'rating': rating,
            'review_count': review_count,
        }

    def _search_scrape(self, keyword: str, city: str, max_results: int) -> list:
        """
        Fallback: scrape Google Maps HTML search results page.
        Extracts structured data from the page's embedded JSON blobs.
        """
        query = urllib.parse.quote_plus(f"{keyword} {city}")
        search_url = f"https://www.google.com/maps/search/{query}"
        logger.info(f"Scraping Google Maps: {search_url}")

        try:
            resp = requests.get(search_url, headers=self.HEADERS, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            raise Exception(f"Failed to reach Google Maps: {e}")

        html = resp.text

        # Extract business data from embedded JSON in the page
        results = []
        # Google Maps embeds data in window.APP_INITIALIZATION_STATE or similar JSON blobs
        # We parse out business listings using regex on the serialised data arrays
        patterns = [
            r'"([^"]{2,80})",[^,]*,\["https?://[^"]+"\],[^,]*,\["(\+?[\d\s\-().]{7,20})"\]',
        ]

        # More reliable: look for the /*""*/ JSON data blocks
        json_blocks = re.findall(r'\\x22([^\\]{5,80})\\x22', html)

        # Parse business name + address blocks from the HTML directly
        soup = BeautifulSoup(html, 'html.parser')

        # Try to find structured listing data
        seen_names = set()
        for div in soup.find_all('div', attrs={'aria-label': True}):
            label = div.get('aria-label', '').strip()
            if not label or label in seen_names or len(label) < 3:
                continue
            if any(skip in label.lower() for skip in ['search', 'map', 'zoom', 'directions', 'menu']):
                continue
            seen_names.add(label)

            # Try to find associated details nearby in the DOM
            text = div.get_text(separator=' ', strip=True)
            phone_match = re.search(r'(\+?1?\s?[\(]?\d{3}[\)]?[\s.\-]?\d{3}[\s.\-]?\d{4})', text)
            phone = phone_match.group(1) if phone_match else ''

            # Look for website in nearby links
            website = ''
            for a in div.find_all('a', href=True):
                href = a['href']
                if href.startswith('http') and 'google.com' not in href and 'goo.gl' not in href:
                    website = href
                    break

            has_real_site = is_real_website(website)
            results.append({
                'name': label,
                'address': '',
                'phone': phone,
                'website': website,
                'has_website': has_real_site,
                'is_hot_lead': not has_real_site,
                'category': keyword.title(),
                'rating': None,
                'review_count': 0,
            })

            if len(results) >= max_results:
                break

        # If scraping got nothing useful, return a helpful error set
        if not results:
            raise Exception(
                "Google Maps HTML scraping returned no results. "
                "Please set the GOOGLE_PLACES_API_KEY environment variable on Render "
                "for reliable lead search. Get a free key at https://console.cloud.google.com/"
            )

        logger.info(f"Scraped {len(results)} leads from Google Maps HTML")
        return results


# ---------------------------------------------------------------------------
# WEBSITE SCRAPER
# ---------------------------------------------------------------------------

class ProspectScraper:
    """Scrapes and extracts key information from prospect URLs."""

    def __init__(self):
        self.headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/91.0.4472.124 Safari/537.36'
            )
        }

    def scrape_url(self, url: str, timeout: int = 10) -> dict:
        try:
            logger.info(f"Scraping URL: {url}")
            response = requests.get(url, headers=self.headers, timeout=timeout)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            for tag in soup(["script", "style", "nav", "footer"]):
                tag.decompose()
            text = re.sub(r'\s+', ' ', soup.get_text(separator=' ', strip=True)).strip()[:8000]
            title = soup.find('title')
            meta_desc = soup.find('meta', attrs={'name': 'description'})
            og_title = soup.find('meta', property='og:title')
            og_desc = soup.find('meta', property='og:description')
            headings = [h.get_text(strip=True) for h in soup.find_all(['h1', 'h2', 'h3']) if h.get_text(strip=True)][:10]
            return {
                'url': url,
                'is_linkedin': 'linkedin.com' in url.lower(),
                'title': title.string if title else '',
                'meta_description': meta_desc.get('content', '') if meta_desc else '',
                'og_title': og_title.get('content', '') if og_title else '',
                'og_description': og_desc.get('content', '') if og_desc else '',
                'headings': headings,
                'content': text,
                'domain': urlparse(url).netloc,
            }
        except requests.exceptions.Timeout:
            raise Exception("Request timeout — website took too long to respond")
        except requests.exceptions.RequestException as e:
            raise Exception(f"Failed to fetch URL: {e}")
        except Exception as e:
            raise Exception(f"Error processing URL: {e}")


# ---------------------------------------------------------------------------
# OUTREACH EMAIL GENERATOR
# ---------------------------------------------------------------------------

class OutreachGenerator:
    """Generates personalized cold outreach emails using Gemini."""

    SYSTEM = (
        "You are an expert cold email writer who creates highly personalized, "
        "concise outreach that feels human and authentic. Never use hype or generic phrases."
    )

    def __init__(self, client):
        self.client = client

    # ---- Original URL-based flow ------------------------------------------

    def generate_email(self, prospect_data: dict, sender_context: str = None) -> dict:
        context = self._build_url_context(prospect_data)
        prompt = self._url_prompt(context, sender_context)
        result = self._call_gemini(prompt)
        result['personalization_notes'] = self._url_signals(prospect_data, result)
        return result

    def _build_url_context(self, d: dict) -> str:
        parts = [f"URL: {d['url']}", f"Domain: {d['domain']}"]
        if d.get('is_linkedin'):
            parts.append("Source: LinkedIn Profile")
        if d.get('title'):
            parts.append(f"Page Title: {d['title']}")
        if d.get('meta_description'):
            parts.append(f"Description: {d['meta_description']}")
        if d.get('headings'):
            parts.append(f"Key Topics: {', '.join(d['headings'][:5])}")
        if d.get('content'):
            parts.append(f"\nPage Content:\n{d['content'][:3000]}")
        return "\n".join(parts)

    def _url_prompt(self, context: str, sender_context: str = None) -> str:
        sender_info = sender_context or (
            "You are reaching out on behalf of a growth consultant who helps companies "
            "scale revenue through automation and AI. Value prop: Custom AI agents that "
            "automate lead gen, customer support, and content creation."
        )
        return f"""Based on the following prospect information, write a highly personalized cold outreach email.

PROSPECT INFORMATION:
{context}

SENDER CONTEXT:
{sender_info}

REQUIREMENTS:
1. Subject: Short, specific, references something from their profile/site
2. Opening: Hook with a specific observation
3. Body: 2-3 short paragraphs. Connect their pain to your solution.
4. CTA: Clear, low-friction (15-min call, quick demo)
5. Tone: Professional but conversational. Under 150 words total.

FORMAT EXACTLY:
SUBJECT: [subject line]

BODY:
[email body]
"""

    def _url_signals(self, prospect_data: dict, email: dict) -> list:
        signals = []
        email_text = f"{email.get('subject','')} {email.get('body','')}".lower()
        if prospect_data.get('is_linkedin'):
            signals.append("LinkedIn profile analyzed")
        else:
            signals.append(f"Company website ({prospect_data.get('domain','')}) analyzed")
        if prospect_data.get('domain'):
            company = prospect_data['domain'].replace('.com','').replace('www.','')
            if company.lower() in email_text:
                signals.append("Company name referenced")
        for heading in prospect_data.get('headings', [])[:5]:
            if len(heading) > 10 and heading.lower() in email_text:
                signals.append(f"Referenced: '{heading}'")
        if not signals:
            signals.append("Content analysis and context matching applied")
        return signals

    # ---- Lead-based flow (from Google Maps scrape) -----------------------

    def generate_email_for_lead(self, lead: dict, sender_context: str = None, website_data: dict = None) -> dict:
        """Two paths: no website → pitch digital presence; has website → personalize from site."""
        sender_info = sender_context or (
            "You help local service businesses get more customers online. "
            "You build professional websites, Google Business profiles, and simple "
            "AI chat widgets. Packages start at $500."
        )
        if not lead.get('has_website'):
            prompt = self._no_website_prompt(lead, sender_info)
        else:
            prompt = self._has_website_prompt(lead, sender_info, website_data)

        result = self._call_gemini(prompt)
        result['lead_type'] = 'no_website' if not lead.get('has_website') else 'has_website'
        result['personalization_notes'] = self._lead_signals(lead)
        return result

    def _no_website_prompt(self, lead: dict, sender_info: str) -> str:
        return f"""Write a cold outreach email to a local business owner who has NO website yet.

BUSINESS INFO:
- Name: {lead['name']}
- Category: {lead.get('category', 'Local Business')}
- Address: {lead.get('address', '')}
- Phone: {lead.get('phone', 'N/A')}
- Google Rating: {lead.get('rating', 'N/A')} ({lead.get('review_count', 0)} reviews)

SENDER CONTEXT:
{sender_info}

KEY ANGLE: They have no website. Their competitors likely do. This costs them customers every day.
Reference their specific business type and location. Make it feel genuine, not templated.

REQUIREMENTS:
- Under 130 words
- Empathetic, not pushy
- Mention the missing website opportunity specifically
- Low-friction CTA (free chat, quick call)

FORMAT EXACTLY:
SUBJECT: [subject line]

BODY:
[email body]
"""

    def _has_website_prompt(self, lead: dict, sender_info: str, website_data: dict = None) -> str:
        site_context = ""
        if website_data:
            site_context = f"""
WEBSITE CONTENT:
- Title: {website_data.get('title', '')}
- Description: {website_data.get('meta_description', '')}
- Key Topics: {', '.join(website_data.get('headings', [])[:5])}
- Excerpt: {website_data.get('content', '')[:1500]}
"""
        return f"""Write a cold outreach email to a local business owner who HAS a website.

BUSINESS INFO:
- Name: {lead['name']}
- Category: {lead.get('category', 'Local Business')}
- Address: {lead.get('address', '')}
- Website: {lead.get('website', '')}
- Google Rating: {lead.get('rating', 'N/A')} ({lead.get('review_count', 0)} reviews)
{site_context}
SENDER CONTEXT:
{sender_info}

KEY ANGLE: Reference something specific from their business. Offer clear added value.

REQUIREMENTS:
- Under 150 words
- Reference at least one specific detail from their business
- Professional but warm tone
- Clear, low-friction CTA

FORMAT EXACTLY:
SUBJECT: [subject line]

BODY:
[email body]
"""

    def _call_gemini(self, prompt: str) -> dict:
        response = self.client.generate_content(
            f"{self.SYSTEM}\n\n{prompt}",
            generation_config=genai.GenerationConfig(temperature=0.7, max_output_tokens=800),
        )
        return self._parse_response(response.text)

    def _parse_response(self, text: str) -> dict:
        subject_match = re.search(r'SUBJECT:\s*(.+?)(?:\n|$)', text, re.IGNORECASE)
        subject = subject_match.group(1).strip() if subject_match else "Quick question"
        body_match = re.search(r'BODY:\s*(.+)', text, re.IGNORECASE | re.DOTALL)
        body = body_match.group(1).strip() if body_match else text
        body = re.sub(r'^(SUBJECT|BODY):.*', '', body, flags=re.IGNORECASE | re.MULTILINE).strip()
        return {'subject': subject, 'body': body}

    def _lead_signals(self, lead: dict) -> list:
        signals = []
        if not lead.get('has_website'):
            signals.append("No website detected — web presence pitch applied")
        else:
            signals.append(f"Website found: {lead.get('website', '')}")
        if lead.get('rating'):
            signals.append(f"Google rating: {lead['rating']} ({lead.get('review_count', 0)} reviews)")
        if lead.get('category'):
            signals.append(f"Category: {lead['category']}")
        return signals


# ---------------------------------------------------------------------------
# FLASK ROUTES
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy', 'service': 'Cold Outreach Engine', 'timestamp': time.time()}), 200


# ---- Original single-URL route (unchanged) --------------------------------

@app.route('/api/generate', methods=['POST'])
def generate_outreach():
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        data = request.get_json()
        url = data.get('url', '').strip()
        if not url:
            return jsonify({'success': False, 'error': 'URL is required'}), 400
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        if not urlparse(url).netloc:
            return jsonify({'success': False, 'error': 'Invalid URL format'}), 400

        scraper = ProspectScraper()
        prospect_data = scraper.scrape_url(url)
        generator = OutreachGenerator(get_gemini_client())
        email_result = generator.generate_email(prospect_data, data.get('sender_context'))

        return jsonify({
            'success': True,
            'data': email_result,
            'prospect': {'url': url, 'domain': prospect_data['domain'], 'title': prospect_data['title']},
        }), 200
    except Exception as e:
        logger.error(f"Error in generate_outreach: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Search leads via Playwright Google Maps scraper ---------------------

@app.route('/api/search-leads', methods=['POST'])
def search_leads():
    """
    Find local businesses by scraping Google Maps — no API key required.
    Body: { "keyword": "plumbers", "city": "Halifax", "max_results": 20 }
    Returns leads tagged as hot (no website) or warm (has website).
    """
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        data = request.get_json()
        keyword = data.get('keyword', '').strip()
        city = data.get('city', '').strip()
        max_results = min(int(data.get('max_results', 20)), 60)

        if not keyword or not city:
            return jsonify({'success': False, 'error': 'keyword and city are required'}), 400

        finder = PlaywrightLeadFinder()
        leads = finder.search(keyword, city, max_results)
        hot = [l for l in leads if l['is_hot_lead']]
        warm = [l for l in leads if not l['is_hot_lead']]

        return jsonify({
            'success': True,
            'total': len(leads),
            'hot_leads': len(hot),
            'warm_leads': len(warm),
            'leads': leads,
        }), 200
    except Exception as e:
        logger.error(f"Error in search_leads: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Bulk generate emails for a list of leads ---------------------------

@app.route('/api/bulk-generate', methods=['POST'])
def bulk_generate():
    """
    Generate cold emails for a list of leads.
    Body: {
        "leads": [...],
        "sender_context": "optional — what you're selling",
        "scrape_websites": false
    }
    """
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        data = request.get_json()
        leads = data.get('leads', [])
        sender_context = data.get('sender_context')
        scrape_websites = data.get('scrape_websites', False)

        if not leads:
            return jsonify({'success': False, 'error': 'leads list is required'}), 400

        generator = OutreachGenerator(get_gemini_client())
        scraper = ProspectScraper() if scrape_websites else None
        results = []

        for lead in leads:
            website_data = None
            if scrape_websites and lead.get('has_website') and lead.get('website'):
                try:
                    website_data = scraper.scrape_url(lead['website'])
                except Exception as e:
                    logger.warning(f"Could not scrape {lead.get('website')}: {e}")

            try:
                email = generator.generate_email_for_lead(lead, sender_context, website_data)
                results.append({'lead': lead, 'email': email, 'success': True})
            except Exception as e:
                logger.error(f"Failed for {lead.get('name')}: {e}")
                results.append({'lead': lead, 'email': None, 'success': False, 'error': str(e)})

            time.sleep(0.5)  # Rate limit buffer

        return jsonify({
            'success': True,
            'total': len(results),
            'generated': sum(1 for r in results if r['success']),
            'results': results,
        }), 200
    except Exception as e:
        logger.error(f"Error in bulk_generate: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Export to CSV -------------------------------------------------------

@app.route('/api/export-csv', methods=['POST'])
def export_csv():
    """
    Export bulk-generate results to downloadable CSV.
    Body: { "results": [...] }
    """
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        data = request.get_json()
        results = data.get('results', [])
        if not results:
            return jsonify({'success': False, 'error': 'No results to export'}), 400

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            'Business Name', 'Category', 'Address', 'Phone', 'Website',
            'Has Website', 'Hot Lead', 'Rating', 'Reviews',
            'Email Subject', 'Email Body', 'Lead Type',
        ])
        for r in results:
            lead = r.get('lead', {})
            email = r.get('email') or {}
            writer.writerow([
                lead.get('name', ''),
                lead.get('category', ''),
                lead.get('address', ''),
                lead.get('phone', ''),
                lead.get('website', ''),
                'No' if lead.get('is_hot_lead') else 'Yes',
                'YES' if lead.get('is_hot_lead') else 'No',
                lead.get('rating', ''),
                lead.get('review_count', ''),
                email.get('subject', ''),
                email.get('body', '').replace('\n', ' '),
                email.get('lead_type', ''),
            ])

        response = make_response(output.getvalue())
        response.headers['Content-Disposition'] = 'attachment; filename=cold_outreach_leads.csv'
        response.headers['Content-Type'] = 'text/csv'
        return response
    except Exception as e:
        logger.error(f"Error in export_csv: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Error handlers ------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return jsonify({'success': False, 'error': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(e):
    return jsonify({'success': False, 'error': 'Internal server error'}), 500


if __name__ == '__main__':
    if not os.getenv('GEMINI_API_KEY'):
        logger.warning("GEMINI_API_KEY not set — AI calls will fail")
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_ENV') == 'development'
    logger.info(f"Starting Cold Outreach Engine on port {port}")
    app.run(host='0.0.0.0', port=port, debug=debug)
