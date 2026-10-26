import csv
import html
import logging
import os
import re
import smtplib
import ssl

from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

CSV_PATH = BASE_DIR / "addresses.csv"
TEMPLATE_PATH = BASE_DIR / "model.html"
LOG_PATH = BASE_DIR / "email_log.txt"


def setup_logger():
    logger = logging.getLogger("email_sender")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        file_handler = logging.FileHandler(
            LOG_PATH,
            mode="a",
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

    return logger


def get_smtp_settings():
    return {
        "host": os.getenv("SMTP_HOST", "").strip(),
        "port": int(os.getenv("SMTP_PORT", "465")),
        "username": os.getenv("SMTP_USER", "").strip(),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "sender_email": os.getenv("EMAIL_FROM", "").strip(),
        "sender_name": os.getenv("EMAIL_FROM_NAME", "").strip(),
    }


def validate_smtp_settings(settings):
    required_fields = [
        settings["host"],
        settings["username"],
        settings["password"],
        settings["sender_email"],
    ]

    return all(required_fields) and settings["port"] in (465, 587)


def load_email_template():
    if not TEMPLATE_PATH.is_file():
        raise FileNotFoundError(
            f"Email template file not found: {TEMPLATE_PATH}"
        )

    return TEMPLATE_PATH.read_text(encoding="utf-8")


def load_recipients():
    if not CSV_PATH.is_file():
        raise FileNotFoundError(
            f"CSV file not found: {CSV_PATH}"
        )

    with CSV_PATH.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as csv_file:
        sample = csv_file.read(8192)
        csv_file.seek(0)

        try:
            dialect = csv.Sniffer().sniff(
                sample,
                delimiters=",;\t",
            )
        except csv.Error:
            dialect = csv.excel

        reader = csv.DictReader(csv_file, dialect=dialect)

        if not reader.fieldnames:
            raise ValueError("The CSV file is empty or has no header.")

        reader.fieldnames = [
            (name or "").strip().lower()
            for name in reader.fieldnames
        ]

        required_columns = {"address", "subject", "link"}
        available_columns = set(reader.fieldnames)

        if not required_columns.issubset(available_columns):
            missing_columns = required_columns - available_columns

            raise ValueError(
                "Missing required CSV columns: "
                + ", ".join(sorted(missing_columns))
            )

        return [
            {
                "address": (row.get("address") or "").strip(),
                "subject": (row.get("subject") or "").strip(),
                "link": (row.get("link") or "").strip(),
                "row_number": index,
            }
            for index, row in enumerate(reader, start=2)
        ]


def validate_recipient(recipient):
    if not recipient or len(recipient) > 254:
        return False

    if any(character.isspace() for character in recipient):
        return False

    return bool(
        re.fullmatch(
            r"[^@\s]+@[^@\s]+\.[^@\s]+",
            recipient,
        )
    )


def validate_link(link):
    try:
        parsed_url = urlparse(link)

        return (
            parsed_url.scheme in ("http", "https")
            and bool(parsed_url.hostname)
            and not any(character.isspace() for character in link)
        )
    except ValueError:
        return False


def render_email_template(template, link):
    safe_link = html.escape(link, quote=True)

    # Replace the generic example URL used by the HTML template
    return template.replace(
        "https://example.com",
        safe_link,
    )


def create_email_message(settings, recipient, subject, html_content):
    message = EmailMessage()

    message["From"] = formataddr(
        (
            settings["sender_name"],
            settings["sender_email"],
        )
    )
    message["To"] = recipient
    message["Subject"] = subject

    message.set_content(
        "This email contains an HTML message. "
        "Please use an HTML-compatible email client to view its content."
    )

    message.add_alternative(html_content, subtype="html")

    return message


def send_email(settings, message):
    smtp_host = settings["host"]
    smtp_port = settings["port"]
    smtp_username = settings["username"]
    smtp_password = settings["password"]

    ssl_context = ssl.create_default_context()

    if smtp_port == 465:
        with smtplib.SMTP_SSL(
            smtp_host,
            smtp_port,
            context=ssl_context,
            timeout=30,
        ) as smtp_server:
            smtp_server.login(smtp_username, smtp_password)
            smtp_server.send_message(message)

    elif smtp_port == 587:
        with smtplib.SMTP(
            smtp_host,
            smtp_port,
            timeout=30,
        ) as smtp_server:
            smtp_server.ehlo()
            smtp_server.starttls(context=ssl_context)
            smtp_server.ehlo()

            smtp_server.login(smtp_username, smtp_password)
            smtp_server.send_message(message)

    else:
        raise ValueError("Unsupported SMTP port. Use 465 or 587.")


def process_recipient(settings, template, recipient_data, logger):
    recipient = recipient_data["address"]
    subject = recipient_data["subject"]
    link = recipient_data["link"]
    row_number = recipient_data["row_number"]

    logger.info(
        "Processing CSV row %s | Recipient: %s",
        row_number,
        recipient or "(empty)",
    )

    if not validate_recipient(recipient):
        logger.error(
            "INVALID_RECIPIENT | CSV row: %s | Address: %s",
            row_number,
            recipient or "(empty)",
        )
        return "failed"

    if not subject:
        logger.error(
            "MISSING_SUBJECT | CSV row: %s | Recipient: %s",
            row_number,
            recipient,
        )
        return "failed"

    if not validate_link(link):
        logger.error(
            "INVALID_LINK | CSV row: %s | Recipient: %s",
            row_number,
            recipient,
        )
        return "failed"

    html_content = render_email_template(template, link)

    message = create_email_message(
        settings,
        recipient,
        subject,
        html_content,
    )

    try:
        send_email(settings, message)

        logger.info(
            "SMTP_ACCEPTED | Recipient: %s | Subject: %s",
            recipient,
            subject,
        )

        return "sent"

    except smtplib.SMTPAuthenticationError:
        logger.exception(
            "SMTP_AUTHENTICATION_FAILED | Recipient: %s",
            recipient,
        )
        return "failed"

    except smtplib.SMTPRecipientsRefused:
        logger.exception(
            "RECIPIENT_REJECTED | Recipient: %s",
            recipient,
        )
        return "failed"

    except smtplib.SMTPException:
        logger.exception(
            "SMTP_ERROR | Recipient: %s",
            recipient,
        )
        return "failed"

    except (OSError, ValueError):
        logger.exception(
            "CONNECTION_OR_CONFIGURATION_ERROR | Recipient: %s",
            recipient,
        )
        return "failed"


def main():
    logger = setup_logger()

    run_started = datetime.now()
    sent_count = 0
    failed_count = 0
    skipped_count = 0

    logger.info("=" * 70)
    logger.info("EMAIL SENDING RUN STARTED")
    logger.info("Start time: %s", run_started.isoformat(timespec="seconds"))
    logger.info("CSV file: %s", CSV_PATH.name)
    logger.info("HTML template: %s", TEMPLATE_PATH.name)

    try:
        settings = get_smtp_settings()

        if not validate_smtp_settings(settings):
            logger.error(
                "INVALID_CONFIGURATION | Check the .env SMTP settings."
            )
            return

        template = load_email_template()
        recipients = load_recipients()

        total_count = len(recipients)

        logger.info("Total CSV records: %s", total_count)

        if total_count == 0:
            logger.warning("The CSV file contains no recipient records.")
            return

        for recipient_data in recipients:
            result = process_recipient(
                settings,
                template,
                recipient_data,
                logger,
            )

            if result == "sent":
                sent_count += 1
            else:
                failed_count += 1

        run_finished = datetime.now()
        duration = (run_finished - run_started).total_seconds()

        logger.info("-" * 70)
        logger.info("EMAIL SENDING RUN SUMMARY")
        logger.info("Total records: %s", total_count)
        logger.info("Accepted by SMTP server: %s", sent_count)
        logger.info("Failed: %s", failed_count)
        logger.info("Skipped: %s", skipped_count)
        logger.info("Start time: %s", run_started.isoformat(timespec="seconds"))
        logger.info(
            "Finish time: %s",
            run_finished.isoformat(timespec="seconds"),
        )
        logger.info("Duration: %.2f seconds", duration)
        logger.info("EMAIL SENDING RUN COMPLETED")
        logger.info("=" * 70)

    except (OSError, UnicodeError, ValueError, csv.Error):
        logger.exception("FATAL_ERROR | The sending process could not continue.")

    except Exception:
        logger.exception("UNEXPECTED_ERROR | An unexpected error occurred.")


if __name__ == "__main__":
    main()