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

_gemini_client = None

def get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            raise ValueError("GEMINI_API_KEY environment variable not set")
        genai.configure(api_key=api_key)
        _gemini_client = genai.GenerativeModel('gemini-2.0-flash')
    return _gemini_client

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
            client = get_gemini_client()
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
            response = client.generate_content(prompt)
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
        client = get_gemini_client()
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

        response = client.generate_content(prompt)
        result = clean_json(response.text)
        return jsonify({'success': True, 'email': result})
    except Exception as e:
        logger.error(f"Email generation failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/mockup', methods=['POST'])
def api_mockup():
    data = request.get_json() or {}
    business_name = data.get('business_name', 'My Business').strip()
    tagline       = data.get('tagline', '').strip()
    industry      = data.get('industry', 'business').strip()
    primary_color = data.get('primary_color', '#6366f1').strip()
    accent_color  = data.get('accent_color', '#a855f7').strip()
    phone         = data.get('phone', '').strip()
    address       = data.get('address', '').strip()
    cta_text      = data.get('cta_text', 'Get a Free Quote').strip()
    services      = data.get('services', '').strip()
    unique_value  = data.get('unique_value', '').strip()

    try:
        client = get_gemini_client()
        prompt = f"""You are an expert web designer. Generate a complete, beautiful, single-page HTML website mockup.

Business Details:
- Name: {business_name}
- Industry: {industry}
- Tagline: {tagline or f'The best {industry} in town'}
- Primary Color: {primary_color}
- Accent Color: {accent_color}
- Phone: {phone or '(555) 000-0000'}
- Address: {address or '123 Main Street'}
- CTA Button Text: {cta_text}
- Services/Products: {services or f'Premium {industry} services'}
- Unique Value Prop: {unique_value or f'Top-rated {industry} with 5-star reviews'}

Build a full HTML page with:
1. Sticky nav bar with business name and CTA button
2. Hero section: bold headline from tagline, subheadline, CTA button, subtle CSS background shape
3. Services grid: 3-4 service cards with icons (use emoji), title, short description
4. Social proof: 3 testimonials with name, role, quote, 5-star rating in stars (★★★★★)
5. Contact section: phone, address, CTA button
6. Footer with copyright

Technical requirements:
- All CSS embedded in <style> tag (no external CSS frameworks)
- Google Fonts: import Inter or Poppins at top
- Primary color {primary_color} for buttons, headings, nav
- Accent color {accent_color} for gradients, highlights, hover states
- Fully mobile responsive with CSS grid/flexbox
- Smooth fade-in animation on hero using @keyframes
- NO placeholder images — use CSS gradients or emoji icons instead
- Content must be realistic and specific to a {industry} business

Return ONLY the complete HTML document starting with <!DOCTYPE html>. No markdown, no explanation."""

        response = client.generate_content(prompt)
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
        client = get_gemini_client()
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

        response = client.generate_content(prompt)
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
