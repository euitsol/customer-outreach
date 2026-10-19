"""Builds seeds/ireland_hospitality_resend.csv from the original research sheet.

The first round of emails went out without tracking. This produces a clean, importable CSV
that re-sends them (with open/click tracking) and tidies the text:

  * stage 1 subject -> "Following up: <original subject>"  (a fake "Re:" on a first email looks deceptive
    and hurts deliverability), plus one honest line saying the note is being sent again
  * stray doubled quotes, leading blank lines and uneven bullet lists are fixed
  * follow-ups get a sign-off; send dates are dropped so the stage wait times apply instead

Usage: python seeds/build_ireland_resend.py "<original sheet>.csv" seeds/ireland_hospitality_resend.csv
"""
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")
from app import importer  # noqa: E402

SUBJECT_PREFIX = "Following up: "
RESEND_LINE = ("I emailed you earlier today about this and want to be sure it reached you, "
               "so I'm sending it once more. Apologies for the repeat.")
BULLET_HEAD = re.compile(r"experience in this field:\s*$")


def clean(text):
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace('""', '"').strip()
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    lines, in_list = [], False
    for line in t.split("\n"):
        s = line.strip()
        if BULLET_HEAD.search(s):
            in_list = True
            lines.append(line)
            continue
        if in_list:
            if not s:
                # blank lines inside the list are dropped; the list ends at "Recognised by"
                continue
            if s.startswith("Recognised by"):
                in_list = False
                lines.append("")
                lines.append(line)
                continue
            lines.append("- " + s.lstrip("-•* ").strip())
            continue
        lines.append(line)
    out = "\n".join(lines)
    # one blank line between the intro and the list
    out = re.sub(r"(experience in this field:)\n(?=- )", r"\1\n\n", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def with_signoff(text):
    return text if re.search(r"Best regards,?\s*$", text) else text + "\n\nBest regards,"


def main(src, dst):
    headers, rows = importer.read_table(src, Path(src).read_bytes())
    col = {h: i for i, h in enumerate(headers)}
    get = lambda r, h: (r[col[h]] if h in col else "").strip()

    out_cols = ["email", "first_name", "last_name", "company", "phone", "priority", "send", "tags", "notes",
                "stage1_subject", "stage1_body", "stage2_subject", "stage2_body", "stage2_date",
                "stage3_subject", "stage3_body", "stage3_date",
                "title", "location", "website", "social_account", "offer_group", "what_to_offer", "research"]
    n = 0
    with open(dst, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(out_cols)
        for r in rows:
            email = get(r, "Email").lower()
            if not email:
                continue
            send = get(r, "Send?") or "Send"
            body1 = clean(get(r, "Email 1"))
            m = re.match(r"(Hi [^\n]*,)\n\n(.*)", body1, re.S)
            if m:
                body1 = f"{m.group(1)}\n\n{RESEND_LINE}\n\n{m.group(2)}"
            subject = get(r, "Subject line")
            group = get(r, "Offer group")
            tags = ["ireland", "hospitality", "resend-tracked"]
            if group[:1] in ("A", "B"):
                tags.append(f"offer-{group[0].lower()}")
            w.writerow([
                email, get(r, "First name"), get(r, "Last name"), get(r, "Company"), get(r, "Phone"),
                get(r, "Priority") if get(r, "Priority") != "–" else "", send, ",".join(tags),
                get(r, "Notes / check before sending"),
                (SUBJECT_PREFIX + subject) if subject else "", body1,
                "", with_signoff(clean(get(r, "Follow-up 1"))) if get(r, "Follow-up 1") else "", "",
                "", with_signoff(clean(get(r, "Follow-up 2"))) if get(r, "Follow-up 2") else "", "",
                get(r, "Title"), get(r, "Location"), get(r, "Website"), get(r, "Social account"),
                group if group != "–" else "", get(r, "What to offer") if get(r, "What to offer") != "–" else "",
                get(r, "What we found (research)"),
            ])
            n += 1
    print(f"wrote {n} rows to {dst}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
