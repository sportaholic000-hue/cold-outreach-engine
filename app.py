import os
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

_gemini_clients = {}

def get_gemini_client(model='gemini-1.5-flash'):
    global _gemini_clients
    if model not in _gemini_clients:
        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            raise ValueError("GEMINI_API_KEY environment variable not set")
        genai.configure(api_key=api_key)
        _gemini_clients[model] = genai.GenerativeModel(model)
    return _gemini_clients[model]

def gemini_generate(prompt, retries=3):
    """Try gemini-1.5-flash first, fall back to gemini-1.5-pro, with retry on 429."""
    models = ['gemini-1.5-flash', 'gemini-1.5-pro']
    last_err = None
    for model in models:
        for attempt in range(retries):
            try:
                client = get_gemini_client(model)
                response = client.generate_content(prompt)
                return response
            except Exception as e:
                err_str = str(e)
                last_err = e
                if '429' in err_str or 'quota' in err_str.lower() or 'exhausted' in err_str.lower():
                    wait = (attempt + 1) * 10
                    logger.warning(f"Quota hit on {model} attempt {attempt+1}, waiting {wait}s: {e}")
                    time.sleep(wait)
                    continue
                else:
                    logger.error(f"Gemini error on {model}: {e}")
                    break
    raise Exception(f"All Gemini models failed. Last error: {last_err}")

PLACEHOLDER_DOMAINS = {
    'godaddy.com', 'wix.com', 'squarespace.com', 'weebly.com',
    'wordpress.com', 'sites.google.com', 'business.site', 'myshopify.com',
    'blogspot.com', 'tumblr.com', 'facebook.com', 'instagram.com',
    'linktr.ee', 'linkinbio.com', 'carrd.co', 'notion.site',
}

def is_real_website(url: str) -> bool:
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

def clean_json(text: str):
    text = text.strip()
    if '```' in text:
        parts = text.split('```')
        for part in parts:
            part = part.strip()
            if part.startswith('json'):
                part = part[4:].strip()
            if part.startswith('[') or part.startswith('{'):
                text = part
                break
    return json.loads(text.strip())

class LeadFinder:
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    def search(self, industry, location, country, job_titles, company_size, max_results):
        api_key = os.getenv('GOOGLE_PLACES_API_KEY')
        if api_key:
            return self._search_with_places(industry, location, country, max_results, api_key)
        return self._search_with_gemini(industry, location, country, job_titles, company_size, max_results)

    def _search_with_gemini(self, industry, location, country, job_titles, company_size, max_results):
        try:
            size_hint = f" ({company_size} employees)" if company_size and company_size != 'Any' else ""
            title_hint = f" Key contacts: {job_titles}." if job_titles else ""
            prompt = f"""Generate {min(max_results, 25)} realistic B2B leads for {industry} companies in {location}, {country}{size_hint}.{title_hint}

Return ONLY a JSON array, no explanation:
[
  {{
    "name": "Company Name",
    "contact_name": "First Last",
    "contact_title": "CEO",
    "email": "first@companydomain.com",
    "phone": "+1 (555) 000-0000",
    "website": "https://companydomain.com",
    "address": "123 Main St, {location}, {country}",
    "employees": "11-50",
    "rating": 4.3,
    "reviews": 28,
    "linkedin": "https://linkedin.com/company/companyname"
  }}
]
Rules:
- Realistic local/regional companies (not Fortune 500 chains)
- Contact names and titles relevant to: {job_titles or 'decision makers'}
- Email format: firstname@companydomain.com
- Mix of websites present and absent (30% no website, empty string)
- Return ONLY the JSON array, nothing else"""
            response = gemini_generate(prompt)
            results = clean_json(response.text)
            if not isinstance(results, list):
                raise ValueError("Expected list")
            return results[:max_results]
        except Exception as e:
            logger.error(f"Gemini lead search failed: {e}")
            return []

    def _search_with_places(self, industry, location, country, max_results, api_key):
        query = f"{industry} in {location} {country}"
        url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
        try:
            resp = requests.get(url, params={'query': query, 'key': api_key}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            results = []
            for r in data.get('results', [])[:max_results]:
                details = self._get_place_details(r.get('place_id'), api_key)
                results.append({
                    'name': r.get('name', ''),
                    'contact_name': '',
                    'contact_title': '',
                    'email': '',
                    'phone': details.get('phone', ''),
                    'website': details.get('website', ''),
                    'address': r.get('formatted_address', ''),
                    'employees': '',
                    'rating': r.get('rating'),
                    'reviews': r.get('user_ratings_total'),
                    'linkedin': '',
                })
            return results
        except Exception as e:
            logger.error(f"Places API failed: {e}")
            return []

    def _get_place_details(self, place_id, api_key):
        url = "https://maps.googleapis.com/maps/api/place/details/json"
        try:
            resp = requests.get(url, params={'place_id': place_id, 'fields': 'formatted_phone_number,website', 'key': api_key}, timeout=5)
            result = resp.json().get('result', {})
            return {'phone': result.get('formatted_phone_number', ''), 'website': result.get('website', '')}
        except Exception:
            return {'phone': '', 'website': ''}


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/search', methods=['POST'])
def api_search():
    data = request.json or {}
    industry     = data.get('industry', '').strip()
    location     = data.get('location', '').strip()
    country      = data.get('country', 'United States').strip()
    job_titles   = data.get('job_titles', '').strip()
    company_size = data.get('company_size', 'Any').strip()
    try:
        max_results = max(1, min(50, int(data.get('max_results', 20))))
    except (ValueError, TypeError):
        max_results = 20

    if not industry:
        return jsonify({'error': 'Industry / niche is required'}), 400
    if not location:
        return jsonify({'error': 'Location is required'}), 400
    if not os.getenv('GEMINI_API_KEY') and not os.getenv('GOOGLE_PLACES_API_KEY'):
        return jsonify({'error': 'No search API configured.', 'results': [], 'count': 0}), 503

    finder = LeadFinder()
    results = finder.search(industry, location, country, job_titles, company_size, max_results)
    return jsonify({'results': results, 'count': len(results)})


@app.route('/api/email', methods=['POST'])
def api_email():
    data = request.json or {}
    prospect_name    = data.get('prospect_name', '').strip()
    prospect_company = data.get('prospect_company', '').strip()
    prospect_title   = data.get('prospect_title', '').strip()
    your_offer       = data.get('your_offer', '').strip()
    pain_point       = data.get('pain_point', '').strip()
    tone             = data.get('tone', 'Professional').strip()
    length           = data.get('length', 'Short (3-4 lines)').strip()
    cta              = data.get('cta', 'Book a call').strip()
    sender_name      = data.get('sender_name', '').strip()

    if not prospect_company or not your_offer:
        return jsonify({'error': 'Prospect company and your offer are required'}), 400

    length_map = {
        'Short (3-4 lines)': '3-4 sentences total, ultra concise',
        'Medium (5-7 lines)': '5-7 sentences, balanced detail',
        'Long (full pitch)': '8-10 sentences, full value pitch with proof',
    }
    length_guide = length_map.get(length, '3-4 sentences total')

    try:
        prompt = f"""Write a cold outreach email:

Prospect: {prospect_name or 'the recipient'}{' (' + prospect_title + ')' if prospect_title else ''} at {prospect_company}
Offer: {your_offer}
Their pain point: {pain_point or 'infer a relevant pain point from their industry'}
Tone: {tone}
Length: {length_guide}
CTA: {cta}
Sender: {sender_name or 'the sender'}

Return ONLY valid JSON, nothing else:
{{
  "subject": "Subject line here",
  "body": "Full email body here with actual newlines as \\n",
  "preview_text": "First 90 chars preview snippet"
}}

Rules:
- Subject: specific and curiosity-driven, no clickbait
- Opening line must reference something specific about {prospect_company}
- Never use 'I hope this email finds you well'
- Single clear CTA at the end
- Sign off with {sender_name or 'the sender'}"""

        response = gemini_generate(prompt)
        result = clean_json(response.text)
        return jsonify({'success': True, 'email': result})
    except Exception as e:
        logger.error(f"Email generation failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/mockup', methods=['POST'])
def api_mockup():
    data = request.get_json() or {}
    business_name = data.get('business_name', 'My Business').strip()
    city          = data.get('city', '').strip()
    industry      = data.get('industry', 'business').strip()
    tagline       = data.get('tagline', '').strip()
    primary_color = data.get('primary_color', '#1a1a2e').strip()
    accent_color  = data.get('accent_color', '#e94560').strip()
    phone         = data.get('phone', '').strip()
    address       = data.get('address', '').strip()
    cta_text      = data.get('cta_text', 'Get a Free Quote').strip()
    services      = data.get('services', '').strip()
    unique_value  = data.get('unique_value', '').strip()
    rating        = data.get('rating', '').strip()
    review_count  = data.get('review_count', '').strip()

    location_str = f"{city}" if city else "your area"
    rating_str = f"{rating} stars ({review_count} reviews)" if rating and review_count else (f"{rating} stars" if rating else "5 stars")

    try:
        prompt = f"""You are a world-class web designer and copywriter. Generate a stunning, complete, production-ready single-page HTML website for a local business.

Business Details:
- Name: {business_name}
- Industry: {industry}
- Location: {location_str}
- Tagline: {tagline or f"#1 {industry.title()} in {location_str}"}
- Phone: {phone or "(555) 000-0000"}
- Address: {address or f"123 Main Street, {location_str}"}
- CTA Button Text: {cta_text}
- Key Services: {services or f"Full-service {industry} solutions"}
- Why Choose Us: {unique_value or f"Trusted by hundreds of {location_str} customers"}
- Rating: {rating_str}

DESIGN REQUIREMENTS — follow these exactly:

1. NAVBAR: Fixed top. Dark background ({primary_color}). Business name in white (bold). Phone number in accent color ({accent_color}). Right-side CTA button in {accent_color}.

2. HERO SECTION: Full viewport height. Background: dark gradient using {primary_color} with a large, blurred CSS circle/blob accent in {accent_color} at 20% opacity (use box-shadow or radial-gradient pseudo-element). Large bold white headline (48px+). Subheadline in light gray. Two CTA buttons side by side (primary filled, secondary outlined). Small trust badge row: ★ rating, checkmark "Licensed & Insured", checkmark "Same-Day Service".

3. STATS BAR: Full-width dark strip below hero. 4 stats in a flex row: "500+ Happy Clients", "15+ Years Experience", "100% Satisfaction", "{rating or '4.9'}★ Rating". Each stat: big bold number in {accent_color}, small label below.

4. SERVICES SECTION: White/light background. Section title "Our Services" centered. Grid of 6 service cards (3 columns). Each card: colored icon box in {accent_color} (use a relevant emoji in a rounded square), bold service name, 2-sentence description. Cards have subtle box-shadow and hover lift effect.

5. WHY CHOOSE US: Dark background ({primary_color}). Left side: large heading + paragraph about the business. Right side: 4 checkmark bullet points of key differentiators. Use CSS grid 60/40 split.

6. TESTIMONIALS: Light gray background. "What Our Customers Say" heading. 3 testimonial cards in a row. Each card: white background, rounded corners, 5 gold stars (★★★★★), italic quote (2-3 sentences, specific and believable), customer name in bold, city in gray. Cards have shadow.

7. CONTACT/CTA SECTION: Full-width dark gradient. Centered headline "Ready to Get Started?". Subtext. Large CTA button. Below: 3 contact info items in a row with icons: phone, address, email (use {business_name.lower().replace(" ", "")}@gmail.com as placeholder email).

8. FOOTER: Dark. Business name + tagline. Copyright {business_name} 2024. Simple nav links: Home, Services, About, Contact.

TECHNICAL REQUIREMENTS:
- @import Google Fonts 'Inter' at the very top of <style>
- font-family: 'Inter', sans-serif throughout
- All CSS in a single <style> block — NO external frameworks
- Fully mobile responsive: stack columns on mobile with @media (max-width: 768px)
- Smooth scroll behavior
- Navbar shrinks/darkens on scroll (small JS)
- Hero fade-in animation using @keyframes fadeInUp
- Service cards: translateY(-8px) on hover with transition
- All colors derived from {primary_color} and {accent_color}
- NO placeholder images — use CSS gradients, emoji in styled boxes, or SVG icons inline
- All content must be hyper-realistic and specific to a {industry} business in {location_str}
- Generate real-sounding staff names for testimonials, real-sounding service names for {industry}

Return ONLY the complete HTML document starting with <!DOCTYPE html>. No markdown, no explanation, no commentary before or after."""

        response = gemini_generate(prompt)
        html = response.text.strip()
        if '```' in html:
            parts = html.split('```')
            for part in parts:
                part = part.strip()
                if part.startswith('html'):
                    part = part[4:].strip()
                if part.strip().startswith('<!DOCTYPE') or part.strip().startswith('<html'):
                    html = part.strip()
                    break
        return jsonify({'html': html, 'success': True})
    except Exception as e:
        logger.error(f"Mockup generation error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/sequence', methods=['POST'])
def api_sequence():
    data = request.json or {}
    campaign_name = data.get('campaign_name', '').strip()
    industry      = data.get('industry', '').strip()
    your_offer    = data.get('your_offer', '').strip()
    goal          = data.get('goal', 'Book a Call').strip()
    tone          = data.get('tone', 'Professional').strip()
    sender_name   = data.get('sender_name', '').strip()
    try:
        steps        = max(2, min(10, int(data.get('steps', 5))))
        days_between = max(1, min(30, int(data.get('days_between', 3))))
    except (ValueError, TypeError):
        steps, days_between = 5, 3

    if not industry or not your_offer:
        return jsonify({'error': 'Industry and your offer are required'}), 400

    try:
        prompt = f"""Create a {steps}-step cold email sequence:

Campaign: {campaign_name or f'{industry} Outreach'}
Target: {industry} businesses
Offer: {your_offer}
Goal: {goal}
Tone: {tone}
Days between emails: {days_between}
Sender: {sender_name or 'the sender'}

Return ONLY a JSON array, nothing else:
[
  {{
    "step": 1,
    "day": 0,
    "subject": "Subject line",
    "body": "Full email body with \\n for newlines",
    "purpose": "e.g. Cold intro / Value follow-up / Social proof / Urgency / Break-up"
  }}
]

Rules:
- Step 1 is day 0 (cold intro). Each step adds {days_between} days.
- Vary angle each step: intro → value add → social proof → urgency → closing the loop
- Never repeat the same opener across steps
- Each email under 120 words (step 1 up to 150 words)
- All content specific to {industry} businesses
- Last step is a polite 'closing the loop / last email' breakup
- Return ONLY the JSON array"""

        response = gemini_generate(prompt)
        sequence = clean_json(response.text)
        if not isinstance(sequence, list):
            raise ValueError("Expected list")
        return jsonify({'success': True, 'sequence': sequence, 'count': len(sequence)})
    except Exception as e:
        logger.error(f"Sequence generation failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/export', methods=['POST'])
def api_export():
    data = request.json or {}
    leads = data.get('leads', [])
    fmt   = data.get('format', 'csv')

    if not leads:
        return jsonify({'error': 'No leads to export'}), 400

    fieldnames = ['name', 'contact_name', 'contact_title', 'email', 'phone',
                  'website', 'address', 'employees', 'rating', 'reviews', 'linkedin']

    if fmt == 'csv':
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for lead in leads:
            writer.writerow({k: lead.get(k, '') for k in fieldnames})
        response = make_response(output.getvalue())
        response.headers['Content-Type'] = 'text/csv'
        response.headers['Content-Disposition'] = 'attachment; filename=leads.csv'
        return response
    else:
        response = make_response(json.dumps(leads, indent=2))
        response.headers['Content-Type'] = 'application/json'
        response.headers['Content-Disposition'] = 'attachment; filename=leads.json'
        return response


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'timestamp': time.time()})


@app.route('/api/debug')
def api_debug():
    gemini_key = os.getenv('GEMINI_API_KEY', '')
    places_key = os.getenv('GOOGLE_PLACES_API_KEY', '')
    return jsonify({
        'gemini_key_set': bool(gemini_key),
        'gemini_key_prefix': gemini_key[:8] + '...' if gemini_key else None,
        'places_key_set': bool(places_key),
        'search_mode': 'google_places' if places_key else ('gemini' if gemini_key else 'none'),
        'timestamp': time.time(),
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
