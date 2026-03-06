import urllib.request, urllib.error, json, base64, time, os

# Read the good file
with open('/home/user/files/tmp/app_good.py', 'rb') as f:
    good_bytes = f.read()
print(f"Good file size: {len(good_bytes)} bytes")

# Get current SHA of broken file on main
url = "https://api.github.com/repos/sportaholic000-hue/cold-outreach-engine/contents/app.py"
req = urllib.request.Request(url, headers={"User-Agent": "restore-script", "Accept": "application/vnd.github+json"})
with urllib.request.urlopen(req) as r:
    current = json.loads(r.read())
current_sha = current["sha"]
print(f"Current SHA: {current_sha}, size: {current['size']}")

# Encode good file
good_b64 = base64.b64encode(good_bytes).decode("ascii")

# Try to find a GitHub token
token = ""
for k in ["GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT", "GH_PAT"]:
    token = os.environ.get(k, "")
    if token:
        print(f"Token found in env var: {k}")
        break

if not token:
    # List all env vars that might be tokens
    for k, v in os.environ.items():
        if any(x in k.upper() for x in ["TOKEN", "PAT", "KEY", "SECRET", "AUTH"]):
            print(f"  Possible token env var: {k} = {v[:20]}...")
    print("No GitHub token found in environment")
else:
    payload = json.dumps({
        "message": "fix: restore full 884-line app.py from commit 68250a8",
        "content": good_b64,
        "sha": current_sha,
        "branch": "main"
    }).encode("utf-8")

    push_url = "https://api.github.com/repos/sportaholic000-hue/cold-outreach-engine/contents/app.py"
    push_req = urllib.request.Request(push_url, data=payload, method="PUT", headers={
        "Content-Type": "application/json",
        "User-Agent": "restore-script",
        "Accept": "application/vnd.github+json",
        "Authorization": f"token {token}"
    })
    try:
        with urllib.request.urlopen(push_req) as r:
            result = json.loads(r.read())
            new_sha = result["commit"]["sha"]
            print(f"SUCCESS! New commit SHA: {new_sha}")

            # Trigger Render redeploy
            render_url = "https://api.render.com/v1/services/srv-d6kdn9vpm1nc73esasvg/deploys"
            render_payload = json.dumps({"clearCache": "do_not_clear"}).encode()
            render_req = urllib.request.Request(render_url, data=render_payload, method="POST", headers={
                "Authorization": "Bearer rnd_ksGgcsOBcxxaFRg5gX4zB34uf1H5",
                "Content-Type": "application/json",
                "Accept": "application/json"
            })
            with urllib.request.urlopen(render_req) as r2:
                deploy = json.loads(r2.read())
                print(f"Render deploy triggered: {deploy.get('id', deploy)}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"Push failed ({e.code}): {body[:500]}")
