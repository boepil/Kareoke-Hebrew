import requests
try:
    r = requests.get("http://127.0.0.1:8765/api/projects", timeout=10)
    with open("response.bin", "wb") as f:
        f.write(r.content)
    print("Done")
except Exception as e:
    print(f"Error: {e}")
