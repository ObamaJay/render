import os
from flask import Flask, request, jsonify
import stripe

app = Flask(__name__)

# Stripe setup
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

@app.route("/webhook", methods=["POST"])
def webhook_received():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except ValueError:
        # Invalid payload
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError:
        # Invalid signature
        return "Invalid signature", 400

    # Handle successful payment
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        print(f"✅ Payment received for: {session.get('customer_email')}")
        # TODO: Add Supabase logic here if needed

    return jsonify(success=True)

@app.route("/", methods=["GET"])
def home():
    return "✅ Webhook server running"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
