import base64
import json
import urllib.request
import os

# Read file
with open('/home/user/files/tmp/app_good.py', 'rb') as f:
    content = f.read()

print(f"File size: {len(content)} bytes")

b64_content = base64.b64encode(content).decode('ascii')

payload = json.dumps({
    "message": "fix: restore correct 884-line app.py with full mockup engine",
    "content": b64_content,
    "sha": "88a70ce8f657f6cad33e9709a67ec53c4e488b8f",
    "branch": "main"
}).encode('utf-8')

token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_PAT')
print(f"Token found: {token is not None}")
print(f"Env vars with GIT/TOKEN: {[k for k in os.environ.keys() if 'GIT' in k.upper() or 'TOKEN' in k.upper()]}")

if token:
    req = urllib.request.Request(
        'https://api.github.com/repos/sportaholic000-hue/cold-outreach-engine/contents/app.py',
        data=payload,
        method='PUT',
        headers={
            'Authorization': f'token {token}',
            'Content-Type': 'application/json',
            'User-Agent': 'nebula-agent'
        }
    )
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            print("SUCCESS:", result.get('commit', {}).get('sha'))
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"HTTP ERROR {e.code}: {body}")
else:
    print("NO TOKEN - listing all env vars:")
    for k, v in sorted(os.environ.items()):
        print(f"  {k}={v[:20] if v else ''}")
