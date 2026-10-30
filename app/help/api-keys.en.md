# API keys & Claude

Let Claude and other tools work with your CRM safely.

## What it is for

You can connect Claude (Claude Desktop, Claude Code or claude.ai) to the CRM. Claude can then search, read and update records, log notes, create tasks and deals, enroll people in campaigns and, only if you allow it, send messages. Ask it things like:

* "Give me my CRM briefing for today."
* "Who replied to the Ireland campaign this week, and what do they want?"
* "Create a deal for Sprout & Co, €18k, stage Qualified, and a task to send the proposal Friday."

## Creating a key

1. Go to *Workspace > Users & API keys > API keys & Claude* and click **Create a key**.
2. Name it and choose its permissions:

| Permission | Lets the key... |
|---|---|
| `read` | search and view people, companies, deals, tasks, campaigns, conversations |
| `write` | create and update people, companies, deals, tasks; log notes; merge people; enroll in campaigns |
| `send` | email or WhatsApp people, start, pause or archive campaigns: things that reach real people |

3. Optionally set **Expires after (days)**.
4. **Copy the key now. It is shown only once.**

## Good to know

* Start with `read` + `write`. Add `send` only to a key that should send messages.
* Keys can be revoked at any time; *Last used* shows activity.
* Everything Claude changes appears in the [audit log](/help/audit) as "Claude".
* Connection steps for each Claude app are on the page itself and on [Integrations](/help/integrations).
