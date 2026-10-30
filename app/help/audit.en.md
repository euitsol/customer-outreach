# Audit log

Who changed what, and when.

## What it is for

Every change made by a user, by the API, by Claude or by the system is recorded. Use the log to find out how a record got into its current state.

## How to use it

1. Filter by **Record type** (person, company, deal, ...), **Done by** (user, API, Claude, system) and **Record id**.
2. Each row shows the time, who did it, the action and the details of what changed.
3. The list shows the latest 50 entries per page, newest first.

## Good to know

* The audit log is read-only.
* Deletes and merges cannot be undone from the app; the log shows who did them, and an admin can restore a backup if it was serious.
* Changes made through Claude are labelled "Claude" ([API keys & Claude](/help/api-keys)).
