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


# ---------------------------------------------------------------------------
# INDUSTRY THEME ENGINE
# ---------------------------------------------------------------------------
INDUSTRY_THEMES = {
    'restaurant': {
        'primary': '#D4A373', 'secondary': '#FAEDCD', 'accent': '#E63946',
        'bg': '#1a1410', 'card': '#2a2218', 'text': '#FAEDCD', 'muted': '#a89279',
        'font': "'Playfair Display', Georgia, serif",
        'hero_cta': 'View Our Menu', 'hero_cta2': 'Reserve a Table',
        'hero_icon': 'fa-utensils', 'hero_icon2': 'fa-calendar-check',
        'section_order': ['menu_highlights', 'about', 'testimonials', 'gallery', 'contact'],
        'unsplash': 'restaurant,fine-dining,food-plating',
    },
    'cafe': {
        'primary': '#C8A96E', 'secondary': '#FFF8F0', 'accent': '#6B4226',
        'bg': '#1c1816', 'card': '#2c2420', 'text': '#FFF8F0', 'muted': '#a09080',
        'font': "'Playfair Display', Georgia, serif",
        'hero_cta': 'See Our Menu', 'hero_cta2': 'Order Online',
        'hero_icon': 'fa-mug-hot', 'hero_icon2': 'fa-bag-shopping',
        'section_order': ['menu_highlights', 'about', 'testimonials', 'contact'],
        'unsplash': 'cafe,coffee-shop,latte-art',
    },
    'plumber': {
        'primary': '#2196F3', 'secondary': '#E3F2FD', 'accent': '#FF6F00',
        'bg': '#0c1929', 'card': '#142640', 'text': '#E3F2FD', 'muted': '#7da8cc',
        'font': "'Inter', 'Segoe UI', sans-serif",
        'hero_cta': 'Get a Free Quote', 'hero_cta2': 'Emergency Service',
        'hero_icon': 'fa-phone', 'hero_icon2': 'fa-bolt',
        'section_order': ['services', 'before_after', 'testimonials', 'service_area', 'contact'],
        'unsplash': 'plumbing,pipe-repair,water',
    },
    'electrician': {
        'primary': '#FFD600', 'secondary': '#FFF9C4', 'accent': '#FF6F00',
        'bg': '#141414', 'card': '#1e1e1e', 'text': '#FFF9C4', 'muted': '#b8a840',
        'font': "'Inter', 'Segoe UI', sans-serif",
        'hero_cta': 'Request a Quote', 'hero_cta2': '24/7 Emergency',
        'hero_icon': 'fa-phone', 'hero_icon2': 'fa-bolt',
        'section_order': ['services', 'about', 'testimonials', 'service_area', 'contact'],
        'unsplash': 'electrician,electrical-work,wiring',
    },
    'dentist': {
        'primary': '#26C6DA', 'secondary': '#E0F7FA', 'accent': '#00838F',
        'bg': '#0a1a1e', 'card': '#122a30', 'text': '#E0F7FA', 'muted': '#6aacb8',
        'font': "'DM Sans', 'Segoe UI', sans-serif",
        'hero_cta': 'Book Appointment', 'hero_cta2': 'Meet Our Team',
        'hero_icon': 'fa-calendar-check', 'hero_icon2': 'fa-user-doctor',
        'section_order': ['services', 'about', 'testimonials', 'team', 'contact'],
        'unsplash': 'dental-office,dentist,smile',
    },
    'salon': {
        'primary': '#E91E63', 'secondary': '#FCE4EC', 'accent': '#880E4F',
        'bg': '#1a0a12', 'card': '#2a1420', 'text': '#FCE4EC', 'muted': '#c07090',
        'font': "'Playfair Display', Georgia, serif",
        'hero_cta': 'Book Now', 'hero_cta2': 'View Gallery',
        'hero_icon': 'fa-calendar-check', 'hero_icon2': 'fa-images',
        'section_order': ['services', 'gallery', 'testimonials', 'about', 'contact'],
        'unsplash': 'hair-salon,beauty,hairstyle',
    },
    'gym': {
        'primary': '#FF5722', 'secondary': '#FBE9E7', 'accent': '#DD2C00',
        'bg': '#120c0a', 'card': '#201510', 'text': '#FBE9E7', 'muted': '#c08060',
        'font': "'Oswald', 'Impact', sans-serif",
        'hero_cta': 'Start Free Trial', 'hero_cta2': 'View Classes',
        'hero_icon': 'fa-dumbbell', 'hero_icon2': 'fa-calendar',
        'section_order': ['services', 'about', 'testimonials', 'pricing', 'contact'],
        'unsplash': 'gym,fitness,workout',
    },
    'lawyer': {
        'primary': '#37474F', 'secondary': '#ECEFF1', 'accent': '#B8860B',
        'bg': '#0e1114', 'card': '#1a1f24', 'text': '#ECEFF1', 'muted': '#78909C',
        'font': "'Libre Baskerville', Georgia, serif",
        'hero_cta': 'Free Consultation', 'hero_cta2': 'Our Practice Areas',
        'hero_icon': 'fa-phone', 'hero_icon2': 'fa-scale-balanced',
        'section_order': ['services', 'about', 'testimonials', 'team', 'contact'],
        'unsplash': 'law-office,legal,courthouse',
    },
    'realtor': {
        'primary': '#1B5E20', 'secondary': '#E8F5E9', 'accent': '#B8860B',
        'bg': '#0c1a0e', 'card': '#14281a', 'text': '#E8F5E9', 'muted': '#6a9a70',
        'font': "'DM Sans', 'Segoe UI', sans-serif",
        'hero_cta': 'View Listings', 'hero_cta2': 'Free Home Valuation',
        'hero_icon': 'fa-house', 'hero_icon2': 'fa-chart-line',
        'section_order': ['services', 'about', 'testimonials', 'service_area', 'contact'],
        'unsplash': 'real-estate,luxury-home,house',
    },
    'auto': {
        'primary': '#D32F2F', 'secondary': '#FFEBEE', 'accent': '#FF6F00',
        'bg': '#140c0c', 'card': '#201414', 'text': '#FFEBEE', 'muted': '#c07070',
        'font': "'Oswald', 'Impact', sans-serif",
        'hero_cta': 'Get an Estimate', 'hero_cta2': 'Our Services',
        'hero_icon': 'fa-phone', 'hero_icon2': 'fa-car',
        'section_order': ['services', 'about', 'testimonials', 'contact'],
        'unsplash': 'auto-repair,mechanic,garage',
    },
    'landscaping': {
        'primary': '#4CAF50', 'secondary': '#E8F5E9', 'accent': '#33691E',
        'bg': '#0c1a0e', 'card': '#14281a', 'text': '#E8F5E9', 'muted': '#6a9a70',
        'font': "'Inter', 'Segoe UI', sans-serif",
        'hero_cta': 'Free Estimate', 'hero_cta2': 'View Our Work',
        'hero_icon': 'fa-phone', 'hero_icon2': 'fa-images',
        'section_order': ['services', 'before_after', 'testimonials', 'service_area', 'contact'],
        'unsplash': 'landscaping,garden-design,lawn',
    },
    'cleaning': {
        'primary': '#00BCD4', 'secondary': '#E0F7FA', 'accent': '#006064',
        'bg': '#0a1a1e', 'card': '#122a30', 'text': '#E0F7FA', 'muted': '#6aacb8',
        'font': "'DM Sans', 'Segoe UI', sans-serif",
        'hero_cta': 'Book a Cleaning', 'hero_cta2': 'Get a Quote',
        'hero_icon': 'fa-calendar-check', 'hero_icon2': 'fa-phone',
        'section_order': ['services', 'about', 'before_after', 'testimonials', 'contact'],
        'unsplash': 'cleaning-service,clean-home,housekeeping',
    },
    'construction': {
        'primary': '#FF8F00', 'secondary': '#FFF3E0', 'accent': '#E65100',
        'bg': '#1a1408', 'card': '#2a2010', 'text': '#FFF3E0', 'muted': '#b89050',
        'font': "'Oswald', 'Impact', sans-serif",
        'hero_cta': 'Request a Bid', 'hero_cta2': 'View Projects',
        'hero_icon': 'fa-phone', 'hero_icon2': 'fa-images',
        'section_order': ['services', 'before_after', 'about', 'testimonials', 'contact'],
        'unsplash': 'construction,building,architecture',
    },
    'spa': {
        'primary': '#9C27B0', 'secondary': '#F3E5F5', 'accent': '#4A148C',
        'bg': '#160a1a', 'card': '#24142a', 'text': '#F3E5F5', 'muted': '#a070b0',
        'font': "'Playfair Display', Georgia, serif",
        'hero_cta': 'Book Treatment', 'hero_cta2': 'View Packages',
        'hero_icon': 'fa-calendar-check', 'hero_icon2': 'fa-spa',
        'section_order': ['services', 'gallery', 'testimonials', 'about', 'contact'],
        'unsplash': 'spa,wellness,massage',
    },
    'default': {
        'primary': '#6366F1', 'secondary': '#EEF2FF', 'accent': '#4338CA',
        'bg': '#0e0e1a', 'card': '#1a1a2e', 'text': '#EEF2FF', 'muted': '#8888bb',
        'font': "'Inter', 'Segoe UI', sans-serif",
        'hero_cta': 'Get Started', 'hero_cta2': 'Learn More',
        'hero_icon': 'fa-phone', 'hero_icon2': 'fa-arrow-down',
        'section_order': ['services', 'about', 'testimonials', 'contact'],
        'unsplash': 'business,office,professional',
    },
}

def get_industry_theme(category):
    """Match a business category to the best industry theme."""
    cat = category.lower()
    # Direct match
    if cat in INDUSTRY_THEMES:
        return INDUSTRY_THEMES[cat]
    # Fuzzy match
    keyword_map = {
        'restaurant': ['restaurant', 'dining', 'food', 'eatery', 'grill', 'bistro', 'diner', 'sushi', 'pizza', 'bbq', 'steakhouse', 'thai', 'chinese', 'italian', 'mexican', 'indian', 'japanese', 'korean', 'vietnamese', 'mediterranean', 'seafood', 'wings', 'burger', 'taco', 'noodle', 'ramen', 'pho', 'deli', 'sandwich'],
        'cafe': ['cafe', 'coffee', 'bakery', 'tea', 'pastry', 'donut', 'dessert', 'ice cream', 'juice', 'smoothie'],
        'plumber': ['plumber', 'plumbing', 'drain', 'pipe', 'water heater', 'sewer'],
        'electrician': ['electrician', 'electrical', 'wiring', 'lighting'],
        'dentist': ['dentist', 'dental', 'orthodont', 'oral', 'teeth', 'tooth'],
        'salon': ['salon', 'hair', 'barber', 'beauty', 'nails', 'nail', 'lash', 'brow', 'wax', 'makeup'],
        'gym': ['gym', 'fitness', 'crossfit', 'yoga', 'pilates', 'martial art', 'boxing', 'training', 'personal trainer'],
        'lawyer': ['lawyer', 'attorney', 'law firm', 'legal', 'notary'],
        'realtor': ['realtor', 'real estate', 'realty', 'property', 'mortgage', 'home inspector'],
        'auto': ['auto', 'mechanic', 'car', 'tire', 'oil change', 'body shop', 'collision', 'transmission', 'brake'],
        'landscaping': ['landscap', 'lawn', 'garden', 'tree', 'mowing', 'irrigation', 'hardscape', 'snow removal'],
        'cleaning': ['clean', 'maid', 'janitorial', 'pressure wash', 'carpet clean', 'window clean'],
        'construction': ['construct', 'contractor', 'building', 'roofing', 'roof', 'siding', 'renovation', 'remodel', 'hvac', 'painting', 'paint', 'drywall', 'flooring', 'deck', 'fence', 'paving', 'concrete', 'mason', 'weld'],
        'spa': ['spa', 'wellness', 'massage', 'facial', 'skin', 'derma', 'acupuncture', 'chiropractic', 'therapy'],
        'dentist': ['doctor', 'medical', 'clinic', 'physician', 'pediatr', 'optometr', 'optic', 'pharmacy', 'physio', 'veterinar', 'vet'],
    }
    for theme_key, keywords in keyword_map.items():
        for kw in keywords:
            if kw in cat:
                return INDUSTRY_THEMES[theme_key]
    return INDUSTRY_THEMES['default']


@app.route('/api/mockup', methods=['POST'])
def api_mockup():
    """
    POST /api/mockup
    Body: { name, city, category, phone, address, website, rating, review_count, description }
    Returns: { html: '...complete personalized HTML mockup...' }
    """
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

    # Get industry theme
    theme = get_industry_theme(category)
    primary = theme['primary']
    secondary = theme['secondary']
    accent = theme['accent']
    bg_color = theme['bg']
    card_color = theme['card']
    text_color = theme['text']
    muted_color = theme['muted']
    font_family = theme['font']
    hero_cta = theme['hero_cta']
    hero_cta2 = theme['hero_cta2']
    hero_icon = theme['hero_icon']
    hero_icon2 = theme['hero_icon2']
    unsplash_kw = theme['unsplash']

    hero_image_url = f"https://source.unsplash.com/1600x900/?{urllib.parse.quote(unsplash_kw)}"

    # Use real stats if we have them, otherwise show nothing fake
    if rating:
        try:
            rating_val = float(rating)
            rating_display = f"{rating_val:.1f}"
            stars_full = int(rating_val)
            stars_half = 1 if (rating_val - stars_full) >= 0.3 else 0
            stars_html = ''.join(['<i class="fa fa-star"></i>'] * stars_full)
            if stars_half:
                stars_html += '<i class="fa fa-star-half-stroke"></i>'
        except (ValueError, TypeError):
            rating_display = ''
            stars_html = ''
    else:
        rating_display = ''
        stars_html = ''

    review_count_display = str(review_count) if review_count else ''

    # Single Gemini call for ALL personalized content
    client = get_gemini_client()
    gemini_content = None
    try:
        mega_prompt = f"""Generate personalized website content for this specific business. Make it feel CUSTOM and REAL, not generic.

BUSINESS:
- Name: {name}
- City: {city}
- Category: {category}
- Phone: {phone or 'N/A'}
- Address: {address or 'N/A'}
- Rating: {rating or 'N/A'}
- Reviews: {review_count or 'N/A'}
- Description: {description or 'N/A'}

Return ONLY valid JSON (no markdown, no code blocks):
{{
  "tagline": "A punchy 4-8 word tagline specific to this exact business and location",
  "hero_subtitle": "One compelling sentence (max 15 words) about what makes THIS business special in THIS city",
  "about_paragraph": "2-3 sentences (max 60 words) about this specific business. Reference the city, their specialty. Make it feel like THEY wrote it.",
  "services": [
    {{"name": "Specific Service 1", "desc": "One sentence explaining this service (12-18 words). Be specific to the industry."}},
    {{"name": "Specific Service 2", "desc": "One sentence explaining this service (12-18 words). Be specific to the industry."}},
    {{"name": "Specific Service 3", "desc": "One sentence explaining this service (12-18 words). Be specific to the industry."}},
    {{"name": "Specific Service 4", "desc": "One sentence explaining this service (12-18 words). Be specific to the industry."}},
    {{"name": "Specific Service 5", "desc": "One sentence explaining this service (12-18 words). Be specific to the industry."}},
    {{"name": "Specific Service 6", "desc": "One sentence explaining this service (12-18 words). Be specific to the industry."}}
  ],
  "reviews": [
    {{"text": "A realistic Google-style review (20-35 words) mentioning a specific service or experience at this business.", "name": "FirstName L.", "rating": 5}},
    {{"text": "A realistic review (20-35 words) with slight personality, mentioning something specific about visiting this place.", "name": "FirstName L.", "rating": 5}},
    {{"text": "A realistic review (20-35 words), slightly different tone, mentions the city or neighborhood.", "name": "FirstName L.", "rating": 4}}
  ],
  "unique_selling_points": [
    "A specific differentiator for THIS business (e.g., 'Same-day emergency service')",
    "Another unique advantage (e.g., 'Family-owned since 2005')",
    "A third selling point (e.g., 'Free estimates within 2 hours')",
    "A fourth selling point (e.g., 'Serving all of {city} metro area')"
  ]
}}

RULES:
- NO generic phrases like "top-quality service" or "experienced team" or "committed to excellence"
- Every piece of text must feel like it was written FOR this specific business
- Reviews must sound like real people, not marketing copy
- Services must be actual services this type of business offers
- If you know specifics about this city/area, reference them"""

        response = client.generate_content(mega_prompt)
        raw = response.text.strip()
        # Clean markdown code blocks if present
        if '```' in raw:
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
            raw = raw.strip()
        gemini_content = json.loads(raw)
    except Exception as e:
        gemini_content = None

    # Fallback content if Gemini fails
    if not gemini_content:
        gemini_content = {
            'tagline': f"{city}'s Go-To {category.title()}",
            'hero_subtitle': f"Serving {city} with dedication and expertise in {category}.",
            'about_paragraph': f"{name} is a locally-owned {category} business right here in {city}. We take pride in knowing our neighbors and delivering results that speak for themselves.",
            'services': [
                {'name': f'{category.title()} Service {i+1}', 'desc': f'Professional {category} solutions tailored to your specific needs.'} for i in range(6)
            ],
            'reviews': [
                {'text': f'Great experience with {name}. They really know what they are doing and the pricing was fair.', 'name': 'Sarah M.', 'rating': 5},
                {'text': f'Used {name} twice now. Reliable, professional, and they actually show up when they say they will.', 'name': 'James T.', 'rating': 5},
                {'text': f'Found {name} on Google and glad I did. Good work at a reasonable price in {city}.', 'name': 'Amanda R.', 'rating': 4},
            ],
            'unique_selling_points': [f'Locally owned in {city}', 'Licensed and insured', 'Free consultations', 'Satisfaction guaranteed']
        }

    tagline = gemini_content.get('tagline', f"{city}'s Best {category.title()}")
    hero_subtitle = gemini_content.get('hero_subtitle', '')
    about_text = gemini_content.get('about_paragraph', '')
    services = gemini_content.get('services', [])[:6]
    reviews = gemini_content.get('reviews', [])[:3]
    usps = gemini_content.get('unique_selling_points', [])[:4]

    # Service icons per industry
    service_icons = {
        'restaurant': ['fa-utensils', 'fa-wine-glass', 'fa-truck', 'fa-cake-candles', 'fa-champagne-glasses', 'fa-fire-burner'],
        'cafe': ['fa-mug-hot', 'fa-cookie', 'fa-blender', 'fa-ice-cream', 'fa-wifi', 'fa-bag-shopping'],
        'plumber': ['fa-wrench', 'fa-faucet-drip', 'fa-hot-tub-person', 'fa-house-flood-water', 'fa-shower', 'fa-toolbox'],
        'electrician': ['fa-bolt', 'fa-lightbulb', 'fa-plug', 'fa-solar-panel', 'fa-fan', 'fa-toolbox'],
        'dentist': ['fa-tooth', 'fa-teeth', 'fa-syringe', 'fa-x-ray', 'fa-face-smile', 'fa-shield-halved'],
        'salon': ['fa-scissors', 'fa-spray-can-sparkles', 'fa-paintbrush', 'fa-face-smile-beam', 'fa-hand-sparkles', 'fa-wand-magic-sparkles'],
        'gym': ['fa-dumbbell', 'fa-person-running', 'fa-heart-pulse', 'fa-stopwatch', 'fa-users', 'fa-ranking-star'],
        'lawyer': ['fa-scale-balanced', 'fa-gavel', 'fa-file-contract', 'fa-handshake', 'fa-building-columns', 'fa-shield-halved'],
        'realtor': ['fa-house', 'fa-key', 'fa-magnifying-glass-dollar', 'fa-handshake', 'fa-chart-line', 'fa-building'],
        'auto': ['fa-car', 'fa-oil-can', 'fa-gears', 'fa-tire', 'fa-battery-full', 'fa-gauge-high'],
        'landscaping': ['fa-leaf', 'fa-tree', 'fa-seedling', 'fa-sun', 'fa-water', 'fa-trowel'],
        'cleaning': ['fa-broom', 'fa-spray-can-sparkles', 'fa-hand-sparkles', 'fa-house-chimney', 'fa-pump-soap', 'fa-window-maximize'],
        'construction': ['fa-hammer', 'fa-hard-hat', 'fa-ruler-combined', 'fa-truck', 'fa-paint-roller', 'fa-screwdriver-wrench'],
        'spa': ['fa-spa', 'fa-hand-holding-heart', 'fa-hot-tub-person', 'fa-gem', 'fa-feather', 'fa-yin-yang'],
    }
    cat_key = None
    for k in service_icons:
        if k in category.lower():
            cat_key = k
            break
    icons = service_icons.get(cat_key, ['fa-check-circle', 'fa-star', 'fa-thumbs-up', 'fa-bolt', 'fa-shield-halved', 'fa-clock'])

    # Build services HTML
    services_html = ''
    for i, svc in enumerate(services):
        icon = icons[i % len(icons)]
        svc_name = svc.get('name', f'Service {i+1}') if isinstance(svc, dict) else str(svc)
        svc_desc = svc.get('desc', '') if isinstance(svc, dict) else ''
        services_html += f'''
      <div class="service-card">
        <div class="service-icon"><i class="fa {icon}"></i></div>
        <div class="service-name">{svc_name}</div>
        <div class="service-desc">{svc_desc}</div>
      </div>'''

    # Build reviews HTML with real star ratings
    reviews_html = ''
    for rev in reviews:
        rev_rating = rev.get('rating', 5) if isinstance(rev, dict) else 5
        rev_text = rev.get('text', '') if isinstance(rev, dict) else str(rev)
        rev_name = rev.get('name', 'Customer') if isinstance(rev, dict) else 'Customer'
        rev_stars = '&#9733;' * int(rev_rating) + ('&#9734;' * (5 - int(rev_rating)))
        initial = rev_name[0].upper() if rev_name else 'C'
        reviews_html += f'''
      <div class="testimonial-card">
        <div class="testimonial-stars">{rev_stars}</div>
        <p class="testimonial-text">"{rev_text}"</p>
        <div class="testimonial-author">
          <div class="testimonial-avatar">{initial}</div>
          <div><div class="testimonial-name">{rev_name}</div><div class="testimonial-location">{city}</div></div>
        </div>
      </div>'''

    # Build USP badges
    usp_html = ''
    usp_icons = ['fa-circle-check', 'fa-shield-halved', 'fa-clock', 'fa-location-dot']
    for i, usp in enumerate(usps):
        usp_icon = usp_icons[i % len(usp_icons)]
        usp_html += f'<div class="about-feature"><i class="fa {usp_icon}"></i> {usp}</div>'

    # Stats section — only show real data
    stats_html = ''
    if rating_display or review_count_display:
        stats_items = ''
        if rating_display:
            stats_items += f'<div class="stat"><div class="stat-num">{rating_display} {stars_html}</div><div class="stat-label">Google Rating</div></div>'
        if review_count_display:
            stats_items += f'<div class="stat"><div class="stat-num">{review_count_display}+</div><div class="stat-label">Google Reviews</div></div>'
        stats_html = f'<div class="hero-stats">{stats_items}</div>'

    # Google Maps embed
    maps_html = ''
    if address:
        maps_query = urllib.parse.quote(f"{name} {address} {city}")
        maps_html = f'''
    <div class="map-container">
      <iframe src="https://www.google.com/maps?q={maps_query}&output=embed" width="100%" height="300" style="border:0;border-radius:12px;" allowfullscreen="" loading="lazy"></iframe>
    </div>'''

    # Google Fonts based on theme
    font_imports = ''
    if 'Playfair' in font_family:
        font_imports = '<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700;900&display=swap" rel="stylesheet">'
    elif 'Oswald' in font_family:
        font_imports = '<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@400;600;700&display=swap" rel="stylesheet">'
    elif 'Libre Baskerville' in font_family:
        font_imports = '<link href="https://fonts.googleapis.com/css2?family=Libre+Baskerville:wght@400;700&display=swap" rel="stylesheet">'
    elif 'DM Sans' in font_family:
        font_imports = '<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">'
    else:
        font_imports = '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;700;900&display=swap" rel="stylesheet">'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name} - {category.title()} in {city}</title>
{font_imports}
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  :root {{
    --primary: {primary};
    --secondary: {secondary};
    --accent: {accent};
    --bg: {bg_color};
    --card: {card_color};
    --text: {text_color};
    --muted: {muted_color};
  }}
  body {{ font-family: {font_family}; background: var(--bg); color: var(--text); }}

  /* NAV */
  nav {{ position: fixed; top: 0; left: 0; right: 0; z-index: 100; background: rgba({int(bg_color[1:3],16)},{int(bg_color[3:5],16)},{int(bg_color[5:7],16)},0.95); backdrop-filter: blur(12px); border-bottom: 1px solid rgba(255,255,255,0.06); padding: 0 40px; display: flex; align-items: center; justify-content: space-between; height: 68px; }}
  .nav-logo {{ font-size: 22px; font-weight: 900; color: var(--primary); letter-spacing: -0.5px; }}
  .nav-links {{ display: flex; gap: 32px; }}
  .nav-links a {{ color: var(--muted); text-decoration: none; font-size: 14px; font-weight: 500; transition: color 0.2s; }}
  .nav-links a:hover {{ color: var(--primary); }}
  .nav-cta {{ background: var(--primary); color: var(--bg); padding: 10px 24px; border-radius: 8px; font-size: 14px; font-weight: 700; text-decoration: none; transition: all 0.2s; }}
  .nav-cta:hover {{ opacity: 0.9; transform: translateY(-1px); }}

  /* HERO */
  .hero {{ position: relative; height: 100vh; min-height: 600px; display: flex; align-items: center; justify-content: center; text-align: center; overflow: hidden; }}
  .hero-bg {{ position: absolute; inset: 0; background-image: url('{hero_image_url}'); background-size: cover; background-position: center; filter: brightness(0.2) saturate(0.8); }}
  .hero-overlay {{ position: absolute; inset: 0; background: linear-gradient(180deg, rgba({int(bg_color[1:3],16)},{int(bg_color[3:5],16)},{int(bg_color[5:7],16)},0.3) 0%, rgba({int(bg_color[1:3],16)},{int(bg_color[3:5],16)},{int(bg_color[5:7],16)},0.85) 100%); }}
  .hero-content {{ position: relative; z-index: 1; max-width: 800px; padding: 0 24px; }}
  .hero-badge {{ display: inline-block; background: rgba({int(primary[1:3],16)},{int(primary[3:5],16)},{int(primary[5:7],16)},0.15); border: 1px solid rgba({int(primary[1:3],16)},{int(primary[3:5],16)},{int(primary[5:7],16)},0.3); color: var(--primary); font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 2px; padding: 6px 16px; border-radius: 100px; margin-bottom: 24px; }}
  .hero h1 {{ font-size: clamp(36px, 6vw, 68px); font-weight: 900; line-height: 1.08; letter-spacing: -2px; margin-bottom: 16px; color: #fff; }}
  .hero h1 span {{ color: var(--primary); display: block; font-size: 0.55em; letter-spacing: 0; margin-top: 8px; }}
  .hero-tagline {{ font-size: clamp(16px, 2vw, 20px); color: rgba(255,255,255,0.7); margin-bottom: 40px; line-height: 1.6; max-width: 600px; margin-left: auto; margin-right: auto; }}
  .hero-actions {{ display: flex; gap: 16px; justify-content: center; flex-wrap: wrap; }}
  .btn-hero {{ padding: 14px 32px; border-radius: 8px; font-size: 16px; font-weight: 700; text-decoration: none; transition: all 0.25s; display: inline-flex; align-items: center; gap: 8px; }}
  .btn-hero-primary {{ background: var(--primary); color: var(--bg); }}
  .btn-hero-primary:hover {{ transform: translateY(-2px); box-shadow: 0 8px 32px rgba({int(primary[1:3],16)},{int(primary[3:5],16)},{int(primary[5:7],16)},0.35); }}
  .btn-hero-secondary {{ background: rgba(255,255,255,0.08); color: #fff; border: 1px solid rgba(255,255,255,0.15); backdrop-filter: blur(4px); }}
  .btn-hero-secondary:hover {{ background: rgba(255,255,255,0.12); }}
  .hero-stats {{ display: flex; gap: 48px; justify-content: center; margin-top: 50px; flex-wrap: wrap; }}
  .stat {{ text-align: center; }}
  .stat-num {{ font-size: 28px; font-weight: 900; color: var(--primary); }}
  .stat-num i {{ font-size: 18px; margin-left: 2px; }}
  .stat-label {{ font-size: 12px; color: var(--muted); margin-top: 4px; text-transform: uppercase; letter-spacing: 1px; }}

  /* SECTION BASE */
  section {{ padding: 100px 40px; }}
  .section-label {{ font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 3px; color: var(--primary); margin-bottom: 12px; }}
  .section-title {{ font-size: clamp(28px, 4vw, 44px); font-weight: 900; letter-spacing: -1px; margin-bottom: 16px; color: #fff; }}
  .section-sub {{ font-size: 16px; color: var(--muted); max-width: 560px; line-height: 1.6; }}
  .section-header {{ margin-bottom: 64px; }}

  /* SERVICES */
  .services {{ background: var(--bg); }}
  .services-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }}
  .service-card {{ background: var(--card); border: 1px solid rgba(255,255,255,0.06); border-radius: 16px; padding: 32px; transition: all 0.3s; position: relative; overflow: hidden; }}
  .service-card::before {{ content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; background: linear-gradient(90deg, var(--primary), transparent); opacity: 0; transition: opacity 0.3s; }}
  .service-card:hover {{ border-color: rgba({int(primary[1:3],16)},{int(primary[3:5],16)},{int(primary[5:7],16)},0.4); transform: translateY(-4px); box-shadow: 0 20px 40px rgba(0,0,0,0.2); }}
  .service-card:hover::before {{ opacity: 1; }}
  .service-icon {{ width: 48px; height: 48px; background: rgba({int(primary[1:3],16)},{int(primary[3:5],16)},{int(primary[5:7],16)},0.12); border-radius: 12px; display: flex; align-items: center; justify-content: center; margin-bottom: 20px; color: var(--primary); font-size: 20px; }}
  .service-name {{ font-size: 17px; font-weight: 700; margin-bottom: 8px; color: #fff; }}
  .service-desc {{ font-size: 14px; color: var(--muted); line-height: 1.6; }}

  /* ABOUT */
  .about {{ background: rgba(255,255,255,0.02); }}
  .about-inner {{ display: grid; grid-template-columns: 1fr 1fr; gap: 60px; max-width: 1100px; margin: 0 auto; align-items: center; }}
  .about-image {{ position: relative; border-radius: 16px; overflow: hidden; }}
  .about-image img {{ width: 100%; height: 400px; object-fit: cover; border-radius: 16px; }}
  .about-image-badge {{ position: absolute; bottom: 16px; left: 16px; background: rgba(0,0,0,0.8); backdrop-filter: blur(8px); color: var(--primary); padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; }}
  .about-text .section-sub {{ margin-bottom: 32px; }}
  .about-features {{ display: flex; flex-direction: column; gap: 12px; }}
  .about-feature {{ display: flex; align-items: center; gap: 12px; font-size: 15px; color: var(--text); }}
  .about-feature i {{ color: var(--primary); font-size: 16px; }}

  /* TESTIMONIALS */
  .testimonials {{ background: var(--bg); }}
  .testimonials-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; max-width: 1100px; margin: 0 auto; }}
  .testimonial-card {{ background: var(--card); border: 1px solid rgba(255,255,255,0.06); border-radius: 16px; padding: 32px; }}
  .testimonial-stars {{ color: var(--primary); font-size: 16px; margin-bottom: 16px; letter-spacing: 2px; }}
  .testimonial-text {{ font-size: 15px; line-height: 1.7; color: var(--text); margin-bottom: 24px; font-style: italic; }}
  .testimonial-author {{ display: flex; align-items: center; gap: 12px; }}
  .testimonial-avatar {{ width: 40px; height: 40px; border-radius: 50%; background: rgba({int(primary[1:3],16)},{int(primary[3:5],16)},{int(primary[5:7],16)},0.2); color: var(--primary); display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 16px; }}
  .testimonial-name {{ font-weight: 600; font-size: 14px; color: #fff; }}
  .testimonial-location {{ font-size: 12px; color: var(--muted); }}

  /* CTA BAND */
  .cta-band {{ background: linear-gradient(135deg, var(--primary), {accent}); padding: 80px 40px; text-align: center; }}
  .cta-band h2 {{ font-size: clamp(24px, 4vw, 40px); font-weight: 900; color: #fff; margin-bottom: 12px; }}
  .cta-band p {{ font-size: 16px; color: rgba(255,255,255,0.85); margin-bottom: 32px; }}
  .btn-cta-dark {{ display: inline-flex; align-items: center; gap: 8px; background: var(--bg); color: var(--primary); padding: 14px 32px; border-radius: 8px; font-size: 16px; font-weight: 700; text-decoration: none; transition: all 0.25s; }}
  .btn-cta-dark:hover {{ transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,0,0,0.3); }}

  /* CONTACT */
  .contact {{ background: rgba(255,255,255,0.02); }}
  .contact-inner {{ display: grid; grid-template-columns: 1fr 1fr; gap: 40px; }}
  .contact-info {{ display: flex; flex-direction: column; gap: 24px; }}
  .contact-item {{ display: flex; gap: 16px; align-items: flex-start; }}
  .contact-icon {{ width: 44px; height: 44px; border-radius: 10px; background: rgba({int(primary[1:3],16)},{int(primary[3:5],16)},{int(primary[5:7],16)},0.12); color: var(--primary); display: flex; align-items: center; justify-content: center; font-size: 18px; flex-shrink: 0; }}
  .contact-label {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px; }}
  .contact-value {{ font-size: 15px; color: var(--text); line-height: 1.5; }}
  .contact-form {{ background: var(--card); border: 1px solid rgba(255,255,255,0.06); border-radius: 16px; padding: 32px; }}
  .form-group {{ margin-bottom: 20px; }}
  .form-group label {{ display: block; font-size: 13px; font-weight: 600; color: var(--muted); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px; }}
  .form-group input, .form-group textarea {{ width: 100%; padding: 12px 16px; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; background: var(--bg); color: var(--text); font-size: 15px; font-family: inherit; transition: border-color 0.2s; }}
  .form-group input:focus, .form-group textarea:focus {{ outline: none; border-color: var(--primary); }}
  .form-group textarea {{ height: 120px; resize: vertical; }}
  .form-submit {{ width: 100%; padding: 14px; background: var(--primary); color: var(--bg); border: none; border-radius: 8px; font-size: 16px; font-weight: 700; cursor: pointer; transition: all 0.2s; display: flex; align-items: center; justify-content: center; gap: 8px; font-family: inherit; }}
  .form-submit:hover {{ opacity: 0.9; transform: translateY(-1px); }}
  .map-container {{ margin-top: 24px; border-radius: 12px; overflow: hidden; border: 1px solid rgba(255,255,255,0.06); }}

  /* FOOTER */
  footer {{ background: rgba(0,0,0,0.3); padding: 60px 40px 40px; text-align: center; border-top: 1px solid rgba(255,255,255,0.06); }}
  .footer-logo {{ font-size: 24px; font-weight: 900; color: var(--primary); margin-bottom: 8px; }}
  .footer-tagline {{ font-size: 14px; color: var(--muted); margin-bottom: 24px; }}
  .footer-links {{ display: flex; gap: 24px; justify-content: center; margin-bottom: 32px; }}
  .footer-links a {{ color: var(--muted); text-decoration: none; font-size: 14px; transition: color 0.2s; }}
  .footer-links a:hover {{ color: var(--primary); }}
  .footer-copy {{ font-size: 13px; color: rgba(255,255,255,0.3); }}

  /* WATERMARK */
  .watermark-bar {{ position: fixed; bottom: 0; left: 0; right: 0; z-index: 999; background: linear-gradient(135deg, var(--primary), {accent}); padding: 10px; text-align: center; }}
  .watermark-bar p {{ font-size: 13px; font-weight: 600; color: #fff; }}
  .watermark-bar span {{ font-weight: 800; }}

  /* RESPONSIVE */
  @media (max-width: 768px) {{
    nav {{ padding: 0 20px; }}
    .nav-links {{ display: none; }}
    section {{ padding: 60px 20px; }}
    .about-inner {{ grid-template-columns: 1fr; gap: 32px; }}
    .contact-inner {{ grid-template-columns: 1fr; }}
    .hero-stats {{ gap: 24px; }}
  }}
</style>
</head>
<body>

<!-- NAV -->
<nav>
  <div class="nav-logo">{name}</div>
  <div class="nav-links">
    <a href="#services">Services</a>
    <a href="#about">About</a>
    <a href="#testimonials">Reviews</a>
    <a href="#contact">Contact</a>
  </div>
  <a href="#contact" class="nav-cta">{hero_cta}</a>
</nav>

<!-- HERO -->
<section class="hero">
  <div class="hero-bg"></div>
  <div class="hero-overlay"></div>
  <div class="hero-content">
    <div class="hero-badge">{city} &bull; {category.title()}</div>
    <h1>{name}<span>{tagline}</span></h1>
    <p class="hero-tagline">{hero_subtitle}</p>
    <div class="hero-actions">
      <a href="#contact" class="btn-hero btn-hero-primary"><i class="fa {hero_icon}"></i> {hero_cta}</a>
      <a href="#services" class="btn-hero btn-hero-secondary"><i class="fa {hero_icon2}"></i> {hero_cta2}</a>
    </div>
    {stats_html}
  </div>
</section>

<!-- SERVICES -->
<section class="services" id="services">
  <div style="max-width:1100px;margin:0 auto;">
    <div class="section-header">
      <div class="section-label">What We Do</div>
      <div class="section-title">Our Services</div>
      <div class="section-sub">Specialized {category} solutions for {city} and surrounding areas.</div>
    </div>
    <div class="services-grid">
      {services_html}
    </div>
  </div>
</section>

<!-- ABOUT -->
<section class="about" id="about">
  <div class="about-inner">
    <div class="about-image">
      <img src="https://source.unsplash.com/800x600/?{urllib.parse.quote(unsplash_kw)},team" alt="{name} team">
      <div class="about-image-badge"><i class="fa fa-shield-halved"></i>&nbsp; Licensed &amp; Insured</div>
    </div>
    <div class="about-text">
      <div class="section-label">About Us</div>
      <div class="section-title">Why Choose {name}?</div>
      <p class="section-sub">{about_text}</p>
      <div class="about-features">
        {usp_html}
      </div>
    </div>
  </div>
</section>

<!-- TESTIMONIALS -->
<section class="testimonials" id="testimonials">
  <div style="max-width:1100px;margin:0 auto;">
    <div class="section-header">
      <div class="section-label">Reviews</div>
      <div class="section-title">What Our Clients Say</div>
      <div class="section-sub">{'Based on ' + review_count_display + ' Google reviews' if review_count_display else 'Real feedback from our customers'}.</div>
    </div>
    <div class="testimonials-grid">
      {reviews_html}
    </div>
  </div>
</section>

<!-- CTA BAND -->
<div class="cta-band">
  <h2>Ready to Get Started?</h2>
  <p>{'Call us at ' + phone + ' or fill out the form below.' if phone else 'Fill out the form below and we will get back to you fast.'}</p>
  <a href="#contact" class="btn-cta-dark"><i class="fa fa-calendar-check"></i> {hero_cta}</a>
</div>

<!-- CONTACT -->
<section class="contact" id="contact">
  <div style="max-width:1000px;margin:0 auto;">
    <div class="section-header" style="text-align:center;">
      <div class="section-label">Get In Touch</div>
      <div class="section-title">Contact {name}</div>
    </div>
    <div class="contact-inner">
      <div class="contact-info">
        {'<div class="contact-item"><div class="contact-icon"><i class="fa fa-phone"></i></div><div><div class="contact-label">Phone</div><div class="contact-value">' + phone + '</div></div></div>' if phone else ''}
        {'<div class="contact-item"><div class="contact-icon"><i class="fa fa-location-dot"></i></div><div><div class="contact-label">Address</div><div class="contact-value">' + address + '</div></div></div>' if address else ''}
        <div class="contact-item"><div class="contact-icon"><i class="fa fa-clock"></i></div><div><div class="contact-label">Hours</div><div class="contact-value">Mon-Fri: 8am - 6pm<br>Sat: 9am - 4pm</div></div></div>
        <div class="contact-item"><div class="contact-icon"><i class="fa fa-map-marker-alt"></i></div><div><div class="contact-label">Service Area</div><div class="contact-value">{city} &amp; surrounding areas</div></div></div>
        {maps_html}
      </div>
      <div class="contact-form">
        <div class="form-group"><label>Your Name</label><input type="text" placeholder="John Smith"></div>
        <div class="form-group"><label>Email</label><input type="email" placeholder="john@email.com"></div>
        <div class="form-group"><label>Phone</label><input type="tel" placeholder="(555) 000-0000"></div>
        <div class="form-group"><label>Message</label><textarea placeholder="Tell us about your project..."></textarea></div>
        <button class="form-submit">{hero_cta} <i class="fa fa-arrow-right"></i></button>
      </div>
    </div>
  </div>
</section>

<!-- FOOTER -->
<footer>
  <div class="footer-logo">{name}</div>
  <div class="footer-tagline">{city}'s trusted {category} specialists</div>
  <div class="footer-links">
    <a href="#services">Services</a>
    <a href="#about">About</a>
    <a href="#testimonials">Reviews</a>
    <a href="#contact">Contact</a>
  </div>
  <div class="footer-copy">&copy; 2025 {name}. All rights reserved.</div>
</footer>

<div class="watermark-bar">
  <p>This is a preview mockup by <span>Cold Outreach Engine</span> &mdash; Imagine what your real website could look like.</p>
</div>

</body>
</html>"""

    return jsonify({'html': html})


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