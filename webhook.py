import os
import re
import datetime
import tempfile
import traceback

import requests
import stripe
from flask import Flask, request, jsonify
from supabase import create_client
from fpdf import FPDF

# ---------------- Flask app ----------------
app = Flask(__name__)

# ---------------- Environment ----------------
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "onboarding@resend.dev")

TABLE_NAME = os.getenv("TABLE_NAME", "leads")
BUCKET = os.getenv("BUCKET", "casefiles")

# ---------------- Clients ----------------
stripe.api_key = STRIPE_SECRET_KEY
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ---------------- Text helpers ----------------
CTRL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")  # strip control chars (keep \t \n \r)

def strip_non_latin1(text: str) -> str:
    return (text or "").encode("latin1", "ignore").decode("latin1")

def normalize_spaces(s: str) -> str:
    # NBSP → space, tabs → single space, drop control chars, soften long runs of dashes/underscores
    s = (s or "").replace("\xa0", " ").replace("\t", " ")
    s = CTRL_RE.sub("", s)
    s = re.sub(r"([-–—_])\1{9,}", lambda m: " ".join([m.group(1)*10] * (len(m.group(0)) // 10 + 1)), s)
    return s

def soften_long_tokens(s: str, max_len: int = 40) -> str:
    """Insert spaces inside any run of non‑whitespace longer than max_len so FPDF can wrap."""
    def chunker(w: str) -> str:
        return " ".join(w[i:i + max_len] for i in range(0, len(w), max_len))
    # Break URLs first
    s = re.sub(r"(https?://\S+)", lambda m: chunker(m.group(0)), s)
    # Then any long token
    return re.sub(r"\S{" + str(max_len) + r",}", lambda m: chunker(m.group(0)), s)

def sanitize_text(text: str) -> str:
    t = strip_non_latin1(text or "")
    t = normalize_spaces(t)
    t = soften_long_tokens(t, max_len=40)
    return t

# ---------------- PDF helpers ----------------
def safe_multicell(pdf: FPDF, line: str, line_height: float, avail_w: float):
    """
    Try multi_cell; if fpdf still complains, fall back to fixed-size cell chunks.
    """
    try:
        pdf.set_x(pdf.l_margin)  # ensure we start from the left margin
        pdf.multi_cell(w=avail_w, h=line_height, txt=line)
    except Exception:
        # Final fallback: hard chunk and write via non-wrapping cell
        chunk = 60
        i = 0
        while i < len(line):
            piece = line[i:i+chunk]
            pdf.set_x(pdf.l_margin)
            # cell() does not wrap; it won't raise the single-character width error
            pdf.cell(w=avail_w, h=line_height, txt=piece, ln=1)
            i += chunk

def generate_pdf(text: str) -> str:
    """Create a PDF from text and return a temp file path."""
    safe_text = sanitize_text(text)

    pdf = FPDF()
    pdf.set_margins(left=12, top=12, right=12)  # explicit margins to get a predictable width
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    # Slightly smaller font to increase per-glyph width headroom
    pdf.set_font("Arial", size=10)

    # Compute explicit available width (avoid w=0 surprises)
    avail_w = pdf.w - pdf.l_margin - pdf.r_margin
    line_h = 7.0

    for raw_line in safe_text.splitlines():
        line = sanitize_text(raw_line)  # per-line sanitize, just in case
        safe_multicell(pdf, line, line_h, avail_w)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    pdf.output(tmp.name)
    return tmp.name

def upload_to_supabase(local_path: str, visa_type: str) -> str:
    """Upload file to Supabase Storage and return a 1‑hour signed URL."""
    ts = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    object_name = f"{sanitize_text(visa_type or 'Checklist')}_{ts}.pdf"
    with open(local_path, "rb") as f:
        supabase.storage.from_(BUCKET).upload(object_name, f, {"content-type": "application/pdf"})
    signed = supabase.storage.from_(BUCKET).create_signed_url(object_name, 3600)
    return signed.get("signedURL", "")

def send_resend_email(to_email: str, petitioner: str, visa_type: str, signed_url: str) -> tuple[int, str]:
    payload = {
        "from": f"ImmigrAI <{FROM_EMAIL}>",
        "to": to_email,
        "subject": "Your ImmigrAI USCIS Checklist",
        "html": (
            f"<p>Hi {sanitize_text(petitioner or 'there')},</p>"
            f"<p>Here is your personalized checklist for your {sanitize_text(visa_type or 'visa')} application.</p>"
            f'<p><a href="{signed_url}">Click here to download your checklist PDF</a></p>'
            "<br><p>Best,<br>The ImmigrAI Team</p>"
        ),
    }
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    return r.status_code, r.text

# ---------------- Health routes ----------------
@app.get("/")
def root():
    return "✅ ImmigrAI webhook running", 200

@app.get("/webhook")
def webhook_info():
    return "Stripe webhook endpoint is alive. Send POST events from Stripe.", 200

# ---------------- Stripe webhook ----------------
@app.post("/webhook")
def stripe_webhook():
    # Sanity check env
    missing = [k for k, v in {
        "STRIPE_SECRET_KEY": STRIPE_SECRET_KEY,
        "STRIPE_WEBHOOK_SECRET": STRIPE_WEBHOOK_SECRET,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_SERVICE_ROLE_KEY": SUPABASE_SERVICE_ROLE_KEY,
        "RESEND_API_KEY": RESEND_API_KEY,
    }.items() if not v]
    if missing:
        print("❌ Missing env vars:", ", ".join(missing))
        return jsonify({"error": "missing_env", "details": missing}), 200

    payload = request.data
    sig = request.headers.get("Stripe-Signature", "")

    # 1) Verify signature
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        print("❌ Signature verification failed:", e)
        return f"Invalid signature: {e}", 400

    event_type = event.get("type")
    print("✅ Event type:", event_type)

    if event_type != "checkout.session.completed":
        return jsonify({"ignored": event_type}), 200

    pdf_path = None
    try:
        session = event["data"]["object"]
        email = (session.get("customer_details") or {}).get("email") or session.get("customer_email")
        print("Email from Stripe:", email)

        if not email:
            print("⚠️ No email present in session; cannot match lead.")
            return jsonify({"status": "no_email"}), 200

        # 2) Fetch most recent matching lead
        q = supabase.table(TABLE_NAME).select("*").eq("email", email).order("created_at", desc=True).limit(1).execute()
        found = bool(q.data)
        print("Lead found?", found)
        if not found:
            return jsonify({"status": "no_matching_lead"}), 200

        lead = q.data[0]
        text = lead.get("checklist_text") or ""
        petitioner = lead.get("petitioner_name") or "there"
        visa_type = lead.get("visa_type") or "Checklist"

        print("Generating PDF for visa_type:", visa_type)

        # 3) Generate PDF (hardened)
        pdf_path = generate_pdf(text)

        # 4) Upload + sign URL
        signed_url = upload_to_supabase(pdf_path, visa_type)
        print("Signed URL (truncated):", (signed_url or "")[:120], "...")

        # 5) Send email
        status, body = send_resend_email(email, petitioner, visa_type, signed_url)
        print("Resend status:", status, body[:200])

        return jsonify({"ok": True}), 200

    except Exception as e:
        print("💥 Handler error:", repr(e))
        traceback.print_exc()
        return jsonify({"error": str(e)}), 200
    finally:
        if pdf_path:
            try:
                os.unlink(pdf_path)
            except Exception:
                pass

# Local run (Render uses gunicorn with `gunicorn webhook:app`)
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
