# Import

Bring in a CSV or Excel list of people.

## Steps

1. **Upload** a CSV or Excel file and choose where it goes:
    * **No campaign: just add people to the CRM**, or
    * an existing campaign, or a new one (people are added **and** enrolled).
2. **Map columns.** The importer guesses: Email, First/Last name, Full name, Company, Company website, Job title, Phone, Priority, Tags, Notes, "Send?" (Send / Do not send / Verify), and per-stage subject, body and date (`stage2_body`, "Follow-up 1", ...). Any other column becomes a **merge field** (campaign import) or a **custom value on the person** (CRM-only import).
3. **Preview.** See how many are new, updated or skipped, and every row with a problem (bad email, bad date).
4. **Import.**

## Rules the importer follows

* Existing people (same email) are **updated, never duplicated**.
* **Empty cells never erase data.**
* Stages that were already sent are never changed.
* Title rows above the header and messy sheets are handled.

## Good to know

* **Download example CSV** shows every supported column.
* After a campaign import, review the sequence in [Campaigns](/help/campaigns).
* Imported people appear under [People](/help/people); campaign members under [Enrollments](/help/enrollments).
