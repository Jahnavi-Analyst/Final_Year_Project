# InformaX AI

InformaX AI is a Flask-based intelligent news analytics platform that fetches live news, filters it by date and source, and enriches it with AI-assisted summaries, credibility tags, sentiment analysis, and topic insights.

## Features

- Live news dashboard with category-based browsing
- Exact-date news filtering using the calendar
- Trusted-source newspaper pages with daily source-wise news
- AI-generated quick summaries
- Credibility labels: `REAL`, `FAKE`, `CHECK`
- Headline sentiment and public mood analysis
- Breaking-news detection and trending-topic extraction
- Save and manage articles locally with SQLite
- Secure login, signup, and logout
- OTP-based forgot-password flow via email
- Admin dashboard for users, activity, and reset requests

## Tech Stack

- Python
- Flask
- SQLite
- Scikit-learn
- TextBlob
- VADER Sentiment
- Feedparser
- NewsAPI

## Project Structure

```text
InformaxAI/
|- app.py
|- db.py
|- train_model.py
|- requirements.txt
|- .env.example
|- dataset/
|- model/
|- static/
`- templates/
```

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create a `.env` file in the project root using `.env.example`.
4. Add your NewsAPI key to the `.env` file.

Example `.env`:

```env
NEWSAPI_KEY=your_newsapi_key_here
FLASK_SECRET_KEY=replace_with_a_secure_secret_key
APP_TIMEZONE=Asia/Kolkata
ADMIN_EMAIL=your_admin_email@example.com
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your_email@example.com
SMTP_PASSWORD=your_app_password
SMTP_FROM_EMAIL=your_email@example.com
```

## Run The App

```bash
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

## Deployment

This project now includes:

- `wsgi.py`
- `Procfile`
- `render.yaml`

You can deploy it on Render or another Python host after setting the required environment variables there.

## Fake-News Classification Note

The `REAL / FAKE / CHECK` label is an AI-assisted credibility estimate based on a trained machine learning model plus source/content heuristics. It should be treated as a support feature, not absolute truth.

## Main Files

- `app.py`: main Flask app and all dashboard logic
- `db.py`: saved-article database helpers
- `train_model.py`: fake-news model training script
- `templates/`: frontend HTML files
- `static/`: CSS, JS, and images
- `model/`: saved ML model files

## Submission Tip

For mentor/demo presentation, explain that the project combines:

- real-time news collection
- AI-assisted summarization
- credibility estimation
- sentiment analysis
- trusted-source filtering
- exact-date news retrieval

This makes it both a news aggregation system and an analytics platform.
