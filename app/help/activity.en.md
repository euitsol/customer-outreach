# Email activity

Every sent, opened, clicked, replied, bounced and unsubscribed event.

## What it is for

A running log of what happened to your campaign emails. Use it to see who engaged and to export results.

## How to use it

1. Filter by search text, campaign, event type, stage and date range (From / To).
2. Click a row to open the person or the email.
3. Use **Export** to download the filtered list as a spreadsheet.

## Good to know

* **Opened** and **clicked** depend on tracking, which can be switched off per campaign or globally ([Sending settings](/help/settings)). Some mail programs block tracking pixels, so opens are approximate. An **Opened** row shows the IP address that loaded the tracking image in its detail column (and in the CSV export). Gmail, Apple Mail and some other programs load images through their own servers, so that address is often the provider's, not the reader's.
* **Source** filter: *Campaigns* are automatic sequence sends; *One-to-one* are messages you (or Claude, or the API) sent from the [Inbox](/help/inbox) or a person's page. By default you see both, so every email and WhatsApp message the app sent is here. One-to-one rows show the status (sent, delivered, read, failed), the mailbox or number, and link to the conversation.
* Replies and bounces are detected from your mailbox (IMAP).
* WhatsApp campaigns show here too (filter **Channel**): *Sent* when Twilio accepts the message, *Delivered* when it reaches the phone, *Opened* when the person reads it (detail "Read on WhatsApp"), *Failed* with the reason when it cannot be delivered, and *Replied* when they answer.
* Unsubscribes are recorded here and also added to [Do not contact](/help/suppression).
