from flask import Flask, request, jsonify
from flask_cors import CORS
import imaplib
import email
import email.utils
import smtplib
import re
import threading
import time
import random
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = Flask(__name__)
CORS(app)

bot_running = False
bot_thread  = None
stats = {"round": 0, "sent": 0, "processed": 0, "replies": 0, "mode": None}
logs  = []

def add_log(msg):
    t = time.strftime("%H:%M:%S")
    logs.append(f"[{t}] {msg}")
    if len(logs) > 200:
        logs.pop(0)

def smtp_connect(email_addr, password):
    """Try port 587 (STARTTLS) first, fallback to 465 (SSL)."""
    try:
        smtp = smtplib.SMTP("smtp.gmail.com", 587, timeout=15)
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(email_addr, password)
        add_log(f"SMTP connected via port 587 (STARTTLS)")
        return smtp
    except Exception as e1:
        add_log(f"Port 587 failed ({e1}), trying 465...")
        try:
            smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15)
            smtp.login(email_addr, password)
            add_log(f"SMTP connected via port 465 (SSL)")
            return smtp
        except Exception as e2:
            add_log(f"Both SMTP ports failed. 587: {e1} | 465: {e2}")
            return None

def imap_connect(email_addr, password):
    """Connect to Gmail IMAP on port 993."""
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(email_addr, password)
        return mail
    except Exception as e:
        add_log(f"IMAP connect failed for {email_addr}: {e}")
        return None

# ─────────────────────────────────────────────
# Shared: send blast emails
# ─────────────────────────────────────────────
def send_to_all(sender, receivers, subject, body, num_to_send):
    smtp = smtp_connect(sender["email"], sender["password"])
    if not smtp:
        add_log("Cannot send — SMTP connection failed.")
        return
    try:
        count = 0
        for r in receivers:
            if count >= num_to_send:
                add_log(f"Reached send limit of {num_to_send}, stopping.")
                break
            msg = MIMEMultipart()
            msg["From"]    = sender["email"]
            msg["To"]      = r["email"]
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain"))
            smtp.sendmail(sender["email"], r["email"], msg.as_string())
            stats["sent"] += 1
            count += 1
            add_log(f"Sent to {r['email']} ({count}/{num_to_send})")
            time.sleep(2)
    except Exception as e:
        add_log(f"Send error: {e}")
    finally:
        try:
            smtp.quit()
        except:
            pass

# ─────────────────────────────────────────────
# Shared: read inbox and reply
# ─────────────────────────────────────────────
def process_receiver(account, target_subject, num, reply_text, only_unread=True):
    email_addr = account["email"]
    password   = account["password"]

    mail = imap_connect(email_addr, password)
    if not mail:
        return

    try:
        mail.select("inbox")

        search_filter = f'UNSEEN SUBJECT "{target_subject}"' if only_unread else f'SUBJECT "{target_subject}"'
        status, messages = mail.search(None, search_filter)
        email_ids = messages[0].split()

        if not email_ids:
            add_log(f"{email_addr}: No matching emails found.")
            mail.logout()
            return

        for eid in reversed(email_ids[-num:]):
            status, data = mail.fetch(eid, '(RFC822)')
            msg          = email.message_from_bytes(data[0][1])

            actual_subject = (msg["Subject"] or "").strip()
            sender_email   = email.utils.parseaddr(msg["From"])[1].lower()

            if actual_subject.lower() != target_subject.lower():
                continue
            if sender_email == email_addr.lower():
                continue
            if actual_subject.lower().startswith("re:"):
                continue

            body = ""
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode(errors="ignore")
                    break

            stats["processed"] += 1
            add_log(f"{email_addr}: Match from {sender_email}")

            # Click first link found in body
            links = re.findall(r'(https?://\S+)', body)
            if links:
                try:
                    requests.get(links[0], timeout=10)
                    add_log(f"{email_addr}: Link clicked — {links[0]}")
                except:
                    pass

            # Mark as read
            mail.store(eid, '+FLAGS', '\\Seen')

            # Send reply
            smtp = smtp_connect(email_addr, password)
            if smtp:
                try:
                    reply = MIMEMultipart()
                    reply["From"]    = email_addr
                    reply["To"]      = sender_email
                    reply["Subject"] = "Re: " + actual_subject
                    reply.attach(MIMEText(reply_text, "plain"))
                    smtp.sendmail(email_addr, sender_email, reply.as_string())
                    stats["replies"] += 1
                    add_log(f"{email_addr}: Replied to {sender_email}")
                except Exception as e:
                    add_log(f"{email_addr}: Reply failed — {e}")
                finally:
                    try:
                        smtp.quit()
                    except:
                        pass
            else:
                add_log(f"{email_addr}: Skipping reply — SMTP unavailable")

            time.sleep(random.randint(3, 7))

        mail.logout()

    except Exception as e:
        add_log(f"{email_addr} error: {e}")
        try:
            mail.logout()
        except:
            pass

# ─────────────────────────────────────────────
# MODE 1: Read & reply only (no sending)
# ─────────────────────────────────────────────
def bot_loop_mode1(config):
    global bot_running
    round_num   = 1
    accounts    = config["accounts"]
    subject     = config["target_subject"]
    num         = config.get("num_emails", 10)
    reply_text  = config["reply_text"]
    interval    = config.get("interval", 60)
    only_unread = config.get("only_unread", True)

    while bot_running:
        stats["round"] = round_num
        add_log(f"--- [Mode 1] Round {round_num} ---")

        for acct in accounts:
            if not bot_running:
                break
            add_log(f"Checking {acct['email']}...")
            process_receiver(acct, subject, num, reply_text, only_unread)
            time.sleep(random.randint(3, 8))

        add_log(f"Sleeping {interval}s before next check...")
        time.sleep(interval)
        round_num += 1

# ─────────────────────────────────────────────
# MODE 2: Send → wait → read & reply
# ─────────────────────────────────────────────
def bot_loop_mode2(config):
    global bot_running
    round_num   = 1
    num_to_send = config.get("num_to_send", len(config["receivers"]))
    only_unread = config.get("only_unread", True)

    while bot_running:
        stats["round"] = round_num
        add_log(f"--- [Mode 2] Round {round_num} ---")

        send_to_all(
            config["sender"],
            config["receivers"],
            config["subject"],
            config["body"],
            num_to_send
        )

        add_log(f"Waiting {config['interval']}s for replies to arrive...")
        time.sleep(config["interval"])

        for r in config["receivers"]:
            if not bot_running:
                break
            add_log(f"Checking inbox: {r['email']}...")
            process_receiver(
                r,
                config["target_subject"],
                config["num_emails"],
                config["reply_text"],
                only_unread
            )
            time.sleep(random.randint(5, 10))

        round_num += 1
        time.sleep(config["interval"])

# ─────────────────────────────────────────────
# API routes
# ─────────────────────────────────────────────
@app.route("/start", methods=["POST"])
def start():
    global bot_running, bot_thread
    if bot_running:
        return jsonify({"status": "already running"})

    config = request.json
    mode   = config.get("mode", 2)

    bot_running = True
    stats.update({"round": 0, "sent": 0, "processed": 0, "replies": 0, "mode": mode})
    logs.clear()

    if mode == 1:
        target = bot_loop_mode1
        add_log("Starting in Mode 1: read & reply only")
    else:
        target = bot_loop_mode2
        add_log("Starting in Mode 2: send then read & reply")

    bot_thread = threading.Thread(target=target, args=(config,), daemon=True)
    bot_thread.start()
    return jsonify({"status": "started", "mode": mode})

@app.route("/stop", methods=["POST"])
def stop():
    global bot_running
    bot_running = False
    add_log("Bot stopped by user.")
    return jsonify({"status": "stopped"})

@app.route("/status")
def status():
    return jsonify({"running": bot_running, "stats": stats, "logs": logs[-50:]})

@app.route("/")
def index():
    return "Bot API running — POST /start with {mode: 1 or 2, ...config}"

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
