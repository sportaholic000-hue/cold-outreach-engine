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


    html += '<style>'
    html += ':root{--neon:#00f5ff;--purple:#7c3aed;--pink:#ec4899;--dark:#0a0a1a;--card:#111128}'
    html += '*{margin:0;padding:0;box-sizing:border-box}'
    html += 'body{font-family:"Inter",sans-serif;background:var(--dark);color:#e2e8f0;min-height:100vh;overflow-x:hidden}'
    html += '@keyframes blob1{0%,100%{transform:translate(0,0) scale(1)}33%{transform:translate(30px,-50px) scale(1.1)}66%{transform:translate(-20px,20px) scale(0.9)}}'
    html += '@keyframes blob2{0%,100%{transform:translate(0,0) scale(1)}33%{transform:translate(-30px,50px) scale(0.9)}66%{transform:translate(40px,-20px) scale(1.1)}}'
    html += '@keyframes fadeInUp{from{opacity:0;transform:translateY(30px)}to{opacity:1;transform:translateY(0)}}'
    html += '@keyframes spin{to{transform:rotate(360deg)}}'
    html += '</style></head><body>'