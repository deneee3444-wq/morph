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
from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}

# ── Job state ────────────────────────────────────────────────────────────────
jobs = {}  # job_id -> { status, log, cdn_url, error }

# ── Ortak Headers ────────────────────────────────────────────────────────────
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

# ── eTemp ────────────────────────────────────────────────────────────────────
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
def index():
    return render_template('index.html')

@app.route('/generate', methods=['POST'])
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
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job bulunamadi'}), 404
    return jsonify(job)

if __name__ == '__main__':
    app.run(debug=True, port=5000)
