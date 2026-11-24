import os
from google import genai

c = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
for m in c.models.list():
    print(m.name)