# Start here: the big picture

How the CRM fits together, and where to find each part.

## How it works

The CRM is built around **people**. Everything else hangs off a person:

```
Company ── People ──┬── Emails / phones / WhatsApp numbers   (how to reach them)
                    ├── Timeline: notes, calls, messages, campaign emails
                    ├── Tasks                                 (what to do next)
                    ├── Deals                                 (money you might make)
                    ├── Conversations in the Inbox            (email + WhatsApp threads)
                    └── Enrollments in campaigns              (automatic email sequences)
```

## The left menu

| Menu | What it is for | Guide |
|---|---|---|
| **Dashboard** | Pipeline value, tasks due, unread messages, campaign stats | [Dashboard](/help/dashboard) |
| **Inbox** | Every reply and WhatsApp chat, one thread per person | [Inbox](/help/inbox) |
| **Tasks** | Your follow-ups | [Tasks](/help/tasks) |
| **People / Companies** | Your contacts and accounts | [People](/help/people), [Companies](/help/companies) |
| **Deals** | The sales pipeline | [Deals](/help/deals) |
| **Campaigns, Enrollments, Outbox, Email activity** | Automatic email sequences and what they are doing | [Campaigns](/help/campaigns), [Enrollments](/help/enrollments), [Outbox](/help/outbox), [Email activity](/help/activity) |
| **Import** | Bring in CSV / Excel lists | [Import](/help/import) |
| **Templates, Tags, Do not contact** | Reusable texts, labels, the block list | [Templates](/help/templates), [Tags](/help/tags), [Do not contact](/help/suppression) |
| **Workspace** | Settings, integrations, fields, users, API keys, audit log | [Sending settings](/help/settings), [Integrations](/help/integrations), [Fields & pipelines](/help/fields), [Users](/help/users), [API keys & Claude](/help/api-keys), [Audit log](/help/audit) |

## Three rules that hold everywhere

1. **A reply on any channel stops automatic emails** to that person (an email reply or a WhatsApp message).
2. **Opt-outs are global.** An unsubscribe or "do not contact" blocks every campaign and every channel.
3. **Everything is logged.** Every change, by you, a colleague, the API or Claude, is in the [audit log](/help/audit).

## First-time setup (about 15 minutes)

1. **Sign in** with the admin login from the server's `.env` file.
2. **Create your own user** under *Workspace > Users & API keys*. The first user becomes the owner.
3. **Check sending** under *Sending settings*: send a test email, run the IMAP test, set your delays, daily limit, signature and unsubscribe footer.
4. **Optional:** connect WhatsApp ([Integrations](/help/integrations)), add custom fields and edit your sales stages ([Fields & pipelines](/help/fields)).

## Finding help

Every page has a **Help** button in the bottom-right corner with **English** and **বাংলা** links. They open the guide for the page you are on. The full list is under *Help & guides* in the menu.
