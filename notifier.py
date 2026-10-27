import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, Any, List
import urllib.request
import json

from models import FullItinerary

logger = logging.getLogger("notifier")


def send_telegram_alert(bot_token: str, chat_id: str, message: str) -> bool:
    """Send alert message via Telegram Bot API using urllib (no extra deps)."""
    if not bot_token or not chat_id:
        logger.warning("Telegram alert skipped: bot_token or chat_id missing.")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                logger.info("Telegram alert sent successfully.")
                return True
            else:
                logger.error(f"Telegram API error HTTP {resp.status}")
                return False
    except Exception as e:
        logger.error(f"Failed to send Telegram alert: {e}")
        return False


def send_email_alert(
    smtp_host: str,
    smtp_port: int,
    sender_email: str,
    smtp_password: str,
    recipient_email: str,
    subject: str,
    body: str
) -> bool:
    """Send alert via standard SMTP with TLS."""
    if not smtp_host or not sender_email or not recipient_email:
        logger.warning("Email alert skipped: SMTP configuration incomplete.")
        return False

    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = recipient_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            if smtp_password:
                server.login(sender_email, smtp_password)
            server.send_message(msg)
        logger.info(f"Email alert sent successfully to {recipient_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email alert: {e}")
        return False


def check_and_send_alerts(config: Dict[str, Any], itineraries: List[FullItinerary]):
    alerts_cfg = config.get("alerts", {})
    if not alerts_cfg.get("enabled", False):
        return

    threshold = alerts_cfg.get("alert_threshold_eur", 1800)
    if not itineraries:
        return

    best = itineraries[0]
    if best.total_price > threshold:
        logger.info(f"Best itinerary (€{best.total_price}) exceeds alert threshold (€{threshold}). No alert triggered.")
        return

    print("\n" + "=" * 70)
    print(f"🚨 PRICE ALERT TRIGGERED: Best total €{best.total_price} <= Threshold €{threshold}!")
    print("=" * 70)

    # Format message
    message = (
        f"✈️ *Flight Deal Found: €{best.total_price}*\n"
        f"Trip: {best.route_summary}\n"
        f"Start Date: {best.start_date}\n\n"
        f"• L1: {best.l1_from}->{best.l1_to} ({best.l1_date}): €{best.l1_flight.price} ({', '.join(best.l1_flight.airlines)})\n"
        f"• L2: {best.l2_from}->{best.l2_to} ({best.l2_date}): €{best.l2_flight.price} ({', '.join(best.l2_flight.airlines)})\n"
        f"• L3: {best.l3_from}->{best.l3_to} ({best.l3_date}): €{best.l3_flight.price} ({', '.join(best.l3_flight.airlines)})\n"
        f"• L4: {best.l4_from}->{best.l4_to} ({best.l4_date}): €{best.l4_flight.price} ({', '.join(best.l4_flight.airlines)})\n\n"
        f"_Note: Separate one-way tickets. Indicative prices._"
    )

    # Telegram
    tg_cfg = alerts_cfg.get("telegram", {})
    if tg_cfg.get("enabled", False):
        token = tg_cfg.get("bot_token")
        cid = tg_cfg.get("chat_id")
        send_telegram_alert(token, cid, message)

    # Email
    email_cfg = alerts_cfg.get("email", {})
    if email_cfg.get("enabled", False):
        send_email_alert(
            smtp_host=email_cfg.get("smtp_host", "smtp.gmail.com"),
            smtp_port=email_cfg.get("smtp_port", 587),
            sender_email=email_cfg.get("sender_email", ""),
            smtp_password=email_cfg.get("smtp_password", ""),
            recipient_email=email_cfg.get("recipient_email", ""),
            subject=f"[Flight Alert] Dublin -> Japan Deal: €{best.total_price}",
            body=message.replace("*", "").replace("_", "")
        )
