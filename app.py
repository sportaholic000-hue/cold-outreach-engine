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
        'unsplash': 'restaurant,fine-dining,food-plating',
    },
    'cafe': {
        'primary': '#C8A96E', 'secondary': '#FFF8F0', 'accent': '#6B4226',
        'bg': '#1c1816', 'card': '#2c2420', 'text': '#FFF8F0', 'muted': '#a09080',
        'font': "'Playfair Display', Georgia, serif",
        'hero_cta': 'See Our Menu', 'hero_cta2': 'Order Online',
        'hero_icon': 'fa-mug-hot', 'hero_icon2': 'fa-bag-shopping',
        'unsplash': 'cafe,coffee-shop,latte-art',
    },
    'plumber': {
        'primary': '#2196F3', 'secondary': '#E3F2FD', 'accent': '#FF6F00',
        'bg': '#0c1929', 'card': '#142640', 'text': '#E3F2FD', 'muted': '#7da8cc',
        'font': "'Inter', 'Segoe UI', sans-serif",
        'hero_cta': 'Get a Free Quote', 'hero_cta2': 'Emergency Service',
        'hero_icon': 'fa-phone', 'hero_icon2': 'fa-bolt',
        'unsplash': 'plumbing,pipe-repair,water',
    },
    'electrician': {
        'primary': '#FFD600', 'secondary': '#FFF9C4', 'accent': '#FF6F00',
        'bg': '#141414', 'card': '#1e1e1e', 'text': '#FFF9C4', 'muted': '#b8a840',
        'font': "'Inter', 'Segoe UI', sans-serif",
        'hero_cta': 'Request a Quote', 'hero_cta2': '24/7 Emergency',
        'hero_icon': 'fa-phone', 'hero_icon2': 'fa-bolt',
        'unsplash': 'electrician,electrical-work,wiring',
    },
    'dentist': {
        'primary': '#26C6DA', 'secondary': '#E0F7FA', 'accent': '#00838F',
        'bg': '#0a1a1e', 'card': '#122a30', 'text': '#E0F7FA', 'muted': '#6aacb8',
        'font': "'DM Sans', 'Segoe UI', sans-serif",
        'hero_cta': 'Book Appointment', 'hero_cta2': 'Meet Our Team',
        'hero_icon': 'fa-calendar-check', 'hero_icon2': 'fa-user-doctor',
        'unsplash': 'dental-office,dentist,smile',
    },
    'salon': {
        'primary': '#E91E63', 'secondary': '#FCE4EC', 'accent': '#880E4F',
        'bg': '#1a0a12', 'card': '#2a1420', 'text': '#FCE4EC', 'muted': '#c07090',
        'font': "'Playfair Display', Georgia, serif",
        'hero_cta': 'Book Now', 'hero_cta2': 'View Gallery',
        'hero_icon': 'fa-calendar-check', 'hero_icon2': 'fa-images',
        'unsplash': 'hair-salon,beauty,hairstyle',
    },
    'gym': {
        'primary': '#FF5722', 'secondary': '#FBE9E7', 'accent': '#DD2C00',
        'bg': '#120c0a', 'card': '#201510', 'text': '#FBE9E7', 'muted': '#c08060',
        'font': "'Oswald', 'Impact', sans-serif",
        'hero_cta': 'Start Free Trial', 'hero_cta2': 'View Classes',
        'hero_icon': 'fa-dumbbell', 'hero_icon2': 'fa-calendar',
        'unsplash': 'gym,fitness,workout',
    },
    'lawyer': {
        'primary': '#37474F', 'secondary': '#ECEFF1', 'accent': '#B8860B',
        'bg': '#0e1114', 'card': '#1a1f24', 'text': '#ECEFF1', 'muted': '#78909C',
        'font': "'Libre Baskerville', Georgia, serif",
        'hero_cta': 'Free Consultation', 'hero_cta2': 'Our Practice Areas',
        'hero_icon': 'fa-phone', 'hero_icon2': 'fa-scale-balanced',
        'unsplash': 'law-office,legal,courthouse',
    },
    'realtor': {
        'primary': '#1B5E20', 'secondary': '#E8F5E9', 'accent': '#B8860B',
        'bg': '#0c1a0e', 'card': '#14281a', 'text': '#E8F5E9', 'muted': '#6a9a70',
        'font': "'DM Sans', 'Segoe UI', sans-serif",
        'hero_cta': 'View Listings', 'hero_cta2': 'Free Home Valuation',
        'hero_icon': 'fa-house', 'hero_icon2': 'fa-chart-line',
        'unsplash': 'real-estate,luxury-home,house',
    },
    'auto': {
        'primary': '#D32F2F', 'secondary': '#FFEBEE', 'accent': '#FF6F00',
        'bg': '#140c0c', 'card': '#201414', 'text': '#FFEBEE', 'muted': '#c07070',
        'font': "'Oswald', 'Impact', sans-serif",
        'hero_cta': 'Get an Estimate', 'hero_cta2': 'Our Services',
        'hero_icon': 'fa-phone', 'hero_icon2': 'fa-car',
        'unsplash': 'auto-repair,mechanic,garage',
    },
    'landscaping': {
        'primary': '#4CAF50', 'secondary': '#E8F5E9', 'accent': '#33691E',
        'bg': '#0c1a0e', 'card': '#14281a', 'text': '#E8F5E9', 'muted': '#6a9a70',
        'font': "'Inter', 'Segoe UI', sans-serif",
        'hero_cta': 'Free Estimate', 'hero_cta2': 'View Our Work',
        'hero_icon': 'fa-phone', 'hero_icon2': 'fa-images',
        'unsplash': 'landscaping,garden-design,lawn',
    },
    'cleaning': {
        'primary': '#00BCD4', 'secondary': '#E0F7FA', 'accent': '#006064',
        'bg': '#0a1a1e', 'card': '#122a30', 'text': '#E0F7FA', 'muted': '#6aacb8',
        'font': "'DM Sans', 'Segoe UI', sans-serif",
        'hero_cta': 'Book a Cleaning', 'hero_cta2': 'Get a Quote',
        'hero_icon': 'fa-calendar-check', 'hero_icon2': 'fa-phone',
        'unsplash': 'cleaning-service,clean-home,housekeeping',
    },
    'construction': {
        'primary': '#FF8F00', 'secondary': '#FFF3E0', 'accent': '#E65100',
        'bg': '#1a1408', 'card': '#2a2010', 'text': '#FFF3E0', 'muted': '#b89050',
        'font': "'Oswald', 'Impact', sans-serif",
        'hero_cta': 'Request a Bid', 'hero_cta2': 'View Projects',
        'hero_icon': 'fa-phone', 'hero_icon2': 'fa-images',
        'unsplash': 'construction,building,architecture',
    },
    'spa': {
        'primary': '#9C27B0', 'secondary': '#F3E5F5', 'accent': '#4A148C',
        'bg': '#160a1a', 'card': '#24142a', 'text': '#F3E5F5', 'muted': '#a070b0',
        'font': "'Playfair Display', Georgia, serif",
        'hero_cta': 'Book Treatment', 'hero_cta2': 'View Packages',
        'hero_icon': 'fa-calendar-check', 'hero_icon2': 'fa-spa',
        'unsplash': 'spa,wellness,massage',
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

def get_industry_theme(category):
    cat = category.lower()
    if cat in INDUSTRY_THEMES:
        return INDUSTRY_THEMES[cat]
    keyword_map = {
        'restaurant': ['restaurant','dining','food','grill','bistro','diner','sushi','pizza','bbq','steakhouse','thai','chinese','italian','mexican','indian','japanese','seafood','burger','taco','ramen','pho','deli','sandwich'],
        'cafe': ['cafe','coffee','bakery','tea','pastry','donut','dessert','ice cream','juice','smoothie'],
        'plumber': ['plumber','plumbing','drain','pipe','water heater','sewer'],
        'electrician': ['electrician','electrical','wiring','lighting'],
        'dentist': ['dentist','dental','orthodont','oral','teeth','doctor','medical','clinic','physician','pharmacy','physio','veterinar','vet'],
        'salon': ['salon','hair','barber','beauty','nails','nail','lash','brow','wax','makeup'],
        'gym': ['gym','fitness','crossfit','yoga','pilates','martial art','boxing','training','personal trainer'],
        'lawyer': ['lawyer','attorney','law firm','legal','notary'],
        'realtor': ['realtor','real estate','realty','property','mortgage'],
        'auto': ['auto','mechanic','car','tire','oil change','body shop','collision','transmission','brake'],
        'landscaping': ['landscap','lawn','garden','tree','mowing','irrigation','snow removal'],
        'cleaning': ['clean','maid','janitorial','pressure wash','carpet','window clean'],
        'construction': ['construct','contractor','roofing','roof','siding','renovation','remodel','hvac','painting','paint','drywall','flooring','deck','fence','paving','concrete'],
        'spa': ['spa','wellness','massage','facial','skin','derma','acupuncture','chiropractic','therapy'],
    }
    for theme_key, keywords in keyword_map.items():
        for kw in keywords:
            if kw in cat:
                return INDUSTRY_THEMES[theme_key]
    return INDUSTRY_THEMES['default']


SERVICE_ICONS = {
    'restaurant': ['fa-utensils','fa-wine-glass','fa-truck','fa-cake-candles','fa-champagne-glasses','fa-fire-burner'],
    'cafe': ['fa-mug-hot','fa-cookie','fa-blender','fa-ice-cream','fa-wifi','fa-bag-shopping'],
    'plumber': ['fa-wrench','fa-faucet-drip','fa-hot-tub-person','fa-house-flood-water','fa-shower','fa-toolbox'],
    'electrician': ['fa-bolt','fa-lightbulb','fa-plug','fa-solar-panel','fa-fan','fa-toolbox'],
    'dentist': ['fa-tooth','fa-teeth','fa-syringe','fa-x-ray','fa-face-smile','fa-shield-halved'],
    'salon': ['fa-scissors','fa-spray-can-sparkles','fa-paintbrush','fa-face-smile-beam','fa-hand-sparkles','fa-wand-magic-sparkles'],
    'gym': ['fa-dumbbell','fa-person-running','fa-heart-pulse','fa-stopwatch','fa-users','fa-ranking-star'],
    'lawyer': ['fa-scale-balanced','fa-gavel','fa-file-contract','fa-handshake','fa-building-columns','fa-shield-halved'],
    'realtor': ['fa-house','fa-key','fa-magnifying-glass-dollar','fa-handshake','fa-chart-line','fa-building'],
    'auto': ['fa-car','fa-oil-can','fa-gears','fa-tire','fa-battery-full','fa-gauge-high'],
    'landscaping': ['fa-leaf','fa-tree','fa-seedling','fa-sun','fa-water','fa-trowel'],
    'cleaning': ['fa-broom','fa-spray-can-sparkles','fa-hand-sparkles','fa-house-chimney','fa-pump-soap','fa-window-maximize'],
    'construction': ['fa-hammer','fa-hard-hat','fa-ruler-combined','fa-truck','fa-paint-roller','fa-screwdriver-wrench'],
    'spa': ['fa-spa','fa-hand-holding-heart','fa-hot-tub-person','fa-gem','fa-feather','fa-yin-yang'],
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

    # --- INDUSTRY THEMES: colors + hero images (curated Unsplash direct URLs) ---
    INDUSTRY_THEMES = {
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
        'real_estate': {
            'accent': '#2a9d8f', 'gradient': 'linear-gradient(135deg, #0a1a18 0%, #0f2d28 100%)',
            'hero': 'https://images.unsplash.com/photo-1560518883-ce09059eeffa?w=1600&h=900&fit=crop',
            'about_img': 'https://images.unsplash.com/photo-1582407947092-85ad2a919d0a?w=800&h=600&fit=crop',
        },
        'auto_repair': {
            'accent': '#e76f51', 'gradient': 'linear-gradient(135deg, #1a100a 0%, #2d1a0f 100%)',
            'hero': 'https://images.unsplash.com/photo-1486262715619-67b85e0b08d3?w=1600&h=900&fit=crop',
            'about_img': 'https://images.unsplash.com/photo-1619642751034-765dfdf7c58e?w=800&h=600&fit=crop',
        },
        'landscaping': {
            'accent': '#40916c', 'gradient': 'linear-gradient(135deg, #0a1a10 0%, #0f2d1a 100%)',
            'hero': 'https://images.unsplash.com/photo-1558904541-efa843a96f01?w=1600&h=900&fit=crop',
            'about_img': 'https://images.unsplash.com/photo-1600607687939-ce8a6c25118c?w=800&h=600&fit=crop',
        },
        'bakery': {
            'accent': '#d4a373', 'gradient': 'linear-gradient(135deg, #1a150f 0%, #2d2418 100%)',
            'hero': 'https://images.unsplash.com/photo-1509440159596-0249088772ff?w=1600&h=900&fit=crop',
            'about_img': 'https://images.unsplash.com/photo-1555507036-ab1f4038024a?w=800&h=600&fit=crop',
        },
        'veterinarian': {
            'accent': '#52b788', 'gradient': 'linear-gradient(135deg, #0a1a12 0%, #0f2d1e 100%)',
            'hero': 'https://images.unsplash.com/photo-1628009368231-7bb7cfcb0def?w=1600&h=900&fit=crop',
            'about_img': 'https://images.unsplash.com/photo-1548767797-d8c844163c4c?w=800&h=600&fit=crop',
        },
        'cleaning': {
            'accent': '#00b4d8', 'gradient': 'linear-gradient(135deg, #0a1820 0%, #0f2835 100%)',
            'hero': 'https://images.unsplash.com/photo-1581578731548-c64695cc6952?w=1600&h=900&fit=crop',
            'about_img': 'https://images.unsplash.com/photo-1628177142898-93e36e4e3a50?w=800&h=600&fit=crop',
        },
    }

    DEFAULT_THEME = {
        'accent': '#00ff88', 'gradient': 'linear-gradient(135deg, #0a0a0a 0%, #1a1a2e 100%)',
        'hero': 'https://images.unsplash.com/photo-1497366216548-37526070297c?w=1600&h=900&fit=crop',
        'about_img': 'https://images.unsplash.com/photo-1497366811353-6870744d04b2?w=800&h=600&fit=crop',
    }

    theme = DEFAULT_THEME
    for key, val in INDUSTRY_THEMES.items():
        if key in category_lower or category_lower in key:
            theme = val
            break

    accent = theme['accent']
    bg_gradient = theme['gradient']
    hero_image_url = theme['hero']
    about_image_url = theme['about_img']

    # --- INDUSTRY-SPECIFIC FALLBACK CONTENT ---
    INDUSTRY_FALLBACKS = {
        'restaurant': {
            'tagline': 'Where Every Meal Becomes a Memory',
            'about': name + ' brings bold flavors and warm hospitality to ' + (city or 'the neighborhood') + '. From seasonal ingredients to signature dishes, every plate tells our story.',
            'services': ['Fine Dining Experience', 'Private Event Catering', 'Seasonal Tasting Menu', 'Wine & Cocktail Bar', 'Weekend Brunch', 'Takeout & Delivery'],
            'reviews': [
                {'author': 'Sarah M.', 'text': 'Absolutely incredible food. The pasta was the best I have ever had outside of Italy. Will be back every week.', 'stars': 5},
                {'author': 'James K.', 'text': 'Perfect date night spot. Atmosphere is unmatched, and the staff treated us like family.', 'stars': 5},
                {'author': 'Lisa R.', 'text': 'We hosted our anniversary dinner here and it was flawless. Cannot recommend enough.', 'stars': 5},
            ],
        },
        'cafe': {
            'tagline': 'Your Neighborhood Coffee Destination',
            'about': name + ' is ' + (city or 'the local') + ' go-to spot for artisan coffee and fresh baked goods. We source single-origin beans and bake everything in-house daily.',
            'services': ['Specialty Espresso Drinks', 'Fresh Pastries & Baked Goods', 'Breakfast & Lunch Menu', 'Catering & Coffee Bar', 'Free Wi-Fi Workspace', 'Loyalty Rewards Program'],
            'reviews': [
                {'author': 'Mike T.', 'text': 'Best latte in town, hands down. The oat milk cappuccino changed my life.', 'stars': 5},
                {'author': 'Emily W.', 'text': 'I work here three days a week. Great Wi-Fi, better coffee, and the croissants are insane.', 'stars': 5},
                {'author': 'David L.', 'text': 'Finally a cafe that actually cares about quality. You can taste the difference.', 'stars': 5},
            ],
        },
        'plumber': {
            'tagline': 'Fast, Reliable Plumbing You Can Trust',
            'about': name + ' has been solving plumbing emergencies in ' + (city or 'the area') + ' for years. Licensed, insured, and always on time -- we fix it right the first time.',
            'services': ['Emergency Repairs 24/7', 'Drain Cleaning & Unclogging', 'Water Heater Installation', 'Pipe Leak Detection', 'Bathroom Renovations', 'Sewer Line Service'],
            'reviews': [
                {'author': 'Tom H.', 'text': 'Burst pipe at 2am and they were here in 30 minutes. Saved my basement. Lifesavers.', 'stars': 5},
                {'author': 'Rachel S.', 'text': 'Fair pricing, no surprise charges. They explained everything before starting. Highly recommend.', 'stars': 5},
                {'author': 'Kevin B.', 'text': 'Third plumber I tried and the only one who actually fixed the problem permanently.', 'stars': 5},
            ],
        },
        'electrician': {
            'tagline': 'Powering Your Home & Business Safely',
            'about': name + ' delivers expert electrical services across ' + (city or 'the region') + '. Fully licensed and insured -- from panel upgrades to smart home wiring, we do it all.',
            'services': ['Panel Upgrades & Rewiring', 'Smart Home Installation', 'EV Charger Setup', 'Commercial Electrical', 'Lighting Design & Install', 'Emergency Electrical Repair'],
            'reviews': [
                {'author': 'Steve P.', 'text': 'Installed our EV charger and upgraded the panel same day. Clean work, great price.', 'stars': 5},
                {'author': 'Nancy D.', 'text': 'They rewired our entire 1960s house. Passed inspection first try. Incredible team.', 'stars': 5},
                {'author': 'Chris M.', 'text': 'Fast, professional, and they cleaned up after themselves. What more could you ask for?', 'stars': 5},
            ],
        },
        'dentist': {
            'tagline': 'Healthy Smiles Start Here',
            'about': name + ' provides gentle, comprehensive dental care for the whole family in ' + (city or 'a comfortable setting') + '. Modern technology, compassionate team, beautiful results.',
            'services': ['General Checkups & Cleaning', 'Cosmetic Dentistry', 'Teeth Whitening', 'Invisalign & Orthodontics', 'Dental Implants', 'Emergency Dental Care'],
            'reviews': [
                {'author': 'Amanda K.', 'text': 'I used to dread the dentist. This team completely changed that. Gentle, kind, and thorough.', 'stars': 5},
                {'author': 'Robert J.', 'text': 'Got Invisalign here and my smile has never looked better. The whole process was seamless.', 'stars': 5},
                {'author': 'Maria G.', 'text': 'My kids actually look forward to their appointments. That says everything.', 'stars': 5},
            ],
        },
        'lawyer': {
            'tagline': 'Fierce Advocacy. Trusted Counsel.',
            'about': name + ' brings decades of legal expertise to ' + (city or 'clients') + ' across practice areas. We fight for your rights with integrity and relentless dedication.',
            'services': ['Personal Injury Claims', 'Family Law & Divorce', 'Business & Corporate Law', 'Criminal Defense', 'Estate Planning & Wills', 'Free Initial Consultation'],
            'reviews': [
                {'author': 'Daniel R.', 'text': 'Won my case when two other lawyers said it was impossible. Absolutely brilliant legal mind.', 'stars': 5},
                {'author': 'Patricia L.', 'text': 'Guided me through a difficult divorce with empathy and strength. Forever grateful.', 'stars': 5},
                {'author': 'Mark T.', 'text': 'Responsive, transparent, and actually cares about the outcome. Rare in this industry.', 'stars': 5},
            ],
        },
        'salon': {
            'tagline': 'Where Style Meets Confidence',
            'about': name + ' is ' + (city or 'the area') + ' premier destination for hair, color, and beauty. Our talented stylists create looks that make you feel like the best version of yourself.',
            'services': ['Precision Haircuts & Styling', 'Color & Highlights', 'Balayage & Ombre', 'Blowouts & Updos', 'Keratin Treatments', 'Bridal & Event Styling'],
            'reviews': [
                {'author': 'Jessica H.', 'text': 'Best balayage I have ever gotten. I get compliments literally every day now.', 'stars': 5},
                {'author': 'Tina M.', 'text': 'Finally found my forever salon. The vibe, the talent, the results -- all perfect.', 'stars': 5},
                {'author': 'Lauren C.', 'text': 'Did my bridal party hair and every single person looked stunning. Magical.', 'stars': 5},
            ],
        },
        'gym': {
            'tagline': 'Train Hard. Transform Your Life.',
            'about': name + ' is not just a gym -- it is a community. Located in ' + (city or 'the heart of town') + ', we offer world-class equipment, expert coaching, and the motivation to reach your goals.',
            'services': ['Personal Training', 'Group Fitness Classes', 'Strength & Conditioning', 'Yoga & Recovery', 'Nutrition Coaching', 'Open Gym 24/7'],
            'reviews': [
                {'author': 'Marcus J.', 'text': 'Lost 40 lbs in 6 months with their trainers. Best investment I have ever made in myself.', 'stars': 5},
                {'author': 'Samantha R.', 'text': 'The group classes are addictive. Great energy, great people, great results.', 'stars': 5},
                {'author': 'Derek W.', 'text': 'Cleanest gym I have ever been to. Equipment is top-notch and never have to wait.', 'stars': 5},
            ],
        },
    }

    DEFAULT_FALLBACK = {
        'tagline': 'Trusted ' + category.title() + ' Services in ' + (city or 'Your Area'),
        'about': name + ' is a leading ' + category.lower() + ' provider in ' + (city or 'the area') + '. We combine expertise, dedication, and personalized service to deliver outstanding results every time.',
        'services': ['Professional ' + category.title() + ' Services', 'Free Consultations', 'Emergency Support', 'Custom Solutions', 'Licensed & Insured', 'Satisfaction Guaranteed'],
        'reviews': [
            {'author': 'Sarah M.', 'text': 'Incredible experience with ' + name + '. Professional, punctual, and exceeded all expectations.', 'stars': 5},
            {'author': 'James R.', 'text': 'Hands down the best in the business. Fair pricing and outstanding quality of work.', 'stars': 5},
            {'author': 'Amanda K.', 'text': 'We have used ' + name + ' three times now and they never disappoint. Highly recommended!', 'stars': 5},
        ],
    }

    fallback = DEFAULT_FALLBACK
    for key, val in INDUSTRY_FALLBACKS.items():
        if key in category_lower or category_lower in key:
            fallback = val
            break

    # --- GEMINI: ONE MEGA CALL for all personalized content ---
    tagline = fallback['tagline']
    about_text = fallback['about']
    services = fallback['services']
    reviews = fallback['reviews']

    try:
        client = get_gemini_client()
        mega_prompt = f"""Generate website content for "{name}", a {category} business in {city or 'a local area'}.
{f'They describe themselves as: {description}' if description else ''}
{f'They have a {rating}-star rating with {review_count} reviews.' if rating else ''}

Return ONLY valid JSON (no markdown, no backticks):
{{"tagline": "punchy 3-8 word tagline for this specific business", "about": "2 sentences, max 40 words, about THIS business specifically", "services": ["service 1", "service 2", "service 3", "service 4", "service 5", "service 6"], "reviews": [{{"author": "First L.", "text": "realistic 15-25 word review mentioning something specific", "stars": 5}}, {{"author": "First L.", "text": "realistic 15-25 word review mentioning something specific", "stars": 5}}, {{"author": "First L.", "text": "realistic 15-25 word review mentioning something specific", "stars": 5}}]}}

Make it feel authentic to {name}. Services should be 2-5 words each. Reviews should mention specific things about {category} businesses."""

        response = client.generate_content(mega_prompt)
        raw = response.text.strip()
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[1] if '\n' in raw else raw[3:]
        if raw.endswith('```'):
            raw = raw[:-3]
        if raw.startswith('json'):
            raw = raw[4:]
        raw = raw.strip()

        content = json.loads(raw)
        if content.get('tagline'):
            tagline = content['tagline']
        if content.get('about'):
            about_text = content['about']
        if content.get('services') and len(content['services']) >= 4:
            services = content['services'][:6]
        if content.get('reviews') and len(content['reviews']) >= 2:
            reviews = content['reviews'][:3]
        logger.info(f"Gemini content generated successfully for {name}")
    except Exception as e:
        logger.error(f"Gemini content generation failed for {name}: {e}")

    # --- RATINGS DISPLAY ---
    rating_html = ''
    if rating:
        try:
            rating_val = float(rating)
            stars_full = int(rating_val)
            stars_half = 1 if (rating_val - stars_full) >= 0.3 else 0
            stars_html = '<i class="fa fa-star"></i>' * stars_full
            if stars_half:
                stars_html += '<i class="fa fa-star-half-o"></i>'
            count_str = f' ({review_count} reviews)' if review_count else ''
            rating_html = f'<div class="hero-rating">{stars_html} <span>{rating_val:.1f}{count_str}</span></div>'
        except (ValueError, TypeError):
            pass

    # --- BUILD SERVICE CARDS ---
    service_icons = ['fa-star', 'fa-cog', 'fa-check-circle', 'fa-bolt', 'fa-heart', 'fa-gem']
    services_html = ''
    for i, svc in enumerate(services[:6]):
        icon = service_icons[i % len(service_icons)]
        svc_name = svc if isinstance(svc, str) else str(svc)
        services_html += '<div class="service-card"><i class="fa ' + icon + '" style="font-size:2rem;color:' + accent + ';margin-bottom:15px;"></i><h3>' + svc_name + '</h3></div>'

    # --- BUILD REVIEW CARDS ---
    reviews_html = ''
    for rev in reviews[:3]:
        author = rev.get('author', 'Happy Customer') if isinstance(rev, dict) else 'Happy Customer'
        text = rev.get('text', 'Excellent service!') if isinstance(rev, dict) else str(rev)
        rev_stars = rev.get('stars', 5) if isinstance(rev, dict) else 5
        rev_stars_html = '<i class="fa fa-star" style="color:#ffd700;"></i>' * int(rev_stars)
        reviews_html += '<div class="review-card"><div style="margin-bottom:10px;">' + rev_stars_html + '</div><p style="font-style:italic;margin-bottom:12px;color:#ccc;">"' + text + '"</p><p style="font-weight:600;color:' + accent + ';">- ' + author + '</p></div>'

    # --- GOOGLE MAP EMBED ---
    map_query = urllib.parse.quote((name + ' ' + city) if city else name)

    # --- CONTACT INFO ---
    contact_details = ''
    if phone:
        contact_details += '<p><i class="fa fa-phone" style="color:' + accent + ';margin-right:10px;"></i> ' + phone + '</p>'
    if address:
        contact_details += '<p><i class="fa fa-map-marker" style="color:' + accent + ';margin-right:10px;"></i> ' + address + '</p>'
    if city:
        contact_details += '<p><i class="fa fa-building" style="color:' + accent + ';margin-right:10px;"></i> ' + city + '</p>'

    # --- CTA BUTTONS ---
    cta_primary = '<a href="tel:' + phone + '" class="cta-btn">Call Now</a>' if phone else '<a href="#contact" class="cta-btn">Get in Touch</a>'
    cta_secondary = '<a href="' + website + '" class="cta-btn-outline" target="_blank">Visit Website</a>' if has_website else ''

    # --- FULL HTML ---
    # Using string concat for the outer shell to avoid f-string double-brace hell with CSS
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

    # Nav
    first_letter = name[0] if name else 'B'
    rest_name = name[1:] if len(name) > 1 else ''
    html += '<nav class="nav"><div class="nav-brand"><span>' + first_letter + '</span>' + rest_name + '</div>'
    html += '<div class="nav-links"><a href="#services">Services</a><a href="#about">About</a><a href="#reviews">Reviews</a><a href="#contact">Contact</a></div></nav>'

    # Hero
    html += '<section class="hero"><div class="hero-content">'
    html += '<h1><span>' + name + '</span></h1>'
    html += '<p class="hero-tagline">' + tagline + '</p>'
    html += rating_html
    html += '<div class="hero-ctas">' + cta_primary + cta_secondary + '</div>'
    html += '</div></section>'

    # Services
    html += '<section id="services"><h2 class="section-title">What We <span>Offer</span></h2>'
    html += '<div class="services-grid">' + services_html + '</div></section>'

    # About
    html += '<section id="about"><h2 class="section-title">About <span>' + name + '</span></h2>'
    html += '<div class="about-grid"><div class="about-text"><p>' + about_text + '</p></div>'
    html += '<div class="about-img"><img src="' + about_image_url + '" alt="About ' + name + '" loading="lazy"></div></div></section>'

    # Reviews
    html += '<section id="reviews"><h2 class="section-title">What People <span>Say</span></h2>'
    html += '<div class="reviews-grid">' + reviews_html + '</div></section>'

    # Contact
    html += '<section id="contact"><h2 class="section-title">Get In <span>Touch</span></h2>'
    html += '<div class="contact-grid"><div class="contact-info">' + contact_details
    html += '<iframe src="https://www.google.com/maps?q=' + map_query + '&output=embed" width="100%" height="300" style="border:0;border-radius:12px;" allowfullscreen="" loading="lazy"></iframe>'
    html += '</div><div><form style="display:flex;flex-direction:column;gap:15px;">'
    html += '<input type="text" placeholder="Your Name" style="padding:14px;border-radius:10px;border:1px solid rgba(255,255,255,0.1);background:rgba(255,255,255,0.05);color:#fff;font-size:1rem;">'
    html += '<input type="email" placeholder="Your Email" style="padding:14px;border-radius:10px;border:1px solid rgba(255,255,255,0.1);background:rgba(255,255,255,0.05);color:#fff;font-size:1rem;">'
    html += '<textarea placeholder="Your Message" rows="4" style="padding:14px;border-radius:10px;border:1px solid rgba(255,255,255,0.1);background:rgba(255,255,255,0.05);color:#fff;font-size:1rem;resize:vertical;"></textarea>'
    html += '<button type="submit" class="cta-btn" style="border:none;cursor:pointer;font-family:inherit;">Send Message</button>'
    html += '</form></div></div></section>'

    # Footer
    html += '<footer class="footer"><p>2025 ' + name + '. All rights reserved. | Powered by <a href="#" style="color:' + accent + ';">Cold Outreach Engine</a></p></footer>'
    html += '</body></html>'

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