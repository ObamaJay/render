import os
import datetime
import tempfile
import requests
import stripe
from fastapi import FastAPI, Request, HTTPException
from supabase import create_client
from fpdf import FPDF

# ---- Env / Clients ----
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
FROM_EMAIL = os.getenv("FROM_EMAIL", "onboarding@resend.dev")

TABLE_NAME = os.getenv("TABLE_NAME", "leads")      # your Streamlit app saves into 'leads'
BUCKET = os.getenv("BUCKET", "casefiles")          # Supabase Storage bucket name

app = FastAPI()

def strip_non_latin1(text: str) -> str:
    return text.encode("latin1", "ignore").decode("latin1")

def generate_pdf(text: str) -> str:
    """Return a temp file path to the generated PDF."""
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_font("Arial", size=12)
    for line in text.split("\n"):
        pdf.multi_cell(0, 10, strip_non_latin1(line))
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    pdf.output(tmp.name)
    return tmp.name

def upload_to_supabase(local_path: str, visa_type: str) -> str:
    """Upload file to Supabase Storage and return a 1‑hour signed URL."""
    ts = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    object_name = f"{visa_type}_{ts}.pdf"
    with open(local_path, "rb") as f:
        supabase.storage.from_(BUCKET).upload(object_name, f, {"content-type": "application/pdf"})
    signed = supabase.storage.from_(BUCKET).create_signed_url(object_name, 3600)
    return signed.get("signedURL", "")

def send_email(to_email: str, petitioner: str, visa_type: str, signed_url: str):
    payload = {
        "from": f"ImmigrAI <{FROM_EMAIL}>",
        "to": to_email,
        "subject": "Your ImmigrAI USCIS Checklist",
        "html": (
            f"<p>Hi {strip_non_latin1(petitioner)},</p>"
            f"<p>Here is your personalized checklist for your {strip_non_latin1(visa_type)} visa application.</p>"
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
    if r.status_code not in (200, 202):
        raise RuntimeError(f"Resend error {r.status_code}: {r.text}")

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(500, "STRIPE_WEBHOOK_SECRET not set")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(400, f"Invalid signature: {e}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        email = (session.get("customer_details") or {}).get("email")
        if not email:
            return {"status": "no_email"}

        # 1) Find latest lead by email
        q = supabase.table(TABLE_NAME).select("*").eq("email", email).order("created_at", desc=True).limit(1).execute()
        if not q.data:
            return {"status": "no_matching_lead"}

        lead = q.data[0]
        checklist = lead.get("checklist_text") or ""
        petitioner = lead.get("petitioner_name") or "there"
        visa_type = lead.get("visa_type") or "Checklist"

        # 2) Generate PDF
        pdf_path = generate_pdf(checklist)

        try:
            # 3) Upload & sign URL
            signed_url = upload_to_supabase(pdf_path, visa_type)

            # 4) Email link
            send_email(email, petitioner, visa_type, signed_url)
        finally:
            try: os.unlink(pdf_path)
            except Exception: pass

    return {"received": True}
