import requests

API_KEY = "8377c27efe1b4f00adc2df8fdef408a5"

url = f"https://newsapi.org/v2/everything?q=technology&apiKey={API_KEY}"

response = requests.get(url)
data = response.json()

for article in data["articles"][:5]:
    print("Title:", article["title"])
    print("Description:", article["description"])
    print("-" * 50)