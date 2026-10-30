# Campaigns

Automatic email sequences sent to the people enrolled in them.

## Creating a campaign

1. Click **New campaign** and give it a name.
2. Open the **Sequence** tab and write each stage.
    * Stage 1 needs a subject. Later stages can leave it empty to reply in the same thread ("Re: ...").
    * Set the **wait** before each follow-up (days and hours).
    * Click a merge-field chip to insert it. `{{first_name|there}}` uses "there" when the first name is empty. Available: name, company, job title, email, phone, company website and domain, your custom fields and any imported column.
    * **Preview** with a real person and **send a test** to yourself.
3. **Enroll people**: import a sheet into the campaign ([Import](/help/import)), select people on [People](/help/people) and click *Enroll*, or use *Enroll someone*.
4. **Settings** tab (optional): start date and time, its own daily limit, delays, sender name, reply-to, tracking on or off.
5. Click **Start campaign**. The worker sends within your limits and send window.

> Nothing is sent while a campaign is a draft or paused.

## What stops an enrollment automatically

A reply (any channel), an unsubscribe, a bounce, "needs verification", paused sending, the person's do-not-contact switch, or the address being on the [Do not contact](/help/suppression) list.

## Overview tab

A funnel per stage: sent, opened, clicked, replied, failed and waiting. Buttons let you retry failures, send a stage now, or cancel queued sends.

## Per-person changes

Open a person's enrollment ([Enrollments](/help/enrollments)) to change a stage's subject, text or send date for just that person, send a stage now, skip it, re-open it or restart the sequence.

## Good to know

* Campaign statuses: draft, active, paused, archived. Starting, pausing and archiving reach real people, so an API key needs the `send` permission to do them ([API keys & Claude](/help/api-keys)).
* Check scheduled and failed emails in the [Outbox](/help/outbox) and all events in [Email activity](/help/activity).
* Global limits and the send window are in [Sending settings](/help/settings).
