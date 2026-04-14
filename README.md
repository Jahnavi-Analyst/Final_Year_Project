# InformaX AI

InformaX AI is a Flask-based intelligent news analytics platform that combines live news collection, trusted-source browsing, exact-date filtering, AI-assisted summarization, credibility support labels, sentiment insights, user personalization, and admin analytics in one application.

It is designed as a final-year project, but it is also structured so it can be deployed online and used from anywhere through a public link.

## Highlights

- live news dashboard with date filtering
- trusted-source browsing
- AI quick summaries
- `REAL`, `FAKE`, and `CHECK` credibility support labels with simple reasons
- headline sentiment and public mood insights
- signup, login, OTP-based password reset, profile, and settings
- saved articles and reading/activity tracking
- admin dashboard with analytics, alerts, and system health
- responsive layout for desktop and mobile
- dark mode and system theme support

## Project Structure

```text
InformaxAI/
|- app.py
|- db.py
|- requirements.txt
|- Procfile
|- render.yaml
|- wsgi.py
|- .env
|- README.md
|- dataset/
|- model/
|- static/
|  |- css/
|  |- js/
|  `- images/
`- templates/
```

## Tech Stack

### Backend

- Python
- Flask
- SQLite

### AI / NLP / ML

- Scikit-learn
- TextBlob
- VADER Sentiment

### News / Parsing

- NewsAPI
- RSS feeds
- BeautifulSoup

### Frontend

- HTML
- CSS
- JavaScript

## Main Features

### User Features

- home dashboard with latest news
- exact-date filtering for older news
- category browsing
- trusted-source pages
- AI-generated quick summaries
- credibility support with reasons
- sentiment and mood indicators
- save and unsave articles per user
- profile, settings, and password management

### Admin Features

- admin-only analytics dashboard
- activity and engagement monitoring
- top searches and reading-time insights
- alert panel and system health overview
- user dataset view

## Local Setup

### 1. Clone the repository

```bash
git clone <your-repository-url>
cd InformaxAI
```

### 2. Create a virtual environment

Windows:

```bash
python -m venv venv
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Create `.env`

Copy `.env.example` and fill in your real values.

Example:

```env
NEWSAPI_KEY=your_real_newsapi_key
FLASK_SECRET_KEY=replace_with_a_secure_secret_key
APP_TIMEZONE=Asia/Kolkata
ADMIN_EMAIL=admin@example.com
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your_email@example.com
SMTP_PASSWORD=your_email_app_password
SMTP_FROM_EMAIL=your_email@example.com
```

### 5. Run the app

```bash
python app.py
```

Then open:

```text
http://127.0.0.1:5000
```

## Important Files

- `app.py`
  Main Flask application with routes, news processing, credibility logic, sentiment logic, authentication, and admin dashboard logic.

- `db.py`
  SQLite helper functions for users, saved articles, OTP records, and activity logs.

- `model/`
  Contains the trained classifier and vectorizer used for credibility support.

- `templates/`
  HTML templates for dashboard, auth pages, saved page, source pages, profile, settings, and admin dashboard.

- `static/css/style.css`
  Main styling file, including responsive layout and dark mode support.

- `render.yaml`
  Render Blueprint config for deployment.

- `Procfile`
  Gunicorn start command for production hosting.

## Deployment Readiness

This project is ready to deploy for demo and showcase use.

It now includes:

- `wsgi.py`
- `Procfile`
- `render.yaml`
- safe `.env.example` placeholders
- Render-ready Gunicorn port binding

## Deploy on Render

### What users will open

After deployment, users will get a public link such as:

```text
https://informax-ai.onrender.com
```

If that exact service name is already taken, Render will generate a similar `onrender.com` URL.

### Step-by-step Render deployment

1. Push this project to GitHub.
2. Create a [Render](https://render.com/) account and log in.
3. In Render, click `New` -> `Blueprint`.
4. Connect the GitHub repository that contains this project.
5. Select the branch to deploy.
6. Render will detect the root `render.yaml`.
7. Fill the required environment variables:
   - `NEWSAPI_KEY`
   - `FLASK_SECRET_KEY`
   - `ADMIN_EMAIL`
   - `SMTP_SERVER`
   - `SMTP_PORT`
   - `SMTP_USERNAME`
   - `SMTP_PASSWORD`
   - `SMTP_FROM_EMAIL`
8. Click `Deploy Blueprint`.
9. Wait for the build and deploy to complete.
10. Open the public Render URL and test the app.

### Recommended Render settings

- for testing only: Free plan
- for real users: Starter plan or higher

### Important note about database storage

This app currently uses SQLite through `db.py`, which stores data in `saved.db`.

If you deploy without persistent storage:

- saved users and activity data can be lost on redeploy or restart
- free instances can sleep when idle

For safer deployment on Render:

1. use a `Starter` web service if possible
2. add a Persistent Disk in Render
3. later, consider moving to Postgres for full production use

## Render Files Included

### `render.yaml`

Uses:

- build command: `pip install -r requirements.txt`
- start command: `gunicorn --bind 0.0.0.0:$PORT wsgi:app`

### `Procfile`

Uses:

- `web: gunicorn --bind 0.0.0.0:$PORT wsgi:app`

## Testing Checklist

Before or after deployment, test these flows:

- signup
- login
- remember me
- forgot password OTP
- update password
- latest news loading
- previous-date news loading
- source pages opening correctly
- credibility labels and reasons displaying correctly
- save and unsave article
- profile and settings pages
- admin dashboard access
- theme switching
- dark mode visibility
- mobile responsiveness

## Known Notes

- some original publisher pages may require login or subscription
- the app does not bypass paywalls
- summary quality depends on the available article text
- `REAL`, `FAKE`, and `CHECK` are decision-support labels, not absolute truth guarantees
- Render Free services can sleep after inactivity

## Recommended Project / Viva Explanation

You can explain the project like this:

> InformaX AI is a real-time intelligent news analytics platform. It collects live news, allows filtering by date, category, and trusted source, and enriches each article with AI summaries, credibility support labels, and sentiment analysis. It also includes user personalization and an admin dashboard for analytics and monitoring.

## Final Note

For a college project, portfolio, or demo, this app is in a good state to deploy.

For long-term public use, the next recommended improvement is moving from SQLite to a production database such as Postgres.
