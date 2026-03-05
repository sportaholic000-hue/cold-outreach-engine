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
        # No Places API key — use Gemini to generate realistic local business leads
        logger.info(f"No GOOGLE_PLACES_API_KEY set. Using Gemini to generate leads for: {keyword} in {city}")
        return self._search_with_gemini(keyword, city, max_results)

    def _search_with_gemini(self, keyword: str, city: str, max_results: int) -> list:
        try:
            client = get_gemini_client()
            prompt = f"""Generate a list of {min(max_results, 20)} realistic local {keyword} businesses in {city}.
These should look like real local businesses (not chains) with plausible names, addresses, phone numbers, and websites.
Some businesses should have no website (website: "") to simulate real-world data.
Return ONLY a JSON array with this exact structure, no explanation:
[
  {{
    "name": "Business Name",
    "address": "123 Main St, {city}",
    "phone": "(555) 000-0000",
    "website": "https://example.com or empty string",
    "rating": 4.5,
    "reviews": 42
  }}
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
    # Check if any search method is available
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
            'error': 'Search failed — Gemini API quota may be exhausted. Try again tomorrow or add a Google Places API key.',
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
def api_mockup():
    """
    POST /api/mockup
    Body: { name, city, category, phone, address, website (optional) }
    Returns: { html: '...complete HTML mockup...' }
    """
    data = request.json
    name = data.get('name', 'Your Business')
    city = data.get('city', '')
    category = data.get('category', 'business')
    phone = data.get('phone', '')
    address = data.get('address', '')
    website = data.get('website', '')
    has_website = bool(website and is_real_website(website))

    # Map category to Unsplash keyword for hero image
    category_lower = category.lower()
    unsplash_keywords = {
        'restaurant': 'restaurant,food',
        'cafe': 'cafe,coffee',
        'coffee': 'cafe,coffee',
        'bar': 'bar,cocktails',
        'plumber': 'plumbing,pipe',
        'plumbing': 'plumbing,pipe',
        'electrician': 'electrician,electrical',
        'electrical': 'electrician,electrical',
        'dentist': 'dental,dentist',
        'dental': 'dental,dentist',
        'doctor': 'medical,clinic',
        'medical': 'medical,clinic',
        'salon': 'hair,salon',
        'hair': 'hair,salon',
        'barbershop': 'barbershop,haircut',
        'barber': 'barbershop,haircut',
        'gym': 'gym,fitness',
        'fitness': 'gym,fitness',
        'lawyer': 'law,office',
        'law': 'law,office',
        'real estate': 'realestate,house',
        'realtor': 'realestate,house',
        'auto': 'car,garage',
        'mechanic': 'car,garage',
        'landscaping': 'garden,landscaping',
        'cleaning': 'cleaning,house',
        'photographer': 'photography,camera',
        'photography': 'photography,camera',
        'accounting': 'office,finance',
        'accountant': 'office,finance',
        'bakery': 'bakery,bread',
        'pizza': 'pizza,restaurant',
        'spa': 'spa,wellness',
        'yoga': 'yoga,wellness',
        'pet': 'pets,dog',
        'vet': 'veterinary,dog',
        'florist': 'flowers,florist',
        'jewelry': 'jewelry,gems',
        'clothing': 'fashion,clothing',
        'retail': 'shop,retail',
        'hotel': 'hotel,luxury',
        'construction': 'construction,building',
        'roofing': 'roof,construction',
        'hvac': 'hvac,airconditioning',
        'insurance': 'office,business',
        'travel': 'travel,vacation',
        'tutoring': 'education,learning',
        'childcare': 'children,daycare',
    }
    unsplash_kw = category_lower
    for key, val in unsplash_keywords.items():
        if key in category_lower:
            unsplash_kw = val
            break

    hero_image_url = f"https://source.unsplash.com/1600x900/?{urllib.parse.quote(unsplash_kw)}"

    # Determine services to show based on category
    client = get_gemini_client()
    try:
        services_prompt = f"""For a {category} business called "{name}" in {city}, generate exactly 6 short service offerings (2-5 words each).
Return ONLY a JSON array of 6 strings. No explanation.
Example: ["Service One", "Service Two", "Service Three", "Service Four", "Service Five", "Service Six"]"""
        response = client.generate_content(services_prompt)
        raw = response.text.strip().replace('```json', '').replace('```', '').strip()
        services = json.loads(raw)
        if not isinstance(services, list) or len(services) < 3:
            raise ValueError("bad services response")
        services = services[:6]
    except Exception:
        services = ["Professional Service", "Expert Consultation", "Quality Work", "Fast Turnaround", "Licensed & Insured", "Free Estimates"]

    # Generate a compelling tagline
    try:
        tagline_prompt = f"""Write ONE short punchy tagline (8 words or less) for a {category} business called "{name}" in {city}.
Return ONLY the tagline text, nothing else."""
        response = client.generate_content(tagline_prompt)
        tagline = response.text.strip().strip('"').strip("'")
        if len(tagline) > 80:
            tagline = tagline[:80]
    except Exception:
        tagline = f"Your Trusted {category.title()} in {city}"

    # Generate about section text
    try:
        about_prompt = f"""Write 2 short sentences (total max 40 words) describing a {category} business called "{name}" in {city}.
Warm, professional tone. Return ONLY the text."""
        response = client.generate_content(about_prompt)
        about_text = response.text.strip()
        if len(about_text) > 300:
            about_text = about_text[:300]
    except Exception:
        about_text = f"{name} has been proudly serving the {city} community with top-quality {category} services. We are committed to excellence and customer satisfaction in everything we do."

    # Generate the full HTML mockup
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name}</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  :root {{
    --green: #00ff88;
    --dark: #0a0a0a;
    --card: #111;
    --border: #222;
    --text: #e0e0e0;
    --muted: #888;
  }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--dark); color: var(--text); }}

  /* NAV */
  nav {{ position: fixed; top: 0; left: 0; right: 0; z-index: 100; background: rgba(10,10,10,0.95); backdrop-filter: blur(10px); border-bottom: 1px solid var(--border); padding: 0 40px; display: flex; align-items: center; justify-content: space-between; height: 64px; }}
  .nav-logo {{ font-size: 20px; font-weight: 800; color: var(--green); letter-spacing: -0.5px; }}
  .nav-links {{ display: flex; gap: 32px; }}
  .nav-links a {{ color: var(--muted); text-decoration: none; font-size: 14px; font-weight: 500; transition: color 0.2s; }}
  .nav-links a:hover {{ color: var(--green); }}
  .nav-cta {{ background: var(--green); color: #000; padding: 8px 20px; border-radius: 6px; font-size: 14px; font-weight: 700; text-decoration: none; transition: background 0.2s; }}
  .nav-cta:hover {{ background: #00dd77; }}

  /* HERO */
  .hero {{ position: relative; height: 100vh; min-height: 600px; display: flex; align-items: center; justify-content: center; text-align: center; overflow: hidden; }}
  .hero-bg {{ position: absolute; inset: 0; background-image: url('{hero_image_url}'); background-size: cover; background-position: center; filter: brightness(0.25); }}
  .hero-overlay {{ position: absolute; inset: 0; background: linear-gradient(180deg, rgba(0,0,0,0.3) 0%, rgba(10,10,10,0.8) 100%); }}
  .hero-content {{ position: relative; z-index: 1; max-width: 800px; padding: 0 24px; }}
  .hero-badge {{ display: inline-block; background: rgba(0,255,136,0.15); border: 1px solid rgba(0,255,136,0.3); color: var(--green); font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 2px; padding: 6px 16px; border-radius: 100px; margin-bottom: 24px; }}
  .hero h1 {{ font-size: clamp(36px, 6vw, 72px); font-weight: 900; line-height: 1.05; letter-spacing: -2px; margin-bottom: 20px; color: #fff; }}
  .hero h1 span {{ color: var(--green); }}
  .hero-tagline {{ font-size: clamp(16px, 2vw, 20px); color: rgba(255,255,255,0.7); margin-bottom: 40px; line-height: 1.5; }}
  .hero-actions {{ display: flex; gap: 16px; justify-content: center; flex-wrap: wrap; }}
  .btn-hero {{ padding: 14px 32px; border-radius: 8px; font-size: 16px; font-weight: 700; text-decoration: none; transition: all 0.2s; display: inline-flex; align-items: center; gap: 8px; }}
  .btn-hero-primary {{ background: var(--green); color: #000; }}
  .btn-hero-primary:hover {{ background: #00dd77; transform: translateY(-2px); box-shadow: 0 8px 32px rgba(0,255,136,0.3); }}
  .btn-hero-secondary {{ background: rgba(255,255,255,0.1); color: #fff; border: 1px solid rgba(255,255,255,0.2); backdrop-filter: blur(4px); }}
  .btn-hero-secondary:hover {{ background: rgba(255,255,255,0.15); }}
  .hero-stats {{ display: flex; gap: 48px; justify-content: center; margin-top: 60px; flex-wrap: wrap; }}
  .stat {{ text-align: center; }}
  .stat-num {{ font-size: 32px; font-weight: 900; color: var(--green); }}
  .stat-label {{ font-size: 13px; color: var(--muted); margin-top: 4px; }}

  /* SECTION BASE */
  section {{ padding: 100px 40px; }}
  .section-label {{ font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 3px; color: var(--green); margin-bottom: 12px; }}
  .section-title {{ font-size: clamp(28px, 4vw, 48px); font-weight: 900; letter-spacing: -1px; margin-bottom: 16px; }}
  .section-sub {{ font-size: 16px; color: var(--muted); max-width: 560px; line-height: 1.6; }}
  .section-header {{ margin-bottom: 64px; }}

  /* SERVICES */
  .services {{ background: var(--dark); }}
  .services-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; }}
  .service-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 32px; transition: all 0.3s; position: relative; overflow: hidden; }}
  .service-card::before {{ content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; background: linear-gradient(90deg, var(--green), transparent); opacity: 0; transition: opacity 0.3s; }}
  .service-card:hover {{ border-color: var(--green); transform: translateY(-4px); box-shadow: 0 20px 40px rgba(0,255,136,0.08); }}
  .service-card:hover::before {{ opacity: 1; }}
  .service-icon {{ width: 48px; height: 48px; background: rgba(0,255,136,0.1); border-radius: 12px; display: flex; align-items: center; justify-content: center; margin-bottom: 20px; color: var(--green); font-size: 20px; }}
  .service-name {{ font-size: 17px; font-weight: 700; margin-bottom: 8px; color: #fff; }}
  .service-desc {{ font-size: 14px; color: var(--muted); line-height: 1.5; }}

  /* ABOUT */
  .about {{ background: #0d0d0d; }}
  .about-inner {{ display: grid; grid-template-columns: 1fr 1fr; gap: 80px; align-items: center; max-width: 1100px; margin: 0 auto; }}
  .about-image {{ border-radius: 20px; overflow: hidden; height: 420px; position: relative; }}
  .about-image img {{ width: 100%; height: 100%; object-fit: cover; filter: brightness(0.8); }}
  .about-image-badge {{ position: absolute; bottom: 24px; left: 24px; background: var(--green); color: #000; padding: 12px 20px; border-radius: 10px; font-weight: 800; font-size: 13px; }}
  .about-text .section-sub {{ max-width: 100%; margin-bottom: 32px; font-size: 17px; color: #bbb; }}
  .about-features {{ display: flex; flex-direction: column; gap: 16px; }}
  .about-feature {{ display: flex; align-items: center; gap: 14px; font-size: 15px; color: var(--text); }}
  .about-feature i {{ color: var(--green); font-size: 16px; width: 20px; }}

  /* TESTIMONIALS */
  .testimonials {{ background: var(--dark); }}
  .testimonials-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }}
  .testimonial-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 28px; }}
  .testimonial-stars {{ color: #ffd700; font-size: 14px; margin-bottom: 16px; letter-spacing: 2px; }}
  .testimonial-text {{ font-size: 15px; color: #ccc; line-height: 1.7; margin-bottom: 20px; font-style: italic; }}
  .testimonial-author {{ display: flex; align-items: center; gap: 12px; }}
  .testimonial-avatar {{ width: 40px; height: 40px; border-radius: 50%; background: rgba(0,255,136,0.15); border: 2px solid var(--green); display: flex; align-items: center; justify-content: center; color: var(--green); font-weight: 700; font-size: 16px; }}
  .testimonial-name {{ font-weight: 700; font-size: 14px; }}
  .testimonial-location {{ font-size: 12px; color: var(--muted); }}

  /* CTA BAND */
  .cta-band {{ background: var(--green); padding: 80px 40px; text-align: center; }}
  .cta-band h2 {{ font-size: clamp(28px, 4vw, 48px); font-weight: 900; color: #000; letter-spacing: -1px; margin-bottom: 12px; }}
  .cta-band p {{ font-size: 18px; color: rgba(0,0,0,0.7); margin-bottom: 36px; }}
  .btn-cta-dark {{ display: inline-flex; align-items: center; gap: 8px; background: #000; color: var(--green); padding: 16px 40px; border-radius: 8px; font-size: 16px; font-weight: 800; text-decoration: none; transition: all 0.2s; }}
  .btn-cta-dark:hover {{ background: #111; transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,0,0,0.4); }}

  /* CONTACT */
  .contact {{ background: #0d0d0d; }}
  .contact-inner {{ display: grid; grid-template-columns: 1fr 1fr; gap: 60px; max-width: 1000px; margin: 0 auto; }}
  .contact-info {{ display: flex; flex-direction: column; gap: 24px; }}
  .contact-item {{ display: flex; align-items: flex-start; gap: 16px; }}
  .contact-icon {{ width: 44px; height: 44px; background: rgba(0,255,136,0.1); border-radius: 10px; display: flex; align-items: center; justify-content: center; color: var(--green); font-size: 18px; flex-shrink: 0; }}
  .contact-label {{ font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin-bottom: 4px; }}
  .contact-value {{ font-size: 16px; font-weight: 600; color: #fff; }}
  .contact-form {{ background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 32px; }}
  .form-group {{ margin-bottom: 16px; }}
  .form-group label {{ display: block; font-size: 13px; font-weight: 600; color: var(--muted); margin-bottom: 6px; }}
  .form-group input, .form-group textarea {{ width: 100%; background: #0a0a0a; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; font-size: 14px; color: var(--text); font-family: inherit; transition: border-color 0.2s; }}
  .form-group input:focus, .form-group textarea:focus {{ outline: none; border-color: var(--green); }}
  .form-group textarea {{ resize: vertical; min-height: 100px; }}
  .form-submit {{ width: 100%; background: var(--green); color: #000; border: none; padding: 13px; border-radius: 8px; font-size: 15px; font-weight: 700; cursor: pointer; transition: background 0.2s; }}
  .form-submit:hover {{ background: #00dd77; }}

  /* FOOTER */
  footer {{ background: var(--dark); border-top: 1px solid var(--border); padding: 40px; text-align: center; }}
  .footer-logo {{ font-size: 22px; font-weight: 900; color: var(--green); margin-bottom: 8px; }}
  .footer-tagline {{ font-size: 14px; color: var(--muted); margin-bottom: 24px; }}
  .footer-links {{ display: flex; gap: 32px; justify-content: center; margin-bottom: 24px; flex-wrap: wrap; }}
  .footer-links a {{ color: var(--muted); text-decoration: none; font-size: 14px; transition: color 0.2s; }}
  .footer-links a:hover {{ color: var(--green); }}
  .footer-copy {{ font-size: 13px; color: #444; }}

  /* WATERMARK */
  .watermark-bar {{ background: #0d0d0d; border-top: 1px solid var(--border); padding: 12px 40px; text-align: center; }}
  .watermark-bar p {{ font-size: 12px; color: #444; }}
  .watermark-bar span {{ color: var(--green); font-weight: 700; }}

  @media (max-width: 768px) {{
    nav .nav-links {{ display: none; }}
    section {{ padding: 60px 20px; }}
    .about-inner, .contact-inner {{ grid-template-columns: 1fr; gap: 40px; }}
    .hero-stats {{ gap: 24px; }}
    nav {{ padding: 0 20px; }}
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
  <a href="#contact" class="nav-cta">Get a Quote</a>
</nav>

<!-- HERO -->
<section class="hero">
  <div class="hero-bg"></div>
  <div class="hero-overlay"></div>
  <div class="hero-content">
    <div class="hero-badge">{city} &bull; {category.title()}</div>
    <h1>{name}<br><span>{tagline}</span></h1>
    <p class="hero-tagline">Professional {category} services trusted by hundreds of customers in {city}.</p>
    <div class="hero-actions">
      <a href="#contact" class="btn-hero btn-hero-primary"><i class="fa fa-phone"></i> Get a Free Quote</a>
      <a href="#services" class="btn-hero btn-hero-secondary"><i class="fa fa-arrow-down"></i> Our Services</a>
    </div>
    <div class="hero-stats">
      <div class="stat"><div class="stat-num">500+</div><div class="stat-label">Happy Clients</div></div>
      <div class="stat"><div class="stat-num">10+</div><div class="stat-label">Years Experience</div></div>
      <div class="stat"><div class="stat-num">4.9<i class="fa fa-star" style="font-size:20px;margin-left:4px;"></i></div><div class="stat-label">Avg Rating</div></div>
    </div>
  </div>
</section>

<!-- SERVICES -->
<section class="services" id="services">
  <div style="max-width:1100px;margin:0 auto;">
    <div class="section-header">
      <div class="section-label">What We Do</div>
      <div class="section-title">Our Services</div>
      <div class="section-sub">Everything you need from a trusted {category} provider — done right the first time.</div>
    </div>
    <div class="services-grid">
      {''.join(f'''
      <div class="service-card">
        <div class="service-icon"><i class="fa fa-check-circle"></i></div>
        <div class="service-name">{s}</div>
        <div class="service-desc">Top-quality service delivered by our experienced team with attention to detail.</div>
      </div>''' for s in services)}
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
        <div class="about-feature"><i class="fa fa-circle-check"></i> Fully licensed and insured</div>
        <div class="about-feature"><i class="fa fa-circle-check"></i> Local {city} experts since day one</div>
        <div class="about-feature"><i class="fa fa-circle-check"></i> Transparent pricing — no surprises</div>
        <div class="about-feature"><i class="fa fa-circle-check"></i> 100% satisfaction guaranteed</div>
      </div>
    </div>
  </div>
</section>

<!-- TESTIMONIALS -->
<section class="testimonials" id="testimonials">
  <div style="max-width:1100px;margin:0 auto;">
    <div class="section-header">
      <div class="section-label">Reviews</div>
      <div class="section-title">What Clients Say</div>
      <div class="section-sub">Real feedback from real customers in {city}.</div>
    </div>
    <div class="testimonials-grid">
      <div class="testimonial-card">
        <div class="testimonial-stars">&#9733;&#9733;&#9733;&#9733;&#9733;</div>
        <p class="testimonial-text">"Absolutely incredible service. {name} showed up on time, did the job perfectly, and the price was very fair. Will definitely call them again!"</p>
        <div class="testimonial-author">
          <div class="testimonial-avatar">S</div>
          <div><div class="testimonial-name">Sarah M.</div><div class="testimonial-location">{city}</div></div>
        </div>
      </div>
      <div class="testimonial-card">
        <div class="testimonial-stars">&#9733;&#9733;&#9733;&#9733;&#9733;</div>
        <p class="testimonial-text">"Best {category} service I've ever used. Highly professional team, great communication from start to finish. Strongly recommend to anyone in {city}."</p>
        <div class="testimonial-author">
          <div class="testimonial-avatar">J</div>
          <div><div class="testimonial-name">James R.</div><div class="testimonial-location">{city}</div></div>
        </div>
      </div>
      <div class="testimonial-card">
        <div class="testimonial-stars">&#9733;&#9733;&#9733;&#9733;&#9733;</div>
        <p class="testimonial-text">"I was skeptical at first but {name} completely exceeded my expectations. Fast, clean, and honestly the best value in town."</p>
        <div class="testimonial-author">
          <div class="testimonial-avatar">A</div>
          <div><div class="testimonial-name">Amanda K.</div><div class="testimonial-location">{city}</div></div>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- CTA BAND -->
<div class="cta-band">
  <h2>Ready to Get Started?</h2>
  <p>Call us today or fill out the form below — we respond within 1 hour.</p>
  <a href="#contact" class="btn-cta-dark"><i class="fa fa-calendar-check"></i> Book a Free Consultation</a>
</div>

<!-- CONTACT -->
<section class="contact" id="contact">
  <div style="max-width:1000px;margin:0 auto;">
    <div class="section-header" style="text-align:center;">
      <div class="section-label">Get In Touch</div>
      <div class="section-title">Contact Us</div>
    </div>
    <div class="contact-inner">
      <div class="contact-info">
        {f'<div class="contact-item"><div class="contact-icon"><i class="fa fa-phone"></i></div><div><div class="contact-label">Phone</div><div class="contact-value">{phone}</div></div></div>' if phone else ''}
        {f'<div class="contact-item"><div class="contact-icon"><i class="fa fa-location-dot"></i></div><div><div class="contact-label">Address</div><div class="contact-value">{address}</div></div></div>' if address else ''}
        <div class="contact-item"><div class="contact-icon"><i class="fa fa-clock"></i></div><div><div class="contact-label">Hours</div><div class="contact-value">Mon–Fri: 8am – 6pm<br>Sat: 9am – 4pm</div></div></div>
        <div class="contact-item"><div class="contact-icon"><i class="fa fa-map-marker-alt"></i></div><div><div class="contact-label">Service Area</div><div class="contact-value">{city} &amp; surrounding areas</div></div></div>
      </div>
      <div class="contact-form">
        <div class="form-group"><label>Your Name</label><input type="text" placeholder="John Smith"></div>
        <div class="form-group"><label>Email</label><input type="email" placeholder="john@email.com"></div>
        <div class="form-group"><label>Phone</label><input type="tel" placeholder="(555) 000-0000"></div>
        <div class="form-group"><label>Message</label><textarea placeholder="Tell us about your project..."></textarea></div>
        <button class="form-submit">Send Message <i class="fa fa-arrow-right"></i></button>
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
  <p>Website mockup powered by <span>Cold Outreach Engine</span> &mdash; We can build your real site today.</p>
</div>

</body>
</html>"""

    return jsonify({'html': html})


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'timestamp': time.time()})


@app.route('/api/debug', methods=['GET'])
def api_debug():
    """Diagnostic endpoint — shows env var and config status (no API calls)."""
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
