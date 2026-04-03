import pandas as pd
import joblib
import os
from sklearn.model_selection import train_test_split
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

# Load datasets
fake = pd.read_csv("../dataset/Fake.csv")
real = pd.read_csv("../dataset/True.csv")

fake["label"] = 0
real["label"] = 1

# Balance dataset
real = real.sample(n=len(fake), random_state=42)

df = pd.concat([fake, real])
df = df.sample(frac=1, random_state=42)

# Combine title + text
df["content"] = df["title"] + " " + df["text"]

X = df["content"]
y = df["label"]

# Vectorization
vectorizer = TfidfVectorizer(max_features=5000, stop_words="english")
X_vec = vectorizer.fit_transform(X)

# Train model
model = LogisticRegression()
model.fit(X_vec, y)

# Save model
joblib.dump(model, "fake_news_model.pkl")
joblib.dump(vectorizer, "vectorizer.pkl")

print("Model Trained & Saved Successfully!")