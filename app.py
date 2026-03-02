import os
import re
import logging
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
from urllib.parse import urlparse
import time

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Initialize Gemini client (lazy initialization)
client = None

def get_gemini_client():
    """Lazy initialization of Gemini client"""
    global client
    if client is None:
        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            raise ValueError("GEMINI_API_KEY environment variable not set")
        genai.configure(api_key=api_key)
        client = genai.GenerativeModel('gemini-1.5-flash')
    return client

class ProspectScraper:
    """Scrapes and extracts key information from prospect URLs"""
    
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
    
    def scrape_url(self, url: str, timeout: int = 10) -> dict:
        try:
            logger.info(f"Scraping URL: {url}")
            response = requests.get(url, headers=self.headers, timeout=timeout)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            for script in soup(["script", "style", "nav", "footer"]):
                script.decompose()
            
            text = soup.get_text(separator=' ', strip=True)
            text = re.sub(r'\s+', ' ', text).strip()
            text = text[:8000]
            
            title = soup.find('title')
            title_text = title.string if title else ""
            
            meta_description = soup.find('meta', attrs={'name': 'description'})
            description = meta_description.get('content', '') if meta_description else ""
            
            og_title = soup.find('meta', property='og:title')
            og_description = soup.find('meta', property='og:description')
            
            is_linkedin = 'linkedin.com' in url.lower()
            
            headings = [h.get_text(strip=True) for h in soup.find_all(['h1', 'h2', 'h3'])]
            headings = [h for h in headings if h][:10]
            
            return {
                'url': url,
                'is_linkedin': is_linkedin,
                'title': title_text,
                'meta_description': description,
                'og_title': og_title.get('content', '') if og_title else "",
                'og_description': og_description.get('content', '') if og_description else "",
                'headings': headings,
                'content': text,
                'domain': urlparse(url).netloc
            }
            
        except requests.exceptions.Timeout:
            raise Exception("Request timeout - the website took too long to respond")
        except requests.exceptions.RequestException as e:
            raise Exception(f"Failed to fetch URL: {str(e)}")
        except Exception as e:
            raise Exception(f"Error processing URL: {str(e)}")


class OutreachGenerator:
    """Generates personalized cold outreach emails using Gemini"""
    
    def __init__(self, gemini_client):
        self.client = gemini_client
    
    def generate_email(self, prospect_data: dict, sender_context: str = None) -> dict:
        try:
            context = self._build_context(prospect_data)
            prompt = self._create_prompt(context, sender_context)
            
            logger.info("Generating personalized email with Gemini")

            system_instruction = "You are an expert cold email writer who creates highly personalized, concise outreach that references specific details from a prospect's profile or website. You write emails that feel human, authentic, and respectful of the recipient's time."

            response = self.client.generate_content(
                f"{system_instruction}\n\n{prompt}",
                generation_config=genai.GenerationConfig(
                    temperature=0.7,
                    max_output_tokens=1000,
                )
            )

            result = response.text
            parsed_email = self._parse_email_response(result)
            parsed_email['personalization_notes'] = self._extract_personalization_signals(prospect_data, parsed_email)
            
            return parsed_email
            
        except Exception as e:
            logger.error(f"Error generating email: {str(e)}")
            raise Exception(f"Failed to generate email: {str(e)}")
    
    def _build_context(self, prospect_data: dict) -> str:
        parts = []
        parts.append(f"URL: {prospect_data['url']}")
        parts.append(f"Domain: {prospect_data['domain']}")
        
        if prospect_data['is_linkedin']:
            parts.append("Source: LinkedIn Profile")
        else:
            parts.append("Source: Company Website")
        
        if prospect_data['title']:
            parts.append(f"Page Title: {prospect_data['title']}")
        if prospect_data['meta_description']:
            parts.append(f"Description: {prospect_data['meta_description']}")
        if prospect_data['og_description']:
            parts.append(f"Summary: {prospect_data['og_description']}")
        if prospect_data['headings']:
            parts.append(f"Key Topics: {', '.join(prospect_data['headings'][:5])}")
        if prospect_data['content']:
            parts.append(f"\nPage Content:\n{prospect_data['content'][:3000]}")
        
        return "\n".join(parts)
    
    def _create_prompt(self, context: str, sender_context: str = None) -> str:
        sender_info = sender_context if sender_context else """
You are reaching out on behalf of a growth consultant who helps companies scale revenue through automation and AI.
Your value proposition: Custom AI agents that automate repetitive business processes (lead gen, customer support, content creation).
"""
        
        prompt = f"""Based on the following information about a prospect, write a highly personalized cold outreach email.

PROSPECT INFORMATION:
{context}

SENDER CONTEXT:
{sender_info}

REQUIREMENTS:
1. Subject line: Short, specific, and references something from their profile/website
2. Opening: Hook them with a specific observation about their company/role/recent activity
3. Body: 2-3 short paragraphs max. Connect their pain point to your solution.
4. CTA: Clear, low-friction ask (15-min call, quick demo, etc.)
5. Tone: Professional but conversational. No hype. Respectful of their time.
6. Length: Keep the entire email under 150 words

FORMAT YOUR RESPONSE EXACTLY LIKE THIS:
SUBJECT: [your subject line]

BODY:
[your email body including greeting, hook, value prop, and CTA]

Make it feel human and authentic. Reference specific details from the prospect information to show you did your research.
"""
        return prompt
    
    def _parse_email_response(self, response: str) -> dict:
        subject_match = re.search(r'SUBJECT:\s*(.+?)(?:\n|$)', response, re.IGNORECASE)
        subject = subject_match.group(1).strip() if subject_match else "Personalized outreach"
        
        body_match = re.search(r'BODY:\s*(.+)', response, re.IGNORECASE | re.DOTALL)
        body = body_match.group(1).strip() if body_match else response
        body = body.replace('SUBJECT:', '').replace('BODY:', '').strip()
        if subject in body:
            body = body.replace(subject, '').strip()
        
        return {'subject': subject, 'body': body}
    
    def _extract_personalization_signals(self, prospect_data: dict, email: dict) -> list:
        signals = []
        email_text = f"{email['subject']} {email['body']}".lower()
        
        if prospect_data['is_linkedin']:
            signals.append("LinkedIn profile analyzed")
        else:
            signals.append(f"Company website ({prospect_data['domain']}) analyzed")
        
        if prospect_data['domain']:
            company_name = prospect_data['domain'].replace('.com', '').replace('www.', '')
            if company_name.lower() in email_text:
                signals.append("Company name referenced")
        
        for heading in prospect_data['headings'][:5]:
            if len(heading) > 10 and heading.lower() in email_text:
                signals.append(f"Referenced: '{heading}'")
        
        title_keywords = set(re.findall(r'\b\w{5,}\b', prospect_data['title'].lower()))
        email_keywords = set(re.findall(r'\b\w{5,}\b', email_text))
        common = title_keywords & email_keywords
        if common and len(common) >= 2:
            signals.append("Incorporated key topics from page")
        
        if not signals:
            signals.append("Content analysis and context matching applied")
        
        return signals


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'healthy',
        'service': 'Cold Outreach Personalization Engine',
        'timestamp': time.time()
    }), 200


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
        
        parsed = urlparse(url)
        if not parsed.netloc:
            return jsonify({'success': False, 'error': 'Invalid URL format'}), 400
        
        sender_context = data.get('sender_context')
        
        if not os.getenv('GEMINI_API_KEY'):
            return jsonify({'success': False, 'error': 'GEMINI_API_KEY environment variable not set'}), 500
        
        scraper = ProspectScraper()
        prospect_data = scraper.scrape_url(url)
        
        gemini_client = get_gemini_client()
        generator = OutreachGenerator(gemini_client)
        email_result = generator.generate_email(prospect_data, sender_context)
        
        return jsonify({
            'success': True,
            'data': email_result,
            'prospect': {
                'url': url,
                'domain': prospect_data['domain'],
                'title': prospect_data['title']
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error in generate_outreach: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.errorhandler(404)
def not_found(e):
    return jsonify({'success': False, 'error': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(e):
    return jsonify({'success': False, 'error': 'Internal server error'}), 500


if __name__ == '__main__':
    if not os.getenv('GEMINI_API_KEY'):
        logger.warning("GEMINI_API_KEY not set - API calls will fail")
    
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_ENV') == 'development'
    
    logger.info(f"Starting Cold Outreach Engine on port {port}")
    app.run(host='0.0.0.0', port=port, debug=debug)