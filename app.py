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

class PlaywrightLeadFinder:
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
        api_key = os.getenv('GOOGLE_PLACES_API_KEY')
        if api_key:
            return self._search_with_api(keyword, city, max_results, api_key)
        logger.info(f"No GOOGLE_PLACES_API_KEY set. Using Gemini to generate leads for: {keyword} in {city}")
        return self._search_with_gemini(keyword, city, max_results)

    def _search_with_gemini(self, keyword: str, city: str, max_results: int) -> list:
        try:
            client = get_gemini_client()
            prompt = f"""Generate a list of {min(max_results, 20)} realistic local {keyword} businesses in {city}.
These should look like real local businesses (not chains) with plausible names, addresses, phone numbers, and websites.
Some businesses should have no website (website: "") to simulate real-world data.
Return ONLY a JSON array with this exact structure, no explanation:
[\
  {{\
    "name": "Business Name",\
    "address": "123 Main St, {city}",\
    "phone": "(555) 000-0000",\
    "website": "https://example.com or empty string",\
    "rating": 4.5,\
    "reviews": 42\
  }}\
]
Rules:
- Mix of businesses with and without websites (about 40% no website)
- Realistic local business names (not chains like McDonald's)
- Plausible phone numbers for {city}
- Ratings between 3.8 and 5.0, reviews between 5 and 300
- Return ONLY the JSON array"""
            response = client.generate_content(prompt)
            raw = response.text.strip().replace('```json', '').replace('```', '').strip()
            businesses = json.loads(raw)
            if not isinstance(businesses, list):
                raise ValueError("Expected a list")
            logger.info(f"Gemini generated {len(businesses)} leads for {keyword} in {city}")
            return businesses[:max_results]
        except Exception as e:
            logger.error(f"Gemini lead generation failed: {e}")
            return []

    def _search_with_api(self, keyword: str, city: str, max_results: int, api_key: str) -> list:
        query = f"{keyword} in {city}"
        url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
        logger.info(f"Using Google Places API: {query}")
        params = {'query': query, 'key': api_key}
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            businesses = []
            for result in data.get('results', [])[:max_results]:
                place_id = result.get('place_id')
                details = self._get_place_details(place_id, api_key)
                businesses.append({
                    'name': result.get('name', ''),
                    'address': result.get('formatted_address', ''),
                    'phone': details.get('phone', ''),
                    'website': details.get('website', ''),
                    'rating': result.get('rating'),
                    'reviews': result.get('user_ratings_total'),
                })
            return businesses
        except Exception as e:
            logger.error(f"Google Places API failed: {e}")
            return []

    def _get_place_details(self, place_id: str, api_key: str) -> dict:
        url = "https://maps.googleapis.com/maps/api/place/details/json"
        params = {'place_id': place_id, 'fields': 'formatted_phone_number,website', 'key': api_key}
        try:
            resp = requests.get(url, params=params, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            result = data.get('result', {})
            return {'phone': result.get('formatted_phone_number', ''), 'website': result.get('website', '')}
        except Exception as e:
            logger.error(f"Failed to get place details: {e}")
            return {'phone': '', 'website': ''}

def enrich_with_ai(business: dict) -> dict:
    client = get_gemini_client()
    name = business.get('name', '')
    address = business.get('address', '')
    website = business.get('website', '')

    if not website or not business.get('phone') or not business.get('email'):
        search_prompt = f"""Find contact information for this business:
- Name: {name}
- Address: {address}

Return ONLY valid, working information in this exact JSON format:
{{
    "website": "full URL or empty string",
    "phone": "phone number or empty string",
    "email": "email address or empty string"
}}
Rules:
- Website must be a real, dedicated business website (not Facebook, Instagram, Wix placeholder, etc.)
- Only include information you're confident is correct
- Use empty strings if you can't find valid info
- Return ONLY the JSON, no explanation
"""
        try:
            response = client.generate_content(search_prompt)
            contact_data = json.loads(response.text.strip().replace('```json', '').replace('```', ''))
            if contact_data.get('website') and is_real_website(contact_data['website']):
                business['website'] = contact_data['website']
                website = contact_data['website']
            if contact_data.get('phone') and not business.get('phone'):
                business['phone'] = contact_data['phone']
            if contact_data.get('email') and not business.get('email'):
                business['email'] = contact_data['email']
        except Exception as e:
            logger.warning(f"AI contact search failed for {name}: {e}")

    if website and is_real_website(website):
        try:
            resp = requests.get(website, headers=PlaywrightLeadFinder.HEADERS, timeout=8)
            soup = BeautifulSoup(resp.text, 'html.parser')
            text = soup.get_text(separator=' ', strip=True)[:3000]
            analysis_prompt = f"""Analyze this business website and identify the TOP 3 specific pain points or improvement opportunities.
Business: {name}
Website content: {text}
Focus on: missing features, poor UX, outdated design, missing marketing elements, competition advantages.
Return ONLY a JSON array of exactly 3 pain points:
["Specific pain point 1", "Specific pain point 2", "Specific pain point 3"]
Return ONLY the JSON array, no explanation."""
            response = client.generate_content(analysis_prompt)
            pain_points = json.loads(response.text.strip().replace('```json', '').replace('```', ''))
            business['pain_points'] = pain_points
        except Exception as e:
            logger.warning(f"Website analysis failed for {name}: {e}")
            business['pain_points'] = []
    else:
        business['pain_points'] = []

    pain_points_text = '\n'.join(f"- {p}" for p in business.get('pain_points', []))
    outreach_prompt = f"""Write a personalized cold email for this business:
Business: {name}
Location: {address}
Website: {website or 'None found'}
Pain Points Identified:
{pain_points_text or 'None analyzed'}
Write a SHORT (3-4 sentences max), highly personalized email that:
1. References something specific about their business
2. Mentions ONE specific pain point or opportunity
3. Offers a clear, relevant solution
4. Ends with a simple call-to-action
Tone: Professional but conversational, helpful not salesy.
Return ONLY the email body text, no subject line, no JSON."""
    try:
        response = client.generate_content(outreach_prompt)
        business['outreach_email'] = response.text.strip()
    except Exception as e:
        logger.warning(f"Email generation failed for {name}: {e}")
        business['outreach_email'] = ""

    return business

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/search', methods=['POST'])
def api_search():
    data = request.json or {}
    keyword = data.get('keyword', '').strip()
    city = data.get('city', '').strip()
    try:
        max_results = max(1, min(50, int(data.get('max_results', 20))))
    except (ValueError, TypeError):
        max_results = 20
    if not keyword or not city:
        return jsonify({'error': 'keyword and city are required'}), 400
    has_places_key = bool(os.getenv('GOOGLE_PLACES_API_KEY'))
    has_gemini_key = bool(os.getenv('GEMINI_API_KEY'))
    if not has_places_key and not has_gemini_key:
        return jsonify({
            'error': 'No search API configured. Please add GOOGLE_PLACES_API_KEY or GEMINI_API_KEY.',
            'results': [],
            'count': 0
        }), 503
    logger.info(f"Search request: {keyword} in {city} (max {max_results})")
    finder = PlaywrightLeadFinder()
    results = finder.search(keyword, city, max_results)
    if not results and not has_places_key and has_gemini_key:
        return jsonify({
            'error': 'Search failed -- Gemini API quota may be exhausted. Try again tomorrow or add a Google Places API key.',
            'results': [],
            'count': 0
        }), 503
    return jsonify({'results': results, 'count': len(results)})

@app.route('/api/enrich', methods=['POST'])
def api_enrich():
    data = request.json or {}
    businesses = data.get('businesses', [])
    if not businesses:
        return jsonify({'error': 'businesses array required'}), 400
    if not os.getenv('GEMINI_API_KEY'):
        return jsonify({
            'error': 'GEMINI_API_KEY not configured. Enrichment requires Gemini.',
            'results': []
        }), 503
    logger.info(f"Enriching {len(businesses)} businesses with AI")
    enriched = []
    for biz in businesses:
        try:
            enriched.append(enrich_with_ai(biz))
        except Exception as e:
            logger.error(f"Enrichment failed for {biz.get('name')}: {e}")
            biz['error'] = str(e)
            enriched.append(biz)
    return jsonify({'results': enriched, 'count': len(enriched)})

@app.route('/api/export', methods=['POST'])
def api_export():
    data = request.json
    businesses = data.get('businesses', [])
    export_format = data.get('format', 'csv')
    if export_format == 'csv':
        output = io.StringIO()
        fieldnames = ['name', 'address', 'phone', 'email', 'website', 'rating', 'reviews', 'pain_points', 'outreach_email']
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for biz in businesses:
            row = {k: biz.get(k, '') for k in fieldnames}
            if isinstance(row['pain_points'], list):
                row['pain_points'] = '; '.join(row['pain_points'])
            writer.writerow(row)
        response = make_response(output.getvalue())
        response.headers['Content-Type'] = 'text/csv'
        response.headers['Content-Disposition'] = 'attachment; filename=leads.csv'
        return response
    else:
        response = make_response(json.dumps(businesses, indent=2))
        response.headers['Content-Type'] = 'application/json'
        response.headers['Content-Disposition'] = 'attachment; filename=leads.json'
        return response


@app.route('/api/mockup', methods=['POST'])
def generate_mockup():
    try:
        data = request.get_json()
        business_name = data.get('business_name', 'My Business')
        city = data.get('city', '')
        category = data.get('category', 'business').lower()
        phone = data.get('phone', '')
        address = data.get('address', '')
        rating = float(data.get('rating', 4.5))
        reviews = int(data.get('reviews', 50))
        description = data.get('description', '')

        # Category themes
        themes = {
            'restaurant': {
                'primary': '#FF6B35', 'secondary': '#2C1810', 'accent': '#FFF5F0',
                'text': '#1a1a1a', 'bg': '#FFFAF7',
                'image': 'https://source.unsplash.com/1200x700/?restaurant,dining,food',
                'services': [('\U0001f37d\ufe0f', 'Fine Dining', 'Exquisite meals crafted by award-winning chefs'),
                             ('\U0001f942', 'Private Events', 'Perfect venue for celebrations and corporate events'),
                             ('\U0001f69a', 'Fast Delivery', 'Hot food delivered to your door in 30 minutes')],
                'reviews_data': [('James R.', 5, 'Absolutely incredible food. Best dining experience in the city!'),
                                 ('Maria S.', 5, 'The atmosphere is magical and the pasta is to die for.'),
                                 ('Tom K.', 4, 'Great service, amazing food. Will definitely be back!')],
            },
            'dental': {
                'primary': '#0EA5E9', 'secondary': '#0369A1', 'accent': '#F0F9FF',
                'text': '#0F172A', 'bg': '#F8FAFC',
                'image': 'https://source.unsplash.com/1200x700/?dental,clinic,smile',
                'services': [('\U0001f9b7', 'General Dentistry', 'Comprehensive care for your whole family'),
                             ('\u2728', 'Teeth Whitening', 'Professional whitening for a radiant smile'),
                             ('\U0001f48e', 'Cosmetic Dentistry', 'Veneers, implants and smile makeovers')],
                'reviews_data': [('Lisa M.', 5, 'Best dentist I have ever been to. Completely painless!'),
                                 ('David P.', 5, 'Amazing staff and state-of-the-art equipment.'),
                                 ('Sarah T.', 5, 'My smile has never looked better. Highly recommend!')],
            },
            'fitness': {
                'primary': '#8B5CF6', 'secondary': '#5B21B6', 'accent': '#1a1a2e',
                'text': '#ffffff', 'bg': '#0F0F1A',
                'image': 'https://source.unsplash.com/1200x700/?gym,fitness,workout',
                'services': [('\U0001f4aa', 'Personal Training', 'One-on-one sessions with certified trainers'),
                             ('\U0001f3cb\ufe0f', 'Group Classes', '50+ weekly classes from yoga to HIIT'),
                             ('\U0001f957', 'Nutrition Coaching', 'Custom meal plans to fuel your goals')],
                'reviews_data': [('Mike D.', 5, 'Lost 30 lbs in 3 months. This gym changed my life!'),
                                 ('Ashley K.', 5, 'The trainers are world-class and so motivating.'),
                                 ('Ryan B.', 5, 'Best gym in the city. Worth every penny!')],
            },
        }

        # Match category
        theme = themes.get('restaurant')
        for key in themes:
            if key in category or category in key:
                theme = themes[key]
                break
        if 'gym' in category or 'crossfit' in category or 'yoga' in category:
            theme = themes['fitness']
        if 'dent' in category or 'ortho' in category:
            theme = themes['dental']

        # Build star string
        full = int(rating)
        half = 1 if rating - full >= 0.5 else 0
        empty = 5 - full - half
        stars_html = '<span style="color:#FFB800;font-size:22px;">' + '\u2605' * full + ('\u00bd' if half else '') + '\u2606' * empty + '</span>'

        # Build services HTML
        services_html = ''
        for icon, title, desc in theme['services']:
            services_html += f'''
            <div style="background:white;border-radius:16px;padding:36px 28px;box-shadow:0 4px 24px rgba(0,0,0,0.08);text-align:center;transition:transform 0.2s;" onmouseover="this.style.transform='translateY(-6px)'" onmouseout="this.style.transform='none'">
                <div style="font-size:48px;margin-bottom:16px;">{icon}</div>
                <h3 style="font-size:18px;font-weight:700;margin-bottom:10px;color:{theme['secondary']};">{title}</h3>
                <p style="color:#666;line-height:1.6;font-size:15px;">{desc}</p>
            </div>'''

        # Build reviews HTML
        reviews_html = ''
        star_char = chr(0x2605)
        for name, stars, text in theme['reviews_data']:
            reviews_html += f'''
            <div style="background:white;border-radius:16px;padding:32px;box-shadow:0 4px 20px rgba(0,0,0,0.07);">
                <div style="color:#FFB800;font-size:18px;margin-bottom:12px;">{star_char * stars}</div>
                <p style="color:#444;line-height:1.7;font-size:15px;margin-bottom:20px;">"{text}"</p>
                <div style="display:flex;align-items:center;gap:12px;">
                    <div style="width:42px;height:42px;border-radius:50%;background:linear-gradient(135deg,{theme['primary']},{theme['secondary']});display:flex;align-items:center;justify-content:center;color:white;font-weight:700;">{name[0]}</div>
                    <span style="font-weight:600;color:{theme['secondary']};">{name}</span>
                </div>
            </div>'''

        phone_emoji = chr(0x1f4de)
        address_emoji = chr(0x1f4cd)
        html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{business_name}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=Playfair+Display:wght@700;800&display=swap" rel="stylesheet">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:'Inter',sans-serif; background:{theme['bg']}; color:{theme['text']}; }}
  nav {{ position:fixed; top:0; left:0; right:0; z-index:100; background:rgba(255,255,255,0.95); backdrop-filter:blur(12px); border-bottom:1px solid rgba(0,0,0,0.08); padding:0 40px; height:68px; display:flex; align-items:center; justify-content:space-between; box-shadow:0 2px 20px rgba(0,0,0,0.06); }}
  nav .brand {{ font-family:'Playfair Display',serif; font-size:22px; font-weight:700; color:{theme['primary']}; }}
  nav ul {{ list-style:none; display:flex; gap:32px; }}
  nav ul li a {{ text-decoration:none; color:{theme['secondary']}; font-weight:500; font-size:15px; transition:color 0.2s; }}
  nav ul li a:hover {{ color:{theme['primary']}; }}
  .cta-btn {{ background:{theme['primary']}; color:white; padding:10px 24px; border-radius:100px; font-weight:600; font-size:14px; text-decoration:none; transition:opacity 0.2s; }}
  .cta-btn:hover {{ opacity:0.88; }}
  section {{ padding:90px 40px; max-width:1100px; margin:0 auto; }}
</style>
</head>
<body>

<nav>
  <div class="brand">{business_name}</div>
  <ul>
    <li><a href="#">Home</a></li>
    <li><a href="#services">Services</a></li>
    <li><a href="#reviews">Reviews</a></li>
    <li><a href="#contact">Contact</a></li>
  </ul>
  <a href="#contact" class="cta-btn">Book Now</a>
</nav>

<!-- HERO -->
<div style="height:100vh;min-height:600px;position:relative;display:flex;align-items:center;justify-content:center;overflow:hidden;background:{theme['secondary']};">
  <img src="{theme['image']}" alt="hero" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0.35;">
  <div style="position:relative;z-index:2;text-align:center;padding:40px;max-width:800px;">
    <div style="display:inline-block;background:rgba(255,255,255,0.15);border:1px solid rgba(255,255,255,0.3);color:white;padding:6px 18px;border-radius:100px;font-size:13px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:24px;">{city}</div>
    <h1 style="font-family:'Playfair Display',serif;font-size:clamp(44px,7vw,80px);font-weight:800;color:white;line-height:1.08;margin-bottom:20px;text-shadow:0 2px 20px rgba(0,0,0,0.3);">{business_name}</h1>
    <p style="font-size:clamp(16px,2vw,20px);color:rgba(255,255,255,0.85);margin-bottom:16px;line-height:1.6;">{description or f'Serving {city} with excellence and passion.'}</p>
    <div style="display:flex;align-items:center;justify-content:center;gap:10px;margin-bottom:36px;">{stars_html}<span style="color:rgba(255,255,255,0.8);font-size:15px;">({reviews:,} reviews)</span></div>
    <div style="display:flex;gap:16px;justify-content:center;flex-wrap:wrap;">
      <a href="#contact" style="background:{theme['primary']};color:white;padding:16px 36px;border-radius:100px;font-weight:700;font-size:16px;text-decoration:none;box-shadow:0 8px 32px rgba(0,0,0,0.2);">Get Started</a>
      <a href="#services" style="background:rgba(255,255,255,0.15);color:white;padding:16px 36px;border-radius:100px;font-weight:600;font-size:16px;text-decoration:none;border:1px solid rgba(255,255,255,0.4);">Learn More</a>
    </div>
  </div>
</div>

<!-- SERVICES -->
<section id="services" style="padding:90px 40px;max-width:1100px;margin:0 auto;">
  <div style="text-align:center;margin-bottom:56px;">
    <h2 style="font-family:'Playfair Display',serif;font-size:clamp(32px,4vw,48px);font-weight:700;color:{theme['secondary']};margin-bottom:14px;">What We Offer</h2>
    <p style="color:#888;font-size:17px;">Everything you need, all in one place</p>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:28px;">
    {services_html}
  </div>
</section>

<!-- REVIEWS -->
<section id="reviews" style="padding:90px 40px;background:{theme['accent']};max-width:100%;">
  <div style="max-width:1100px;margin:0 auto;">
    <div style="text-align:center;margin-bottom:56px;">
      <h2 style="font-family:'Playfair Display',serif;font-size:clamp(32px,4vw,48px);font-weight:700;color:{theme['secondary']};margin-bottom:14px;">What Our Customers Say</h2>
      <div style="display:flex;align-items:center;justify-content:center;gap:10px;">{stars_html}<span style="font-size:16px;color:#555;">{rating} out of 5 \u00b7 {reviews:,} reviews</span></div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:24px;">
      {reviews_html}
    </div>
  </div>
</section>

<!-- CONTACT -->
<section id="contact" style="padding:90px 40px;max-width:1100px;margin:0 auto;text-align:center;">
  <h2 style="font-family:'Playfair Display',serif;font-size:clamp(32px,4vw,48px);font-weight:700;color:{theme['secondary']};margin-bottom:48px;">Get In Touch</h2>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:24px;margin-bottom:48px;">
    {'<div style="background:white;border-radius:16px;padding:32px;box-shadow:0 4px 20px rgba(0,0,0,0.07);"><div style="font-size:36px;margin-bottom:12px;">' + phone_emoji + '</div><h3 style="font-weight:600;margin-bottom:8px;">Phone</h3><p style="color:#666;">' + phone + '</p></div>' if phone else ''}
    {'<div style="background:white;border-radius:16px;padding:32px;box-shadow:0 4px 20px rgba(0,0,0,0.07);"><div style="font-size:36px;margin-bottom:12px;">' + address_emoji + '</div><h3 style="font-weight:600;margin-bottom:8px;">Address</h3><p style="color:#666;">' + address + '</p></div>' if address else ''}
    <div style="background:white;border-radius:16px;padding:32px;box-shadow:0 4px 20px rgba(0,0,0,0.07);"><div style="font-size:36px;margin-bottom:12px;">\u2b50</div><h3 style="font-weight:600;margin-bottom:8px;">Rating</h3><p style="color:#666;">{rating}/5 ({reviews:,} reviews)</p></div>
  </div>
  <a href="{'https://maps.google.com/?q=' + address.replace(' ','+') if address else '#'}" target="_blank" style="background:{theme['primary']};color:white;padding:18px 48px;border-radius:100px;font-weight:700;font-size:17px;text-decoration:none;display:inline-block;box-shadow:0 8px 32px rgba(0,0,0,0.15);">Get Directions</a>
</section>

<!-- FOOTER -->
<footer style="background:{theme['secondary']};color:rgba(255,255,255,0.7);text-align:center;padding:32px;font-size:14px;">
  \u00a9 2024 {business_name} \u00b7 {city} \u00b7 All rights reserved
</footer>

</body>
</html>'''

        return jsonify({'html': html, 'success': True})
    except Exception as e:
        logger.error(f"Mockup generation error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'timestamp': time.time()})

@app.route('/api/debug', methods=['GET'])
def api_debug():
    """Diagnostic endpoint -- shows env var and config status (no API calls)."""
    gemini_key = os.getenv('GEMINI_API_KEY', '')
    places_key = os.getenv('GOOGLE_PLACES_API_KEY', '')
    return jsonify({
        'gemini_key_set': bool(gemini_key),
        'gemini_key_prefix': gemini_key[:8] + '...' if gemini_key else None,
        'places_key_set': bool(places_key),
        'places_key_prefix': places_key[:8] + '...' if places_key else None,
        'search_mode': 'google_places' if places_key else ('gemini_fallback' if gemini_key else 'none'),
        'has_gemini_fallback': hasattr(PlaywrightLeadFinder, '_search_with_gemini'),
        'timestamp': time.time(),
    })

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
