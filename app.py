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
Return ONLY a JSON array with this exact structure:
[\
  {{\
    "name": "Business Name",\
    "address": "123 Main St, {city}",\
    "phone": "(555) 000-0000",\
    "website": "https://example.com or empty string",\
    "rating": 4.5,\
    "reviews": 42\
  }}\
]"""
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

Return ONLY valid JSON:
{{"website": "full URL or empty string", "phone": "phone number or empty string", "email": "email address or empty string"}}"""
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
            analysis_prompt = f"""Analyze this business website and identify the TOP 3 specific pain points.
Business: {name}
Website content: {text}
Return ONLY a JSON array of exactly 3 pain points:
["Specific pain point 1", "Specific pain point 2", "Specific pain point 3"]"""
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
Pain Points:
{pain_points_text or 'None analyzed'}
Write a SHORT (3-4 sentences max) email. Return ONLY the email body text."""
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
        return jsonify({'error': 'No search API configured.', 'results': [], 'count': 0}), 503
    logger.info(f"Search request: {keyword} in {city} (max {max_results})")
    finder = PlaywrightLeadFinder()
    results = finder.search(keyword, city, max_results)
    return jsonify({'results': results, 'count': len(results)})

@app.route('/api/enrich', methods=['POST'])
def api_enrich():
    data = request.json or {}
    businesses = data.get('businesses', [])
    if not businesses:
        return jsonify({'error': 'businesses array required'}), 400
    if not os.getenv('GEMINI_API_KEY'):
        return jsonify({'error': 'GEMINI_API_KEY not configured.', 'results': []}), 503
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

INDUSTRY_THEMES = {
    'restaurant': {
        'primary': '#D4A373', 'secondary': '#FAEDCD', 'accent': '#E63946',
        'bg': '#1a1410', 'card': '#2a2218', 'text': '#FAEDCD', 'muted': '#a89279',
        'font': "'Playfair Display', Georgia, serif",
        'hero_cta': 'View Our Menu', 'hero_cta2': 'Reserve a Table',
        'hero_icon': 'fa-utensils', 'hero_icon2': 'fa-calendar-check',
        'unsplash': 'restaurant,fine-dining,food-plating',
    },
    'default': {
        'primary': '#6366F1', 'secondary': '#EEF2FF', 'accent': '#4338CA',
        'bg': '#0e0e1a', 'card': '#1a1a2e', 'text': '#EEF2FF', 'muted': '#8888bb',
        'font': "'Inter', 'Segoe UI', sans-serif",
        'hero_cta': 'Get Started', 'hero_cta2': 'Learn More',
        'hero_icon': 'fa-phone', 'hero_icon2': 'fa-arrow-down',
        'unsplash': 'business,office,professional',
    },
}

@app.route('/api/mockup', methods=['POST'])
def api_mockup():
    data = request.json
    name = data.get('name', 'Your Business')
    city = data.get('city', '')
    category = data.get('category', 'business')
    phone = data.get('phone', '')
    address = data.get('address', '')
    website = data.get('website', '')
    rating = data.get('rating', '')
    review_count = data.get('review_count', '')
    description = data.get('description', '')
    has_website = bool(website and is_real_website(website))
    category_lower = category.lower().strip()

    MOCKUP_THEMES = {
        'restaurant': {
            'accent': '#e63946', 'gradient': 'linear-gradient(135deg, #1a0a0a 0%, #2d0f0f 100%)',
            'hero': 'https://images.unsplash.com/photo-1517248135467-4c7edcad34c4?w=1600&h=900&fit=crop',
            'about_img': 'https://images.unsplash.com/photo-1414235077428-338989a2e8c0?w=800&h=600&fit=crop',
        },
        'cafe': {
            'accent': '#d4a373', 'gradient': 'linear-gradient(135deg, #1a150f 0%, #2d2418 100%)',
            'hero': 'https://images.unsplash.com/photo-1501339847302-ac426a4a7cbb?w=1600&h=900&fit=crop',
            'about_img': 'https://images.unsplash.com/photo-1495474472287-4d71bcdd2085?w=800&h=600&fit=crop',
        },
        'plumber': {
            'accent': '#0077b6', 'gradient': 'linear-gradient(135deg, #0a1520 0%, #0f2030 100%)',
            'hero': 'https://images.unsplash.com/photo-1585704032915-c3400ca199e7?w=1600&h=900&fit=crop',
            'about_img': 'https://images.unsplash.com/photo-1607472586893-edb57bdc0e39?w=800&h=600&fit=crop',
        },
        'electrician': {
            'accent': '#f4a261', 'gradient': 'linear-gradient(135deg, #1a1408 0%, #2d220e 100%)',
            'hero': 'https://images.unsplash.com/photo-1621905251189-08b45d6a269e?w=1600&h=900&fit=crop',
            'about_img': 'https://images.unsplash.com/photo-1558618666-fcd25c85f82e?w=800&h=600&fit=crop',
        },
        'dentist': {
            'accent': '#48cae4', 'gradient': 'linear-gradient(135deg, #0a1a20 0%, #0f2a35 100%)',
            'hero': 'https://images.unsplash.com/photo-1629909613654-28e377c37b09?w=1600&h=900&fit=crop',
            'about_img': 'https://images.unsplash.com/photo-1588776814546-1ffcf47267a5?w=800&h=600&fit=crop',
        },
        'lawyer': {
            'accent': '#c9a959', 'gradient': 'linear-gradient(135deg, #1a1810 0%, #2d2a1a 100%)',
            'hero': 'https://images.unsplash.com/photo-1589829545856-d10d557cf95f?w=1600&h=900&fit=crop',
            'about_img': 'https://images.unsplash.com/photo-1521791055366-0d553872125f?w=800&h=600&fit=crop',
        },
        'salon': {
            'accent': '#e891b2', 'gradient': 'linear-gradient(135deg, #1a0f14 0%, #2d1a24 100%)',
            'hero': 'https://images.unsplash.com/photo-1560066984-138dadb4c035?w=1600&h=900&fit=crop',
            'about_img': 'https://images.unsplash.com/photo-1522337360788-8b13dee7a37e?w=800&h=600&fit=crop',
        },
        'gym': {
            'accent': '#ef233c', 'gradient': 'linear-gradient(135deg, #1a0a0a 0%, #2d1010 100%)',
            'hero': 'https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=1600&h=900&fit=crop',
            'about_img': 'https://images.unsplash.com/photo-1571902943202-507ec2618e8f?w=1600&h=900&fit=crop',
        },
        'cleaning': {
            'accent': '#00b4d8', 'gradient': 'linear-gradient(135deg, #0a1820 0%, #0f2835 100%)',
            'hero': 'https://images.unsplash.com/photo-1581578731548-c64695cc6952?w=1600&h=900&fit=crop',
            'about_img': 'https://images.unsplash.com/photo-1628177142898-93e36e4e3a50?w=800&h=600&fit=crop',
        },
    }

    DEFAULT_MOCKUP_THEME = {
        'accent': '#00ff88', 'gradient': 'linear-gradient(135deg, #0a0a0a 0%, #1a1a2e 100%)',
        'hero': 'https://images.unsplash.com/photo-1497366216548-37526070297c?w=1600&h=900&fit=crop',
        'about_img': 'https://images.unsplash.com/photo-1497366811353-6870744d04b2?w=800&h=600&fit=crop',
    }

    theme = DEFAULT_MOCKUP_THEME
    for key, val in MOCKUP_THEMES.items():
        if key in category_lower or category_lower in key:
            theme = val
            break

    accent = theme['accent']
    bg_gradient = theme['gradient']
    hero_image_url = theme['hero']
    about_image_url = theme['about_img']

    tagline = 'Trusted ' + category.title() + ' Services in ' + (city or 'Your Area')
    about_text = name + ' is a leading ' + category.lower() + ' provider in ' + (city or 'the area') + '.'
    services = ['Professional Services', 'Free Consultations', 'Emergency Support', 'Custom Solutions', 'Licensed & Insured', 'Satisfaction Guaranteed']
    reviews = [
        {'author': 'Sarah M.', 'text': 'Incredible experience. Professional and exceeded all expectations.', 'stars': 5},
        {'author': 'James R.', 'text': 'Best in the business. Fair pricing and outstanding quality.', 'stars': 5},
        {'author': 'Amanda K.', 'text': 'Used them three times now and they never disappoint. Highly recommended!', 'stars': 5},
    ]

    try:
        client = get_gemini_client()
        mega_prompt = f"""Generate website content for "{name}", a {category} business in {city or 'a local area'}.
Return ONLY valid JSON (no markdown):
{{"tagline": "punchy 3-8 word tagline", "about": "2 sentences about this business", "services": ["service 1", "service 2", "service 3", "service 4", "service 5", "service 6"], "reviews": [{{"author": "First L.", "text": "realistic review", "stars": 5}}, {{"author": "First L.", "text": "realistic review", "stars": 5}}, {{"author": "First L.", "text": "realistic review", "stars": 5}}]}}"""
        response = client.generate_content(mega_prompt)
        raw = response.text.strip().replace('```json', '').replace('```', '').replace('json\n', '').strip()
        content = json.loads(raw)
        if content.get('tagline'): tagline = content['tagline']
        if content.get('about'): about_text = content['about']
        if content.get('services') and len(content['services']) >= 4: services = content['services'][:6]
        if content.get('reviews') and len(content['reviews']) >= 2: reviews = content['reviews'][:3]
    except Exception as e:
        logger.error(f"Gemini content generation failed for {name}: {e}")

    rating_html = ''
    if rating:
        try:
            rating_val = float(rating)
            stars_full = int(rating_val)
            stars_html = '<i class="fa fa-star"></i>' * stars_full
            count_str = f' ({review_count} reviews)' if review_count else ''
            rating_html = f'<div class="hero-rating">{stars_html} <span>{rating_val:.1f}{count_str}</span></div>'
        except (ValueError, TypeError):
            pass

    service_icons = ['fa-star', 'fa-cog', 'fa-check-circle', 'fa-bolt', 'fa-heart', 'fa-gem']
    services_html = ''
    for i, svc in enumerate(services[:6]):
        icon = service_icons[i % len(service_icons)]
        services_html += '<div class="service-card"><i class="fa ' + icon + '" style="font-size:2rem;color:' + accent + ';margin-bottom:15px;"></i><h3>' + str(svc) + '</h3></div>'

    reviews_html = ''
    for rev in reviews[:3]:
        author = rev.get('author', 'Happy Customer') if isinstance(rev, dict) else 'Happy Customer'
        text = rev.get('text', 'Excellent service!') if isinstance(rev, dict) else str(rev)
        rev_stars = rev.get('stars', 5) if isinstance(rev, dict) else 5
        rev_stars_html = '<i class="fa fa-star" style="color:#ffd700;"></i>' * int(rev_stars)
        reviews_html += '<div class="review-card"><div style="margin-bottom:10px;">' + rev_stars_html + '</div><p style="font-style:italic;margin-bottom:12px;color:#ccc;">"' + text + '"</p><p style="font-weight:600;color:' + accent + ';">- ' + author + '</p></div>'

    map_query = urllib.parse.quote((name + ' ' + city) if city else name)

    contact_details = ''
    if phone:
        contact_details += '<p><i class="fa fa-phone" style="color:' + accent + ';margin-right:10px;"></i> ' + phone + '</p>'
    if address:
        contact_details += '<p><i class="fa fa-map-marker" style="color:' + accent + ';margin-right:10px;"></i> ' + address + '</p>'
    if city:
        contact_details += '<p><i class="fa fa-building" style="color:' + accent + ';margin-right:10px;"></i> ' + city + '</p>'

    cta_primary = '<a href="tel:' + phone + '" class="cta-btn">Call Now</a>' if phone else '<a href="#contact" class="cta-btn">Get in Touch</a>'
    cta_secondary = '<a href="' + website + '" class="cta-btn-outline" target="_blank">Visit Website</a>' if has_website else ''

    html = '<!DOCTYPE html><html lang="en"><head>'
    html += '<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">'
    html += '<title>' + name + ' | ' + (city or category.title()) + '</title>'
    html += '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/4.7.0/css/font-awesome.min.css">'
    html += '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">'
    html += '<style>'
    html += '* { margin:0; padding:0; box-sizing:border-box; }'
    html += 'body { font-family:"Inter",sans-serif; background:' + bg_gradient + '; color:#e0e0e0; overflow-x:hidden; }'
    html += 'a { text-decoration:none; color:inherit; }'
    html += '.nav { position:fixed; top:0; left:0; right:0; z-index:1000; padding:18px 40px; display:flex; justify-content:space-between; align-items:center; background:rgba(10,10,10,0.85); backdrop-filter:blur(20px); border-bottom:1px solid rgba(255,255,255,0.05); }'
    html += '.nav-brand { font-size:1.4rem; font-weight:800; color:#fff; }'
    html += '.nav-brand span { color:' + accent + '; }'
    html += '.nav-links { display:flex; gap:28px; }'
    html += '.nav-links a { color:#aaa; font-weight:500; font-size:0.9rem; transition:color 0.3s; }'
    html += '.nav-links a:hover { color:' + accent + '; }'
    html += '.hero { position:relative; min-height:100vh; display:flex; align-items:center; justify-content:center; text-align:center; background:linear-gradient(to bottom, rgba(0,0,0,0.6), rgba(0,0,0,0.8)), url("' + hero_image_url + '") center/cover no-repeat; }'
    html += '.hero-content { max-width:800px; padding:0 20px; }'
    html += '.hero h1 { font-size:3.5rem; font-weight:900; line-height:1.1; margin-bottom:16px; color:#fff; }'
    html += '.hero h1 span { color:' + accent + '; }'
    html += '.hero-tagline { font-size:1.3rem; color:#bbb; margin-bottom:10px; font-weight:300; }'
    html += '.hero-rating { margin-top:12px; color:#ffd700; font-size:1.1rem; }'
    html += '.hero-rating span { color:#fff; margin-left:8px; font-weight:600; }'
    html += '.hero-ctas { margin-top:30px; display:flex; gap:15px; justify-content:center; flex-wrap:wrap; }'
    html += '.cta-btn { display:inline-block; padding:14px 36px; background:' + accent + '; color:#000; font-weight:700; font-size:1rem; border-radius:50px; transition:transform 0.3s, box-shadow 0.3s; }'
    html += '.cta-btn:hover { transform:translateY(-2px); box-shadow:0 8px 30px ' + accent + '66; }'
    html += '.cta-btn-outline { display:inline-block; padding:14px 36px; border:2px solid ' + accent + '; color:' + accent + '; font-weight:700; font-size:1rem; border-radius:50px; transition:all 0.3s; }'
    html += '.cta-btn-outline:hover { background:' + accent + '; color:#000; }'
    html += 'section { padding:80px 40px; max-width:1200px; margin:0 auto; }'
    html += '.section-title { text-align:center; font-size:2.2rem; font-weight:800; color:#fff; margin-bottom:50px; }'
    html += '.section-title span { color:' + accent + '; }'
    html += '.services-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:25px; }'
    html += '.service-card { background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.06); border-radius:16px; padding:35px 25px; text-align:center; transition:transform 0.3s, border-color 0.3s; }'
    html += '.service-card:hover { transform:translateY(-5px); border-color:' + accent + '44; }'
    html += '.service-card h3 { font-size:1.1rem; font-weight:600; color:#fff; }'
    html += '.about-grid { display:grid; grid-template-columns:1fr 1fr; gap:50px; align-items:center; }'
    html += '.about-text p { font-size:1.1rem; line-height:1.8; color:#bbb; }'
    html += '.about-img { border-radius:16px; overflow:hidden; }'
    html += '.about-img img { width:100%; height:400px; object-fit:cover; }'
    html += '.reviews-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:25px; }'
    html += '.review-card { background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.06); border-radius:16px; padding:30px; }'
    html += '.contact-grid { display:grid; grid-template-columns:1fr 1fr; gap:50px; }'
    html += '.contact-info { display:flex; flex-direction:column; gap:15px; font-size:1.05rem; }'
    html += '.footer { text-align:center; padding:30px; color:#555; font-size:0.85rem; border-top:1px solid rgba(255,255,255,0.05); }'
    html += '@media (max-width:768px) { .hero h1 { font-size:2.2rem; } .about-grid, .contact-grid { grid-template-columns:1fr; } .nav-links { display:none; } section { padding:60px 20px; } }'
    html += '</style></head><body>'

    first_letter = name[0] if name else 'B'
    rest_name = name[1:] if len(name) > 1 else ''
    html += '<nav class="nav"><div class="nav-brand"><span>' + first_letter + '</span>' + rest_name + '</div>'
    html += '<div class="nav-links"><a href="#services">Services</a><a href="#about">About</a><a href="#reviews">Reviews</a><a href="#contact">Contact</a></div></nav>'

    html += '<section class="hero"><div class="hero-content">'
    html += '<h1><span>' + name + '</span></h1>'
    html += '<p class="hero-tagline">' + tagline + '</p>'
    html += rating_html
    html += '<div class="hero-ctas">' + cta_primary + cta_secondary + '</div>'
    html += '</div></section>'

    html += '<section id="services"><h2 class="section-title">What We <span>Offer</span></h2>'
    html += '<div class="services-grid">' + services_html + '</div></section>'

    html += '<section id="about"><h2 class="section-title">About <span>' + name + '</span></h2>'
    html += '<div class="about-grid"><div class="about-text"><p>' + about_text + '</p></div>'
    html += '<div class="about-img"><img src="' + about_image_url + '" alt="About ' + name + '" loading="lazy"></div></div></section>'

    html += '<section id="reviews"><h2 class="section-title">What People <span>Say</span></h2>'
    html += '<div class="reviews-grid">' + reviews_html + '</div></section>'

    html += '<section id="contact"><h2 class="section-title">Get In <span>Touch</span></h2>'
    html += '<div class="contact-grid"><div class="contact-info">' + contact_details
    html += '<iframe src="https://www.google.com/maps?q=' + map_query + '&output=embed" width="100%" height="300" style="border:0;border-radius:12px;" allowfullscreen="" loading="lazy"></iframe>'
    html += '</div><div><form style="display:flex;flex-direction:column;gap:15px;">'
    html += '<input type="text" placeholder="Your Name" style="padding:14px;border-radius:10px;border:1px solid rgba(255,255,255,0.1);background:rgba(255,255,255,0.05);color:#fff;font-size:1rem;">'
    html += '<input type="email" placeholder="Your Email" style="padding:14px;border-radius:10px;border:1px solid rgba(255,255,255,0.1);background:rgba(255,255,255,0.05);color:#fff;font-size:1rem;">'
    html += '<textarea placeholder="Your Message" rows="4" style="padding:14px;border-radius:10px;border:1px solid rgba(255,255,255,0.1);background:rgba(255,255,255,0.05);color:#fff;font-size:1rem;resize:vertical;"></textarea>'
    html += '<button type="submit" class="cta-btn" style="border:none;cursor:pointer;font-family:inherit;">Send Message</button>'
    html += '</form></div></div></section>'

    html += '<footer class="footer"><p>2025 ' + name + '. All rights reserved. | Powered by <a href="#" style="color:' + accent + ';">Cold Outreach Engine</a></p></footer>'
    html += '</body></html>'

    return jsonify({'html': html})

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'timestamp': time.time()})

@app.route('/api/debug', methods=['GET'])
def api_debug():
    gemini_key = os.getenv('GEMINI_API_KEY', '')
    places_key = os.getenv('GOOGLE_PLACES_API_KEY', '')
    return jsonify({
        'gemini_key_set': bool(gemini_key),
        'gemini_key_prefix': gemini_key[:8] + '...' if gemini_key else None,
        'places_key_set': bool(places_key),
        'places_key_prefix': places_key[:8] + '...' if places_key else None,
        'search_mode': 'google_places' if places_key else ('gemini_fallback' if gemini_key else 'none'),
        'timestamp': time.time(),
    })

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
