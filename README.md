# AI Cold Outreach Personalization Engine

A production-ready Flask API that generates hyper-personalized cold outreach emails using AI. Simply provide a prospect's LinkedIn profile or company website URL, and the engine scrapes key information, analyzes it with Google Gemini 1.5 Flash, and generates a personalized cold email with subject line, body, and CTA.

## Features

- **Smart Web Scraping**: Extracts prospect information from LinkedIn profiles and company websites
- **AI-Powered Personalization**: Uses Google Gemini 1.5 Flash to craft authentic, personalized emails
- **Clean API**: RESTful JSON API with proper error handling
- **Beautiful Frontend**: Single-page interface for non-technical users
- **Production Ready**: Includes health checks, logging, CORS support, and error handling
- **Flexible Context**: Customize your value proposition per request

## Quick Deploy to Render

1. Fork this repo
2. Go to [render.com](https://render.com) and create a new Web Service
3. Connect your GitHub repo
4. Add environment variable: `GEMINI_API_KEY=your-key`
5. Deploy — live URL in ~3 minutes

## API

### POST /api/generate
```json
{
  "url": "https://linkedin.com/in/john-doe",
  "sender_context": "Optional: describe your offer"
}
```

### GET /health
Health check endpoint.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| GEMINI_API_KEY | Yes | Get free at aistudio.google.com/app/apikey |
| PORT | No | Default 8080 |
| FLASK_ENV | No | Set to production for deploy |