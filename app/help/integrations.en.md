# Integrations

The status of every connection: email, WhatsApp, Claude and the REST API.

## Cards

* **Email (SMTP + IMAP)**: shows Connected or Not configured. Server settings come from the `.env` file; test them in [Sending settings](/help/settings).
* **WhatsApp Business**: shows Connected or Not connected. The card has **How to connect**, the webhook URL to paste into Meta, and a test send. An admin sets four values in `.env`.
* **Claude (MCP server)**: how many API keys are active and how to connect. See [API keys & Claude](/help/api-keys).
* **REST API**: the base address and a link to the key page.

## Below the cards

* **Channel accounts**: one row per connected channel.
* **Recent webhook deliveries**: the latest messages and status updates received from WhatsApp, with errors. Check here when a WhatsApp message was refused or did not arrive.

## Good to know

WhatsApp free text works only within 24 hours of the person's last message; otherwise use an approved template ([Templates](/help/templates), [Inbox](/help/inbox)).
