import os
import re
import csv
import io
import json
import logging
import time
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
# PLAYWRIGHT GOOGLE MAPS SCRAPER  (zero cost, no API key)
# ---------------------------------------------------------------------------

class PlaywrightLeadFinder:
    """
    Scrapes Google Maps using Playwright headless Chrome.
    No API key required — completely free.
    """

    def search(self, keyword: str, city: str, max_results: int = 20) -> list:
        from playwright.sync_api import sync_playwright

        query = f"{keyword} {city}"
        logger.info(f"Scraping Google Maps for: {query}")
        results = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()

            try:
                search_url = f"https://www.google.com/maps/search/{requests.utils.quote(query)}"
                page.goto(search_url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(2000)

                # Scroll the results pane to load more listings
                results_pane = page.query_selector('div[role="feed"]')
                loaded = 0
                while loaded < max_results:
                    if results_pane:
                        results_pane.evaluate("el => el.scrollTop += 800")
                    else:
                        page.mouse.wheel(0, 800)
                    page.wait_for_timeout(1200)

                    items = page.query_selector_all('a[href*="/maps/place/"]')
                    loaded = len(items)
                    if loaded >= max_results:
                        break

                    # Check for end-of-results indicator
                    end_msg = page.query_selector('span.HlvSq')
                    if end_msg:
                        break

                # Collect listing links (deduplicated)
                items = page.query_selector_all('a[href*="/maps/place/"]')
                seen_hrefs = set()
                listing_urls = []
                for item in items:
                    href = item.get_attribute('href')
                    if href and href not in seen_hrefs:
                        seen_hrefs.add(href)
                        listing_urls.append(href)
                    if len(listing_urls) >= max_results:
                        break

                logger.info(f"Found {len(listing_urls)} listing URLs")

                # Visit each listing to extract details
                for url in listing_urls[:max_results]:
                    try:
                        lead = self._extract_listing(page, url)
                        if lead:
                            results.append(lead)
                        page.wait_for_timeout(800)
                    except Exception as e:
                        logger.warning(f"Failed to extract listing {url}: {e}")
                        continue

            finally:
                browser.close()

        logger.info(f"Scraped {len(results)} leads from Google Maps")
        return results

    def _extract_listing(self, page, url: str) -> dict:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1500)

        def get_text(selector):
            el = page.query_selector(selector)
            return el.inner_text().strip() if el else ""

        # Business name
        name = get_text('h1.DUwDvf') or get_text('h1[data-attrid="title"]') or get_text('h1')

        # Address
        address = ""
        addr_el = page.query_selector('button[data-item-id="address"]')
        if addr_el:
            address = addr_el.inner_text().strip()
        if not address:
            address = get_text('[data-item-id="address"] .Io6YTe')

        # Phone
        phone = ""
        phone_el = page.query_selector('button[data-item-id^="phone:tel:"]')
        if phone_el:
            phone = phone_el.inner_text().strip()

        # Website
        website = ""
        web_el = page.query_selector('a[data-item-id="authority"]')
        if web_el:
            website = web_el.get_attribute('href') or ""
        if website.startswith('https://www.google.com/url'):
            # Unwrap Google redirect
            match = re.search(r'[?&]q=([^&]+)', website)
            if match:
                website = requests.utils.unquote(match.group(1))

        # Category
        category = get_text('button.DkEaL') or get_text('[jsaction*="category"]')

        # Rating & reviews
        rating_text = get_text('div.F7nice span[aria-hidden="true"]')
        rating = None
        try:
            rating = float(rating_text) if rating_text else None
        except ValueError:
            pass

        review_text = get_text('div.F7nice span[aria-label*="review"]')
        review_count = 0
        try:
            nums = re.findall(r'[\d,]+', review_text)
            review_count = int(nums[0].replace(',', '')) if nums else 0
        except Exception:
            pass

        if not name:
            return None

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
