import random
import string
import time
import re
import os
import uuid
import threading
import tempfile
import requests
from urllib.parse import urlparse, parse_qs, unquote
from bs4 import BeautifulSoup
from flask import (Flask, request, jsonify, render_template, render_template_string,
                   session, redirect, url_for, Response)
from werkzeug.utils import secure_filename
from functools import wraps

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.secret_key = 'morphstudio_xK9mPqL2024_fixed'

PASSWORD = '123'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}

# ── Job state ─────────────────────────────────────────────────────────────────
jobs = {}

# ── Auth ──────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

LOGIN_HTML = '''<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MORPH STUDIO — Login</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:wght@400&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:#050507;color:#e8e8f0;font-family:"DM Sans",sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;background-image:radial-gradient(ellipse at 50% 0%,rgba(200,255,0,.04) 0%,transparent 60%)}
.box{width:360px;background:#0d0d12;border:1px solid #1e1e2a;border-radius:14px;padding:2.5rem 2rem;display:flex;flex-direction:column;gap:1.5rem;box-shadow:0 40px 80px rgba(0,0,0,.6)}
.logo{font-family:"Bebas Neue",sans-serif;font-size:2.2rem;letter-spacing:.2em;color:#c8ff00;text-align:center;text-shadow:0 0 40px rgba(200,255,0,.35)}
.logo span{color:#e8e8f0}
.sub{font-size:.7rem;text-align:center;color:#5a5a72;letter-spacing:.15em;font-family:"DM Sans",sans-serif;margin-top:-.5rem}
.field{display:flex;flex-direction:column;gap:.5rem}
label{font-size:.68rem;letter-spacing:.12em;color:#5a5a72;text-transform:uppercase}
input{width:100%;background:#111118;border:1px solid #1e1e2a;color:#e8e8f0;border-radius:6px;padding:.75rem 1rem;font-size:1rem;font-family:inherit;outline:none;transition:border-color .2s}
input:focus{border-color:#c8ff00}
button{width:100%;padding:.85rem;background:#c8ff00;color:#000;border:none;border-radius:6px;font-family:"Bebas Neue",sans-serif;font-size:1.1rem;letter-spacing:.15em;cursor:pointer;transition:all .2s;margin-top:.25rem}
button:hover{box-shadow:0 8px 30px rgba(200,255,0,.25);transform:translateY(-1px)}
.err{color:#ff4d6d;font-size:.8rem;text-align:center;background:rgba(255,77,109,.08);border:1px solid rgba(255,77,109,.2);padding:.6rem;border-radius:4px}
</style>
</head>
<body>
<div class="box">
  <div>
    <div class="logo">MORPH<span> STUDIO</span></div>
    <div class="sub">IMAGE → VIDEO ENGINE</div>
  </div>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="POST" style="display:flex;flex-direction:column;gap:1rem">
    <div class="field">
      <label>Şifre</label>
      <input type="password" name="password" placeholder="••••••••" autofocus autocomplete="current-password">
    </div>
    <button type="submit">GİRİŞ YAP</button>
  </form>
</div>
</body>
</html>'''

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect(url_for('index'))
    error = None
    if request.method == 'POST':
        if request.form.get('password') == PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        error = 'Yanlış şifre'
    return render_template_string(LOGIN_HTML, error=error)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

# ── Video Proxy (for CORS-free frame extraction + forced download) ─────────────
@app.route('/proxy_video')
@login_required
def proxy_video():
    url = request.args.get('url', '')
    download = request.args.get('dl', '0') == '1'
    if not url:
        return 'No URL', 400
    req_headers = {'User-Agent': 'Mozilla/5.0'}
    range_header = request.headers.get('Range')
    if range_header:
        req_headers['Range'] = range_header
    try:
        r = requests.get(url, headers=req_headers, stream=True, timeout=60)
        resp_headers = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': '*',
        }
        for h in ['Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges']:
            if h in r.headers:
                resp_headers[h] = r.headers[h]
        if 'Accept-Ranges' not in resp_headers:
            resp_headers['Accept-Ranges'] = 'bytes'
        if download:
            resp_headers['Content-Disposition'] = 'attachment; filename="morph_video.mp4"'
        return Response(r.iter_content(chunk_size=65536), status=r.status_code, headers=resp_headers)
    except Exception as e:
        return str(e), 500

# ── Common Headers ────────────────────────────────────────────────────────────
def make_headers():
    return {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "content-type": "application/json",
        "origin": "https://app.morphstudio.com",
        "priority": "u=1, i",
        "referer": "https://app.morphstudio.com/",
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    }

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ── eTemp ─────────────────────────────────────────────────────────────────────
class eTemp:
    def random_email(self, length):
        return ''.join(
            random.SystemRandom().choice(string.ascii_lowercase + string.digits)
            for _ in range(length)
        )

    def getEmail(self):
        return self.random_email(15) + '@spamok.com'

    def getVerifyLink(self, mail):
        username = mail.replace('@spamok.com', '')
        for attempt in range(30):
            r = requests.get(f'https://api.spamok.com/v2/EmailBox/{username}')
            mails = r.json().get('mails', [])
            for m in mails:
                subject = m.get('subject', '')
                if 'Verify' in subject or 'Confirm' in subject:
                    mail_id = m['id']
                    detail = requests.get(f'https://api.spamok.com/v2/Email/{username}/{mail_id}')
                    html = detail.json().get('messageHtml', '')
                    soup = BeautifulSoup(html, 'html.parser')
                    for a in soup.find_all('a', href=True):
                        href = a['href']
                        if 'morphstudio.com/redirect-verify-email' in href:
                            return href
                    match = re.search(
                        r'(https://app\.morphstudio\.com/redirect-verify-email[^\s\'"<>]+)', html)
                    if match:
                        return match.group(1).strip()
            time.sleep(1)
        return None

# ── Core pipeline ─────────────────────────────────────────────────────────────
def run_job(job_id, image_path, prompt, model_id, duration, resolution):
    sess = requests.Session()
    headers = make_headers()
    log = jobs[job_id]['log']

    def log_step(msg):
        log.append(msg)
        jobs[job_id]['log'] = log

    try:
        # 1. Register
        temp = eTemp()
        email = temp.getEmail()
        log_step(f"📧 Hesap olusturuluyor: {email}")

        reg = sess.post("https://api.morphstudio.com/api/user/register",
                        headers=headers,
                        json={"email": email,
                              "password": "gAAAAABpxEt-er9DyB8oUlOOVEDzEWjljI7--ObDcYvxtQUGG7Gx7cXipXyodubFFvxJ3KQcLC1uyAeua1vrwxZ71cfSQgRMOA=="})
        user_id = reg.json().get("userId")
        if not user_id:
            raise Exception("userId alinamadi")
        log_step(f"✅ Kayit basarili (userId: {user_id[:8]}...)")

        # 2. Send verify email
        sess.post("https://api.morphstudio.com/api/user/send-verify-email",
                  headers=headers, json={"userId": user_id})
        log_step("📨 Dogrulama maili gonderildi")

        # 3. Get verify link
        log_step("⏳ SpamOK'tan mail bekleniyor...")
        verify_link = temp.getVerifyLink(email)
        if not verify_link:
            raise Exception("Dogrulama maili 30 saniye icinde gelmedi")
        log_step("🔗 Dogrulama linki alindi")

        # 4. Verify email
        parsed = urlparse(verify_link)
        params = parse_qs(parsed.query)
        sess.post("https://api.morphstudio.com/api/user/verify-email",
                  headers=headers,
                  json={"email": unquote(params["email"][0]),
                        "token": params["token"][0],
                        "userId": params["userId"][0]})
        log_step("✅ Email dogrulandi")

        # 5. Upload image
        filename = os.path.basename(image_path)
        log_step(f"🖼️  Gorsel yukleniyor: {filename}")

        create_resp = sess.post("https://api.morphstudio.com/api/v1/storage/create",
                                headers=headers,
                                json={"displayName": filename, "isPublic": True})
        create_data = create_resp.json()
        object_id = create_data["objectId"]
        presigned = create_data["presigned"]
        upload_url = presigned["url"]
        fields = presigned["fields"]

        with open(image_path, "rb") as f:
            file_data = f.read()

        form_fields = [
            ("key",            fields["key"]),
            ("AWSAccessKeyId", fields["AWSAccessKeyId"]),
            ("policy",         fields["policy"]),
            ("signature",      fields["signature"]),
            ("file",           (filename, file_data, "image/jpeg")),
        ]
        gcs_headers = {k: v for k, v in make_headers().items()
                       if k not in ("content-type", "sec-fetch-site")}
        gcs_headers["sec-fetch-site"] = "cross-site"

        up = requests.post(upload_url, headers=gcs_headers, files=form_fields)
        if up.status_code not in (200, 204):
            raise Exception(f"GCS upload hatasi: {up.status_code}")
        log_step(f"✅ Gorsel yuklendi (objectId: {object_id[:8]}...)")

        # 6. Create video node
        log_step(f"🎬 Video olusturuluyor — model: {model_id}, prompt: \"{prompt}\"")
        vid = sess.post(
            "https://api.morphstudio.com/api/v1/moca/media_session/video/node/create",
            headers=headers,
            json={"session_id": "", "model_id": model_id, "sessionType": "video",
                  "params": {"prompt": prompt, "duration": duration,
                             "resolution": resolution, "start_image_url": object_id}})
        log_step("⚙️  Video kuyruğa alindi, isleniyor...")

        # 7. Poll
        timeout, interval, elapsed = 300, 5, 0
        while elapsed < timeout:
            time.sleep(interval)
            elapsed += interval
            list_resp = sess.get(
                "https://api.morphstudio.com/api/v1/moca/media_session/video/list?limit=100",
                headers=headers)
            data = list_resp.json()
            for date, sessions_list in data.get("sessions", {}).items():
                for s in sessions_list:
                    for node in s.get("recentNodes", []):
                        status = node.get("status", "")
                        cdn_url = node.get("cdn_url", "")
                        progress = node.get("progress", {}).get("progress", 0)

                        if status == "failed":
                            raise Exception(node.get("error_message", "Video basarisiz"))

                        if cdn_url:
                            log_step(f"🎉 Video hazir!")
                            jobs[job_id]['status'] = 'done'
                            jobs[job_id]['cdn_url'] = cdn_url
                            return

                        log_step(f"⏳ İşleniyor... %{progress}")

        raise Exception("Timeout: video 5 dakikada tamamlanamadi")

    except Exception as e:
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['error'] = str(e)
        log_step(f"❌ Hata: {e}")
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/generate', methods=['POST'])
@login_required
def generate():
    if 'image' not in request.files:
        return jsonify({'error': 'Gorsel gerekli'}), 400
    file = request.files['image']
    if not file or not allowed_file(file.filename):
        return jsonify({'error': 'Gecersiz dosya'}), 400

    filename = secure_filename(file.filename)
    ext = filename.rsplit('.', 1)[1].lower()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f'.{ext}')
    file.save(tmp.name)
    save_path = tmp.name

    prompt     = request.form.get('prompt', '')
    model_id   = request.form.get('model', 'seedance_lite')
    duration   = int(request.form.get('duration', 5))
    resolution = request.form.get('resolution', '480p')

    job_id = uuid.uuid4().hex
    jobs[job_id] = {'status': 'running', 'log': [], 'cdn_url': None, 'error': None}

    t = threading.Thread(target=run_job,
                         args=(job_id, save_path, prompt, model_id, duration, resolution),
                         daemon=True)
    t.start()

    return jsonify({'job_id': job_id})

@app.route('/status/<job_id>')
@login_required
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job bulunamadi'}), 404
    return jsonify(job)

if __name__ == '__main__':
    app.run(debug=True, port=5000)
