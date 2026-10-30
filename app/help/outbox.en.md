# Outbox

Scheduled, queued and failed campaign emails.

## What it is for

The outbox shows what is waiting to be sent and what went wrong. Open it when you want to know "did that email go out?".

## Tabs

* **Upcoming**: emails scheduled for later.
* **Queued**: emails you asked to send now; the worker picks them up first.
* **Failed**: emails that could not be delivered, with the error.

Filter by search text, campaign and stage.

## Actions

Tick rows, then:

* **Retry**: put failed emails back in the queue (not for bounced or unsubscribed people).
* **Cancel**: take queued emails out of the queue.
* **Skip**: skip that stage for those people.

Click a message to see exactly what was sent or will be sent.

## If nothing is being sent

1. On the [Dashboard](/help/dashboard) *System* box: is sending switched off, is SMTP configured, is the worker alive?
2. Is the campaign **active**? Is it outside the send window or over the daily limit ([Sending settings](/help/settings))?
3. Is the person replied, unsubscribed, bounced, needs verification, paused or do-not-contact? The enrollment page says why ([Enrollments](/help/enrollments)).

## Good to know

Failures retry automatically a few times. After that, use **Retry**. The number of attempts and the pause between them are set in [Sending settings](/help/settings).
