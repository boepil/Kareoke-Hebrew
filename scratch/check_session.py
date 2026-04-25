import requests
import json

try:
    response = requests.get("http://127.0.0.1:8765/api/session")
    print(json.dumps(response.json(), indent=2, ensure_ascii=False))
except Exception as e:
    print(f"Failed to connect: {e}")
