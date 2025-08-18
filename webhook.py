@app.route("/webhook", methods=["POST"])
def webhook_received():
    payload = request.data
    sig = request.headers.get("Stripe-Signature")

    # 1) Verify signature
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        print("❌ Signature error:", e)
        return f"Invalid signature: {e}", 400

    print("✅ Event type:", event.get("type"))

    # Only handle checkout completion
    if event.get("type") != "checkout.session.completed":
        return jsonify({"ignored": event.get("type")}), 200

    try:
        session = event["data"]["object"]
        email = (session.get("customer_details") or {}).get("email") or session.get("customer_email")
        print("Email from Stripe:", email)

        if not email:
            print("⚠️ No email in session")
            return jsonify({"status": "no_email"}), 200

        # Fetch latest matching lead
        q = supabase.table(TABLE_NAME).select("*").eq("email", email).order("created_at", desc=True).limit(1).execute()
        print("Lead found?", bool(q.data))
        if not q.data:
            return jsonify({"status": "no_matching_lead"}), 200

        lead = q.data[0]
        visa_type = lead.get("visa_type") or "Checklist"
        petitioner = lead.get("petitioner_name") or "there"
        text = lead.get("checklist_text") or ""
        print("Generating PDF for visa_type:", visa_type)

        # PDF
        pdf_path = generate_pdf(text)

        # Upload
        signed_url = upload_to_supabase(pdf_path, visa_type)
        print("Signed URL:", signed_url[:120], "...")

        # Email
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": f"ImmigrAI <{FROM_EMAIL}>",
                "to": email,
                "subject": "Your ImmigrAI USCIS Checklist",
                "html": (
                    f"<p>Hi {strip_non_latin1(petitioner)},</p>"
                    f"<p>Here is your personalized checklist for your {strip_non_latin1(visa_type)} application.</p>"
                    f'<p><a href="{signed_url}">Click here to download your checklist PDF</a></p>'
                    "<br><p>Best,<br>The ImmigrAI Team</p>"
                ),
            },
            timeout=20,
        )
        print("Resend status:", r.status_code, r.text[:180])

        return jsonify({"ok": True}), 200

    except Exception as e:
        # Prevent 502s and show the exact reason in logs
        import traceback
        print("💥 Handler error:", repr(e))
        traceback.print_exc()
        return jsonify({"error": str(e)}), 200  # return 200 so Stripe stops retrying
    finally:
        try:
            os.unlink(pdf_path)
        except Exception:
            pass
