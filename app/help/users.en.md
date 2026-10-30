# Users

The people who sign in to the CRM.

## Roles

| Role | Can do |
|---|---|
| **Owner** | Everything, including managing users |
| **Admin** | Manage users, API keys, custom fields and pipelines |
| **Member** | Work with records; cannot change those settings |

## How to use it

1. **Add a user**: enter a name, email, role and a password of at least 10 characters. The very first user becomes the **owner**.
2. Sign out and back in with that email. Your name now appears on notes and records you create, and records can be assigned to you.
3. Admins can reset a password, change a role or disable a user's access.
4. Your own name and password are under your profile (click your name at the bottom of the left menu).

## Good to know

* The admin login from the server's `.env` file is only for the first set-up. Create personal users and use those.
* Everyone's actions are recorded in the [audit log](/help/audit).
* Keys for Claude and other tools are separate: [API keys & Claude](/help/api-keys).
