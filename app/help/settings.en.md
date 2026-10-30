# Sending settings

How fast, when and as whom the CRM sends campaign email.

## Sections

| Section | What you set |
|---|---|
| **Sending** | The master switch: pause all sending |
| **Pace** | A random wait (minimum to maximum seconds) after every email, the daily limit across all campaigns (counted from midnight in your timezone), and how often the worker checks for work and replies |
| **Send window** | "Only send during these hours": From / Until, optionally Monday to Friday only, and the timezone. Emails due outside the window wait for the next opening |
| **Failures & retries** | Attempts per email before it needs a manual retry, and the pause between attempts in minutes (multiplied by the attempt number: 30, 60, 90...) |
| **Sender identity & footer** | Sender display name, optional Reply-To address, signature, the unsubscribe footer (recommended; GDPR/PECR require it for marketing email), footer text, postal address, and whether to track opens and link clicks |
| **Connections** | Shows SMTP, IMAP, tracking URL, worker and reply-checker status, with a test email and an IMAP login test. Credentials live in the server's `.env` file and are never shown |

## Testing your connection

1. **Send a test email through SMTP**: type your address and click *Send* to prove that sending works.
2. **Test IMAP login**: confirms replies and bounces will be detected.

## Good to know

* **Pause all sending** stops every campaign at once. The menu shows a red "paused" badge while it is on.
* Rejected addresses are not retried; they are marked bounced. If the mail server itself is unreachable, the worker waits 5 minutes and tries again without using up attempts.
* Individual campaigns can have their own daily limit and delays ([Campaigns](/help/campaigns)).
* New mailboxes should stay low (20-50 a day) and ramp up. A low daily limit and random delays protect your reputation.
* A one-click `List-Unsubscribe` header is always added. Unsubscribes land in [Do not contact](/help/suppression).
