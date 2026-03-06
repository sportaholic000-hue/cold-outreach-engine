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
