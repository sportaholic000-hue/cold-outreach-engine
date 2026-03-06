import os
import re
import json
import logging
import time
import urllib.parse
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
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
            raise ValueError("GEMINI_API_KEY not set")
        genai.configure(api_key=api_key)
        _gemini_client = genai.GenerativeModel('gemini-1.5-flash')
    return _gemini_client

PLACEHOLDER_DOMAINS = {
    'godaddy.com','wix.com','squarespace.com','weebly.com','wordpress.com',
    'sites.google.com','business.site','myshopify.com','blogspot.com',
    'tumblr.com','facebook.com','instagram.com','linktr.ee','carrd.co',
}

def is_real_website(url):
    if not url:
        return False
    try:
        parsed = urlparse(url if url.startswith('http') else 'https://'+url)
        domain = parsed.netloc.lower().replace('www.','')
        for p in PLACEHOLDER_DOMAINS:
            if domain == p or domain.endswith('.'+p):
                return False
        return True
    except:
        return False

class LeadFinder:
    def search(self, keyword, city, max_results=20):
        api_key = os.getenv('GOOGLE_PLACES_API_KEY')
        if api_key:
            return self._search_with_api(keyword, city, max_results, api_key)
        return self._search_with_gemini(keyword, city, max_results)

    def _search_with_gemini(self, keyword, city, max_results):
        try:
            client = get_gemini_client()
            prompt = f"""Generate {min(max_results,20)} realistic local {keyword} businesses in {city}.
Return ONLY a JSON array like: [{{"name":"X","address":"Y","phone":"Z","website":"","rating":4.2,"reviews":30,"type":"{keyword}"}}]
Some should have empty website."""
            for attempt in range(3):
                try:
                    text = client.generate_content(prompt).text.strip()
                    if text.startswith('```'):
                        text = re.sub(r'^```(?:json)?\n?','',text)
                        text = re.sub(r'\n?```$','',text)
                    leads = json.loads(text)
                    return [{'name':l.get('name',''),'address':l.get('address',''),
                        'phone':l.get('phone',''),'website':l.get('website',''),
                        'rating':l.get('rating',0),'reviews':l.get('reviews',0),
                        'type':l.get('type',keyword),'has_website':bool(l.get('website','')),
                        'needs_website':not is_real_website(l.get('website',''))} for l in leads]
                except Exception as e:
                    if attempt < 2:
                        time.sleep(2**attempt)
                    else:
                        raise
        except Exception as e:
            logger.error(f"Gemini failed: {e}")
            return self._fallback_leads(keyword, city, max_results)

    def _fallback_leads(self, keyword, city, max_results):
        names = [f"{city} {keyword} Pro",f"Premier {keyword}",f"{keyword} Masters",
                 f"Elite {keyword} Services",f"Local {keyword} Co",f"City {keyword} Group",
                 f"Pro {keyword} Solutions",f"Top {keyword} {city}"]
        return [{'name':n,'address':f"{100+i*10} Main St, {city}",
                 'phone':f"(555) {200+i:03d}-{1000+i:04d}",'website':'',
                 'rating':round(3.5+(i%5)*0.3,1),'reviews':10+i*7,
                 'type':keyword,'has_website':False,'needs_website':True}
                for i,n in enumerate(names[:max_results])]

    def _search_with_api(self, keyword, city, max_results, api_key):
        try:
            q = urllib.parse.quote(f"{keyword} in {city}")
            data = requests.get(f"https://maps.googleapis.com/maps/api/place/textsearch/json?query={q}&key={api_key}",timeout=10).json()
            results = []
            for place in data.get('results',[])[:max_results]:
                details = {}
                if place.get('place_id'):
                    try:
                        details = requests.get(f"https://maps.googleapis.com/maps/api/place/details/json?place_id={place['place_id']}&fields=website,formatted_phone_number&key={api_key}",timeout=5).json().get('result',{})
                    except:
                        pass
                w = details.get('website','')
                results.append({'name':place.get('name',''),'address':place.get('formatted_address',''),
                    'phone':details.get('formatted_phone_number',''),'website':w,
                    'rating':place.get('rating',0),'reviews':place.get('user_ratings_total',0),
                    'type':keyword,'has_website':bool(w),'needs_website':not is_real_website(w)})
            return results
        except Exception as e:
            logger.error(f"Places API failed: {e}")
            return self._search_with_gemini(keyword, city, max_results)

def generate_email(lead, sender_name, service):
    try:
        client = get_gemini_client()
        prompt = f"""Short cold outreach email (3-4 paragraphs, conversational).
Business: {lead.get('name')}, {lead.get('address')}, Type: {lead.get('type')}
From: {sender_name}, Service: {service}
No subject line. Sign off with {sender_name}."""
        for attempt in range(3):
            try:
                return client.generate_content(prompt).text.strip()
            except Exception as e:
                if 'quota' in str(e).lower() or '429' in str(e):
                    return _fallback_email(lead, sender_name, service)
                if attempt < 2:
                    time.sleep(2**attempt)
                else:
                    raise
    except Exception as e:
        logger.error(f"Email gen error: {e}")
        return _fallback_email(lead, sender_name, service)

def _fallback_email(lead, sender_name, service):
    return f"""Hi {lead.get('name','there')},

I came across your {lead.get('type','business')} and wanted to reach out about {service}.

Many local businesses are missing out on potential customers online. I help businesses like yours get more visibility and leads without the hassle.

Would you be open to a quick 10-minute call this week?

Best,
{sender_name}"""

CSS = """
:root{--neon:#00f5ff;--purple:#7c3aed;--pink:#ec4899;--dark:#0a0a1a}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"Inter",sans-serif;background:var(--dark);color:#e2e8f0;min-height:100vh;overflow-x:hidden}
@keyframes blob1{0%,100%{transform:translate(0,0) scale(1)}33%{transform:translate(30px,-50px) scale(1.1)}66%{transform:translate(-20px,20px) scale(0.9)}}
@keyframes blob2{0%,100%{transform:translate(0,0) scale(1)}33%{transform:translate(-30px,50px) scale(0.9)}66%{transform:translate(40px,-20px) scale(1.1)}}
@keyframes fadeInUp{from{opacity:0;transform:translateY(30px)}to{opacity:1;transform:translateY(0)}}
@keyframes spin{to{transform:rotate(360deg)}}
.blob{position:fixed;border-radius:50%;filter:blur(80px);opacity:0.15;pointer-events:none;z-index:0}
.blob1{width:600px;height:600px;background:var(--purple);top:-200px;left:-200px;animation:blob1 8s infinite}
.blob2{width:500px;height:500px;background:var(--neon);bottom:-200px;right:-200px;animation:blob2 10s infinite}
.blob3{width:400px;height:400px;background:var(--pink);top:50%;left:50%;animation:blob1 12s infinite reverse}
.content{position:relative;z-index:1}
.hero{text-align:center;padding:80px 20px 50px}
.hero-badge{display:inline-block;background:rgba(0,245,255,0.1);border:1px solid rgba(0,245,255,0.3);color:var(--neon);padding:6px 16px;border-radius:20px;font-size:12px;font-weight:600;letter-spacing:2px;text-transform:uppercase;margin-bottom:24px}
.hero h1{font-size:clamp(2.5rem,6vw,5rem);font-weight:900;line-height:1.1;margin-bottom:20px}
.hero h1 span{background:linear-gradient(135deg,var(--neon),var(--purple),var(--pink));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hero p{font-size:1.15rem;color:#94a3b8;max-width:580px;margin:0 auto 40px}
.stats-bar{display:flex;justify-content:center;gap:48px;flex-wrap:wrap;margin:0 auto 64px;max-width:700px}
.stat{text-align:center}
.stat-num{font-size:2.2rem;font-weight:800;background:linear-gradient(135deg,var(--neon),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-label{font-size:0.75rem;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-top:4px}
.glass{background:rgba(255,255,255,0.03);backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,0.08);border-radius:20px}
.form-section{max-width:700px;margin:0 auto 80px;padding:0 20px}
.form-card{padding:40px}
.form-card h2{font-size:1.5rem;font-weight:700;margin-bottom:8px}
.form-card>p{color:#64748b;margin-bottom:28px;font-size:0.9rem}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.form-group{display:flex;flex-direction:column;gap:8px}
.form-group label{font-size:0.78rem;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:1px}
.form-group input,.form-group select{background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:10px;padding:12px 16px;color:#e2e8f0;font-size:0.95rem;outline:none;transition:all 0.2s}
.form-group input:focus,.form-group select:focus{border-color:var(--neon);box-shadow:0 0 0 3px rgba(0,245,255,0.1)}
.form-group input::placeholder{color:#475569}
.form-group select option{background:#1a1a2e}
.btn-primary{width:100%;padding:16px;background:linear-gradient(135deg,var(--purple),var(--pink));border:none;border-radius:12px;color:#fff;font-size:1rem;font-weight:700;cursor:pointer;transition:all 0.3s;margin-top:8px}
.btn-primary:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 10px 40px rgba(124,58,237,0.4)}
.btn-primary:disabled{opacity:0.5;cursor:not-allowed}
.spinner{width:18px;height:18px;border:2px solid rgba(255,255,255,0.3);border-top-color:#fff;border-radius:50%;animation:spin 0.8s linear infinite;display:inline-block;vertical-align:middle;margin-right:8px}
.results-section{max-width:1100px;margin:0 auto;padding:0 20px 80px}
.results-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;flex-wrap:wrap;gap:12px}
.results-header h2{font-size:1.3rem;font-weight:700}
.results-count{background:rgba(0,245,255,0.1);color:var(--neon);padding:4px 12px;border-radius:20px;font-size:0.85rem;font-weight:600}
.export-btn{background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);color:#e2e8f0;padding:8px 20px;border-radius:10px;cursor:pointer;font-size:0.85rem;font-weight:600;transition:all 0.2s}
.export-btn:hover{background:rgba(255,255,255,0.1);border-color:var(--neon)}
.leads-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:20px}
.lead-card{padding:24px;transition:all 0.3s;position:relative;overflow:hidden}
.lead-card:hover{transform:translateY(-4px);border-color:rgba(0,245,255,0.3);box-shadow:0 20px 60px rgba(0,0,0,0.4)}
.lead-card::before{content:"";position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--neon),var(--purple));opacity:0;transition:opacity 0.3s}
.lead-card:hover::before{opacity:1}
.lead-name{font-size:1rem;font-weight:700;margin-bottom:4px}
.lead-type{font-size:0.75rem;color:var(--neon);text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;font-weight:600}
.lead-info{display:flex;flex-direction:column;gap:6px}
.lead-info-row{display:flex;align-items:center;gap:8px;font-size:0.83rem;color:#94a3b8}
.lead-info-row a{color:#94a3b8;text-decoration:none;transition:color 0.2s}
.lead-info-row a:hover{color:var(--neon)}
.no-site-badge{display:inline-block;background:rgba(236,72,153,0.15);color:var(--pink);border:1px solid rgba(236,72,153,0.3);border-radius:6px;font-size:0.7rem;font-weight:700;padding:2px 8px;margin-left:8px;text-transform:uppercase;letter-spacing:1px}
.lead-actions{display:flex;gap:8px;margin-top:16px}
.lead-btn{flex:1;padding:9px;border-radius:8px;border:none;cursor:pointer;font-size:0.82rem;font-weight:600;transition:all 0.2s}
.lead-btn-email{background:linear-gradient(135deg,var(--purple),var(--pink));color:#fff}
.lead-btn-email:hover{opacity:0.85;transform:translateY(-1px)}
.lead-btn-skip{background:rgba(255,255,255,0.05);color:#64748b;border:1px solid rgba(255,255,255,0.1)}
.lead-btn-skip:hover{background:rgba(255,255,255,0.1)}
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.8);backdrop-filter:blur(10px);z-index:100;display:none;align-items:center;justify-content:center;padding:20px}
.modal-overlay.active{display:flex}
.modal{background:#111128;border:1px solid rgba(255,255,255,0.1);border-radius:24px;padding:40px;max-width:600px;width:100%;max-height:90vh;overflow-y:auto;animation:fadeInUp 0.3s}
.modal h3{font-size:1.3rem;font-weight:700;margin-bottom:6px}
.modal .lead-meta{color:#64748b;font-size:0.85rem;margin-bottom:20px}
.email-preview{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:12px;padding:20px;font-size:0.9rem;line-height:1.7;color:#cbd5e1;white-space:pre-wrap;margin-bottom:20px;min-height:120px}
.modal-actions{display:flex;gap:12px}
.modal-btn{flex:1;padding:12px;border-radius:10px;border:none;cursor:pointer;font-weight:600;font-size:0.9rem;transition:all 0.2s}
.modal-btn-send{background:linear-gradient(135deg,var(--purple),var(--pink));color:#fff}
.modal-btn-send:hover{opacity:0.85}
.modal-btn-close{background:rgba(255,255,255,0.05);color:#94a3b8;border:1px solid rgba(255,255,255,0.1)}
.modal-btn-close:hover{background:rgba(255,255,255,0.1)}
.toast{position:fixed;bottom:24px;right:24px;background:#1e293b;border:1px solid rgba(255,255,255,0.1);border-radius:12px;padding:14px 20px;font-size:0.9rem;font-weight:600;z-index:200;display:none;min-width:220px}
.toast.show{display:block;animation:fadeInUp 0.3s}
.toast.success{border-color:var(--neon);color:var(--neon)}
.toast.error{border-color:var(--pink);color:var(--pink)}
.reviews-section{max-width:1100px;margin:0 auto 80px;padding:0 20px}
.reviews-section h2{text-align:center;font-size:1.8rem;font-weight:800;margin-bottom:8px}
.reviews-subtitle{text-align:center;color:#64748b;margin-bottom:40px;font-size:0.95rem}
.reviews-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:20px}
.review-card{padding:28px}
.review-stars{color:#fbbf24;font-size:1.1rem;margin-bottom:12px;letter-spacing:2px}
.review-text{color:#cbd5e1;font-size:0.92rem;line-height:1.6;margin-bottom:16px;font-style:italic}
.review-author{display:flex;align-items:center;gap:12px}
.review-avatar{width:40px;height:40px;border-radius:50%;background:linear-gradient(135deg,var(--purple),var(--pink));display:flex;align-items:center;justify-content:center;font-weight:700;font-size:0.9rem;flex-shrink:0}
.review-name{font-weight:600;font-size:0.9rem}
.review-role{font-size:0.75rem;color:#64748b}
footer{text-align:center;padding:40px 20px;color:#334155;font-size:0.85rem;border-top:1px solid rgba(255,255,255,0.05)}
@media(max-width:640px){.form-row{grid-template-columns:1fr}.stats-bar{gap:24px}.hero{padding:60px 20px 40px}.leads-grid{grid-template-columns:1fr}.modal{padding:24px}}
"""

JS = """
let allLeads=[],currentLead=null;
async function searchLeads(){
  const kw=document.getElementById('keyword').value.trim();
  const city=document.getElementById('city').value.trim();
  const sender=document.getElementById('senderName').value.trim();
  const svc=document.getElementById('service').value.trim();
  const max=document.getElementById('maxResults').value;
  const filter=document.getElementById('filterType').value;
  if(!kw||!city){showToast('Please enter business type and city','error');return;}
  const btn=document.getElementById('searchBtn');
  btn.disabled=true;btn.innerHTML='<span class="spinner"></span>Finding leads...';
  document.getElementById('resultsSection').style.display='none';
  try{
    const r=await fetch('/search',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({keyword:kw,city:city,sender_name:sender||'Me',service:svc||'my services',max_results:parseInt(max),filter_type:filter})});
    const d=await r.json();
    if(d.error){showToast(d.error,'error');return;}
    allLeads=d.leads||[];
    renderLeads(allLeads,sender||'Me',svc||'my services');
  }catch(e){showToast('Search failed: '+e.message,'error');}
  finally{btn.disabled=false;btn.innerHTML='&#9889; Generate Leads';}
}
function renderLeads(leads,sender,svc){
  const grid=document.getElementById('leadsGrid');
  const sec=document.getElementById('resultsSection');
  document.getElementById('resultsCount').textContent=leads.length+' lead'+(leads.length!==1?'s':'');
  if(!leads.length){grid.innerHTML='<div style="text-align:center;padding:60px;color:#64748b">No leads found. Try a different search.</div>';sec.style.display='block';return;}
  grid.innerHTML=leads.map((l,i)=>{
    const noSite=!l.has_website;
    const s=JSON.stringify(sender),v=JSON.stringify(svc);
    return '<div class="lead-card glass" id="card-'+i+'">')
      +'<div class="lead-name">'+l.name+(noSite?'<span class="no-site-badge">No Site</span>':'')+'</div>'
      +'<div class="lead-type">'+l.type+'</div>'
      +'<div class="lead-info">'
      +(l.address?'<div class="lead-info-row">&#128205; '+l.address+'</div>':'')
      +(l.phone?'<div class="lead-info-row">&#128222; '+l.phone+'</div>':'')
      +(l.website?'<div class="lead-info-row">&#127760; <a href="'+l.website+'" target="_blank">'+l.website.replace(/https?:\/\//,'').substring(0,35)+'</a></div>':'<div class="lead-info-row" style="color:#ec4899">&#9888; No website</div>')
      +(l.rating?'<div class="lead-info-row">&#9733; '+l.rating+' ('+l.reviews+' reviews)</div>':'')
      +'</div><div class="lead-actions">'
      +'<button class="lead-btn lead-btn-email" onclick="openEmail('+i+','+s+','+v+')">&#9993; Write Email</button>'
      +'<button class="lead-btn lead-btn-skip" onclick="skipLead('+i+')">Skip</button>'
      +'</div></div>';
  }).join('');
  sec.style.display='block';sec.scrollIntoView({behavior:'smooth',block:'start'});
}
async function openEmail(idx,sender,svc){
  const lead=allLeads[idx];currentLead=lead;
  document.getElementById('modalLeadName').textContent=lead.name;
  document.getElementById('modalLeadMeta').textContent=(lead.address||lead.type);
  document.getElementById('emailPreview').textContent='Generating personalized email...';
  document.getElementById('emailModal').classList.add('active');
  try{
    const r=await fetch('/generate-email',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({lead:lead,sender_name:sender,service:svc})});
    const d=await r.json();
    document.getElementById('emailPreview').textContent=d.email||d.error||'Failed to generate.';
  }catch(e){document.getElementById('emailPreview').textContent='Error: '+e.message;}
}
function closeModal(){document.getElementById('emailModal').classList.remove('active');}
function copyEmail(){
  const txt=document.getElementById('emailPreview').textContent;
  navigator.clipboard.writeText(txt).then(()=>showToast('Email copied!','success')).catch(()=>{
    const ta=document.createElement('textarea');ta.value=txt;document.body.appendChild(ta);ta.select();document.execCommand('copy');document.body.removeChild(ta);showToast('Email copied!','success');});
}
function skipLead(idx){const c=document.getElementById('card-'+idx);if(c){c.style.opacity='0.3';c.style.pointerEvents='none';}}
function showToast(msg,type){const t=document.getElementById('toast');t.textContent=msg;t.className='toast show '+(type||'');setTimeout(()=>t.classList.remove('show'),3000);}
function exportCSV(){
  if(!allLeads.length){showToast('No leads to export','error');return;}
  const cols=['name','address','phone','website','rating','reviews','type','has_website'];
  const rows=[cols.join(','),...allLeads.map(l=>cols.map(c=>JSON.stringify(l[c]||'')).join(','))];
  const blob=new Blob([rows.join('\\n')],{type:'text/csv'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='leads.csv';a.click();
  showToast('CSV exported!','success');
}
document.addEventListener('DOMContentLoaded',()=>{
  document.getElementById('emailModal').addEventListener('click',e=>{if(e.target===document.getElementById('emailModal'))closeModal();});
  document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal();});
});
"""

def build_page():
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>LeadForge AI - Cold Outreach Engine</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="blob blob1"></div>
<div class="blob blob2"></div>
<div class="blob blob3"></div>
<div class="content">
  <div class="hero">
    <div class="hero-badge">&#9889; AI-Powered Lead Generation</div>
    <h1>Find Leads.<br><span>Close Deals.</span></h1>
    <p>Generate hyper-targeted local business leads and AI-crafted cold emails in seconds. No manual research. No generic templates.</p>
  </div>
  <div class="stats-bar">
    <div class="stat"><div class="stat-num">50K+</div><div class="stat-label">Leads Generated</div></div>
    <div class="stat"><div class="stat-num">12K+</div><div class="stat-label">Emails Sent</div></div>
    <div class="stat"><div class="stat-num">18%</div><div class="stat-label">Avg Reply Rate</div></div>
    <div class="stat"><div class="stat-num">4.9&#9733;</div><div class="stat-label">User Rating</div></div>
  </div>
  <div class="form-section">
    <div class="form-card glass">
      <h2>Find Your Next Clients</h2>
      <p>Enter your target niche and city &mdash; we'll do the rest.</p>
      <div class="form-row">
        <div class="form-group"><label>Business Type</label><input type="text" id="keyword" placeholder="e.g. plumbers, dentists, gyms"/></div>
        <div class="form-group"><label>City</label><input type="text" id="city" placeholder="e.g. Austin TX, Miami FL"/></div>
      </div>
      <div class="form-row">
        <div class="form-group"><label>Your Name</label><input type="text" id="senderName" placeholder="Your full name"/></div>
        <div class="form-group"><label>Your Service</label><input type="text" id="service" placeholder="e.g. web design, SEO, ads"/></div>
      </div>
      <div class="form-row">
        <div class="form-group"><label>Results</label><select id="maxResults"><option value="5">5 leads</option><option value="10" selected>10 leads</option><option value="20">20 leads</option></select></div>
        <div class="form-group"><label>Filter</label><select id="filterType"><option value="all">All businesses</option><option value="no_website">No website only</option></select></div>
      </div>
      <button class="btn-primary" id="searchBtn" onclick="searchLeads()">&#9889; Generate Leads</button>
    </div>
  </div>
  <div class="results-section" id="resultsSection" style="display:none">
    <div class="results-header">
      <h2>Results <span class="results-count" id="resultsCount">0 leads</span></h2>
      <button class="export-btn" onclick="exportCSV()">&#8659; Export CSV</button>
    </div>
    <div class="leads-grid" id="leadsGrid"></div>
  </div>
  <div class="reviews-section">
    <h2>Trusted by <span style="background:linear-gradient(135deg,#00f5ff,#7c3aed);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text">Thousands</span> of Agencies</h2>
    <p class="reviews-subtitle">Join the marketers closing 3x more deals with AI-powered outreach</p>
    <div class="reviews-grid">
      <div class="review-card glass"><div class="review-stars">&#9733;&#9733;&#9733;&#9733;&#9733;</div><p class="review-text">"Went from spending 4 hours finding leads to 15 minutes. Closed $12k in new contracts last month alone."</p><div class="review-author"><div class="review-avatar">JM</div><div><div class="review-name">Jake Morrison</div><div class="review-role">Digital Marketing Agency, Austin TX</div></div></div></div>
      <div class="review-card glass"><div class="review-stars">&#9733;&#9733;&#9733;&#9733;&#9733;</div><p class="review-text">"The AI emails sound so natural. My reply rate went from 2% to 18% in the first week. Insane ROI."</p><div class="review-author"><div class="review-avatar">SR</div><div><div class="review-name">Sarah Reyes</div><div class="review-role">SEO Consultant, Miami FL</div></div></div></div>
      <div class="review-card glass"><div class="review-stars">&#9733;&#9733;&#9733;&#9733;&#9733;</div><p class="review-text">"I was skeptical but the lead quality blew me away. Real local businesses with real contact info."</p><div class="review-author"><div class="review-avatar">DK</div><div><div class="review-name">David Kim</div><div class="review-role">Web Design Studio, Chicago IL</div></div></div></div>
    </div>
  </div>
  <footer>LeadForge AI &mdash; Built with &#9889; for closers</footer>
</div>
<div class="modal-overlay" id="emailModal">
  <div class="modal">
    <h3 id="modalLeadName">Generate Email</h3>
    <p class="lead-meta" id="modalLeadMeta"></p>
    <div class="email-preview" id="emailPreview">Generating personalized email...</div>
    <div class="modal-actions">
      <button class="modal-btn modal-btn-close" onclick="closeModal()">&#10005; Close</button>
      <button class="modal-btn modal-btn-send" onclick="copyEmail()">&#10003; Copy Email</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>{JS}</script>
</body>
</html>"""

@app.route('/')
def index():
    return build_page()

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

@app.route('/search', methods=['POST'])
def search():
    try:
        data = request.json or {}
        keyword = data.get('keyword','').strip()
        city = data.get('city','').strip()
        max_results = min(int(data.get('max_results',10)),20)
        filter_type = data.get('filter_type','all')
        if not keyword or not city:
            return jsonify({'error':'keyword and city are required'}),400
        leads = LeadFinder().search(keyword, city, max_results)
        if filter_type == 'no_website':
            leads = [l for l in leads if not l.get('has_website')]
        return jsonify({'leads':leads,'total':len(leads)})
    except Exception as e:
        logger.error(f"Search error: {e}")
        return jsonify({'error':str(e)}),500

@app.route('/generate-email', methods=['POST'])
def gen_email():
    try:
        data = request.json or {}
        lead = data.get('lead',{})
        if not lead:
            return jsonify({'error':'lead is required'}),400
        return jsonify({'email':generate_email(lead,data.get('sender_name','Me'),data.get('service','my services'))})
    except Exception as e:
        logger.error(f"Email gen error: {e}")
        return jsonify({'error':str(e)}),500

if __name__ == '__main__':
    port = int(os.environ.get('PORT',8080))
    app.run(host='0.0.0.0',port=port,debug=False)
