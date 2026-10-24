"""Canonical forms for identities, so the same person is recognised across imports and channels."""
import re
from urllib.parse import urlparse

import phonenumbers

from app import config

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Consumer mailbox providers: their domain says nothing about the person's company.
FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.ie", "hotmail.com", "hotmail.co.uk",
    "outlook.com", "outlook.ie", "live.com", "live.ie", "msn.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "protonmail.com", "proton.me", "gmx.com", "gmx.de", "web.de", "mail.com", "yandex.com",
    "zoho.com", "eircom.net",
}


def email(value):
    """Lower-cased address, or None if it doesn't look like one."""
    v = (value or "").strip().lower()
    if v.startswith("mailto:"):
        v = v[7:]
    return v if EMAIL_RE.match(v) else None


def email_domain(value):
    e = email(value)
    return e.split("@", 1)[1] if e else None


def company_domain_from_email(value):
    d = email_domain(value)
    return d if d and d not in FREEMAIL else None


def domain_from_url(value):
    v = (value or "").strip().lower()
    if not v:
        return None
    if "://" not in v:
        v = "http://" + v
    try:
        host = urlparse(v).hostname or ""
    except ValueError:
        return None
    host = host[4:] if host.startswith("www.") else host
    return host if "." in host else None


def phone(value, region=None):
    """E.164 (+353...) when the number parses, else the trimmed input (so nothing is lost)."""
    raw = (value or "").strip()
    if not raw:
        return None
    candidate = "+" + raw[2:] if raw.startswith("00") else raw
    try:
        num = phonenumbers.parse(candidate, region or config.DEFAULT_PHONE_REGION)
        if phonenumbers.is_possible_number(num):
            return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        pass
    return raw[:50]


def whatsapp_id(value, region=None):
    """WhatsApp addresses are E.164 digits without the plus sign."""
    p = phone(value, region)
    return p[1:] if p and p.startswith("+") and p[1:].isdigit() else None


def from_whatsapp_id(wa_id):
    digits = re.sub(r"\D", "", wa_id or "")
    return f"+{digits}" if digits else None


def split_name(full):
    first, _, last = (full or "").strip().partition(" ")
    return first or None, last.strip() or None
