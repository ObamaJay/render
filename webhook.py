import os
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


# ---------------- Helpers ----------------
def strip_non_latin1(text: str) -> str:
    return (text or "").encode("latin1", "ignore").decode("latin1")


def generate_pdf(text: str) -> str:
    """Create a PDF from text and return a temp file path."""
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_font("Arial", size=12)
    for line in strip_non_latin1(text).split("\n"):
        pdf.multi_cell(0, 10, line)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    pdf.output(tmp.name)
    return tmp.name


def upload_to_supabase(local_path: str, visa_type: str) -> str:
    """Upload file to Supabase Storage and return a 1‑hour signed URL."""
    ts = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    object_name = f"{strip_non_latin1(visa_type or 'Checklist')}_{ts}.pdf"
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
            f"<p>Hi {strip_non_latin1(petitioner or 'there')},</p>"
            f"<p>Here is your personalized checklist for your {strip_non_latin1(visa_type or 'visa')} application.</p>"
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
    # 0) Verify config present
    missing = [k for k, v in {
        "STRIPE_SECRET_KEY": STRIPE_SECRET_KEY,
        "STRIPE_WEBHOOK_SECRET": STRIPE_WEBHOOK_SECRET,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_SERVICE_ROLE_KEY": SUPABASE_SERVICE_ROLE_KEY,
        "RESEND_API_KEY": RESEND_API_KEY,
    }.items() if not v]
    if missing:
        print("❌ Missing env vars:", ", ".join(missing))
        # still return 200 so Stripe stops retrying
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
        # Ignore other events for now
        return jsonify({"ignored": event_type}), 200

    pdf_path = None
    try:
        session = event["data"]["object"]
        # email could be in two places depending on Checkout config
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

        # 3) Generate PDF
        pdf_path = generate_pdf(text)

        # 4) Upload + sign URL
        signed_url = upload_to_supabase(pdf_path, visa_type)
        print("Signed URL (truncated):", signed_url[:120], "...")

        # 5) Send email
        status, body = send_resend_email(email, petitioner, visa_type, signed_url)
        print("Resend status:", status, body[:200])

        return jsonify({"ok": True}), 200

    except Exception as e:
        # Prevent 502s; show why in logs
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
