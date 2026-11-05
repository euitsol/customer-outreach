# Mailboxes

The email accounts the CRM sends from and reads replies from. Add as many as you need.

## Why more than one

* Spread a large campaign over several mailboxes so none of them sends too much in a day.
* Send different campaigns from different brands, for example one mailbox per website.
* Each mailbox has its own optional daily limit, so a new mailbox can be warmed up slowly.

## The table

| Column | Meaning |
|---|---|
| **Mailbox** | Name and From address |
| **Sending** | The outgoing (SMTP) server, or *not set up* when something is missing |
| **Replies** | The incoming (IMAP) server and the result of the last check. *not read* means replies to this mailbox are not detected |
| **Today** | Campaign emails sent from it since midnight, and its daily limit |
| **Used by** | The campaigns that have ticked it |
| **Status** | Active, Disabled, or Incomplete (a server, login or password is missing) |

The first row, marked *(.env)*, is the account from the server's `.env` file. Its server and password cannot be changed here; you can rename it, give it a limit or disable it.

## Adding a mailbox

Fill in the **Add a mailbox** form: a name, the From address, the outgoing server and port (465 = SSL, 587 = STARTTLS), the password, and optionally the incoming server (IMAP, port 993) so replies and bounces are detected. Leave the login blank to use the email address, and leave the IMAP login and password blank to reuse the SMTP ones.

The password is stored in the CRM database and is never shown again. Leave the password blank when editing to keep the stored one. Only owners and admins can add or change mailboxes.

Open **Edit & test** on a row to send a test email, test the IMAP login, change the settings, or delete it. A mailbox that a campaign uses cannot be deleted: remove it from the campaign or disable it.

## Choosing mailboxes for a campaign

Open the campaign, then **Settings > Send from**, and tick the mailboxes it may use.

* Nothing ticked: the campaign sends from the first active mailbox only. Adding a new mailbox therefore never changes a running campaign.
* Several ticked: each email goes out from the ticked mailbox that has sent the fewest emails today. Follow-ups to a person stay on the mailbox that wrote to them first, so the conversation looks like one thread.
* If every ticked mailbox has reached its daily limit, the campaign waits until after midnight. If none of the ticked mailboxes is active, it does not send and the Settings tab says so.

## One-to-one emails

When you write to someone from [People](/help/people) or the [Inbox](/help/inbox), the email goes out from the mailbox that last wrote in that thread. When you have more than one mailbox, a **Send from** list lets you choose another.

## Good to know

* Mail between your own mailboxes is ignored by the reply checker.
* Replies and bounces are read from every active mailbox with IMAP set up. If one mailbox cannot be reached, the others are still checked.
* The global daily limit ([Sending settings](/help/settings)) and each campaign's own limit still apply on top of the mailbox limits.
