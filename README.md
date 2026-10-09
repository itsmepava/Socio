# Socio · Local demo

An early local prototype for organizations and their members. Socio helps communities share plans and welcome members through one simple membership experience. The demo includes organization approval, plan setup, member applications, membership status, and a simulated payment journey. Payment outcomes and the clock are simulated; no real money or bank connection is involved.

The interface uses a Canvas White, Obsidian Black, and Accent Orange palette, with a neon highlight, system-aware light/dark themes, and reduced-motion support. The selected theme is remembered in the browser.

## Run locally on Windows

For a guided setup that prints progress, opens on port `5001` by default, prepares the local database, and can add local presentation data, run this from PowerShell in the extracted folder:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\start-local.ps1
```

Keep that PowerShell window open while using Socio. If port `5001` is already in use, run `start-local.ps1 -Port 5002`. The script updates `PUBLIC_BASE_URL` to match the selected port. Choose `Y` at the sample-data prompt to create the Colombo Chess Club gallery entry, plans, offers, and an organization admin. It asks you to set that admin's email and password; no default credentials are used. Choose `Y` at the platform-admin prompt only for a fresh database without an existing platform administrator.

To start the app manually instead, use the steps below.

1. Open PowerShell in this folder.
2. Create and activate a Python environment, then install dependencies:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

3. Set a private local secret and database path for this PowerShell session:

   ```powershell
   $env:SECRET_KEY = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
   $env:DATABASE_URL = 'sqlite:///membership.db'
   $env:PUBLIC_BASE_URL = 'http://127.0.0.1:5000'
   $env:DEV_EMAIL_PREVIEW = '1'
   ```

   New member and organization-admin accounts require email confirmation. For local testing without an email provider, set `$env:DEV_EMAIL_PREVIEW = '1'`; Socio prints confirmation links in the Flask server console. This preview option only starts with a localhost `PUBLIC_BASE_URL` and must not be enabled on a public server. To send real email, configure your provider's `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_FROM`, `MAIL_USE_TLS`, and optionally `MAIL_USE_SSL`. Keep SMTP credentials private and out of Git. Confirmation links expire in 24 hours and can only be used once. Platform administrators created with the local CLI are already verified.

   For a local Gmail SMTP trial, set these in the same PowerShell window **before** running `start-local.ps1`:

   ```powershell
   $env:MAIL_SERVER = 'smtp.gmail.com'
   $env:MAIL_PORT = '587'
   $env:MAIL_USERNAME = 'your-account@gmail.com'
   $gmailAppPassword = Read-Host 'Gmail app password' -AsSecureString
   $env:MAIL_PASSWORD = [System.Net.NetworkCredential]::new('', $gmailAppPassword).Password
   Remove-Variable gmailAppPassword
   $env:MAIL_FROM = 'Socio <your-account@gmail.com>'
   $env:MAIL_USE_TLS = '1'
   $env:MAIL_USE_SSL = '0'
   ```

   Gmail SMTP uses TLS on port 587. Google app passwords require 2-Step Verification and are less preferred than modern sign-in methods; use this only for a controlled trial, keep the credential private, and switch to a transactional email provider for consumer traffic. [Google's SMTP setup](https://support.google.com/a/answer/176600) and [app-password guidance](https://support.google.com/accounts/answer/185833) explain account requirements. If SMTP is configured, Socio sends the confirmation email instead of printing a local preview link. For SMS confirmation, Socio still needs an SMS provider, verified phone-number flow, and abuse/cost controls.

4. Create the local tables and your platform administrator:

   ```powershell
   flask --app 'app:create_app' init-db
   flask --app 'app:create_app' seed-demo
   flask --app 'app:create_app' create-admin
   ```

   `seed-demo` securely asks you to choose a local organization-admin email and password, then adds the Colombo Chess Club sample organization, plans, and offers. It only runs against local SQLite with a localhost URL and does not overwrite an existing organization. The platform administrator command securely prompts for its own email and password. There are no default credentials.

   Socio prepares the local SQLite database at startup and adds new demo fields without deleting existing records. You can also rerun `init-db` manually; it is safe for an existing local demo database.

5. Start the app:

   ```powershell
   flask --app 'app:create_app' run
   ```

Open http://127.0.0.1:5000. Use synthetic data only.

## Share a temporary team preview with GitHub Codespaces

Codespaces lets teammates run Socio from the GitHub repository without installing Python on their own computers. It is a development preview: the app stops when the Codespace stops, and its local SQLite database and uploaded images are not durable production storage.

### Update the repository first

1. Upload the contents of this release folder to the root of your GitHub repository, replacing the older app files. Keep the repository structure intact (`app.py`, `requirements.txt`, `templates/`, and `static/` should be at the repository root).
2. Do not upload `.venv`, `.git`, `instance/`, database files, `.env`, or any secrets.
3. Commit the update to the `main` branch.

### Start Socio in a new Codespace

Create a new Codespace from the repository's `main` branch after the commit finishes. An existing Codespace keeps its own checkout and can still show the older app; either pull the new commit there or create a fresh Codespace.

In the Codespaces terminal, run:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export DATABASE_URL=sqlite:///membership.db
export PUBLIC_BASE_URL=http://127.0.0.1:5001
export DEV_EMAIL_PREVIEW=1
export SHOW_PLATFORM_ADMIN_LINK=1
python -m flask --app 'app:create_app' init-db
python -m flask --app 'app:create_app' seed-demo
python -m flask --app 'app:create_app' create-admin
python -m flask --app 'app:create_app' run --host 0.0.0.0 --port 5001
```

The setup commands for demo data and the platform administrator are for a fresh database. If you already created them in that Codespace, skip those commands. Choose credentials when prompted; Socio has no default admin password. Keep the terminal running. Open the **Ports** tab, set port `5001` visibility to **Public** only if your team needs to access it, and share the forwarded HTTPS URL. Anyone with that URL can reach the app, so use test data and strong account passwords.

`DEV_EMAIL_PREVIEW=1` prints email-confirmation links to the server terminal. Those links use localhost and work only in that Codespace. To let teammates confirm new accounts through email, configure SMTP and set `PUBLIC_BASE_URL` to the forwarded HTTPS URL, then set `DEV_EMAIL_PREVIEW=0`. Keep SMTP credentials and `SECRET_KEY` private. For a public preview, set `SESSION_COOKIE_SECURE=1`.

A Codespace is designed for development and team testing, not always-on consumer hosting. Its local database and uploaded images can be lost when the Codespace is deleted. Use synthetic data and simulated payments only.

## Try the end-to-end local demo

1. In the local setup, the platform-admin sign-in shortcut is visible in the navigation and home page for development. It opens `http://127.0.0.1:5001/platform-admin` (or the port you selected). This shortcut is enabled by `start-local.ps1`; it is off by default in the app configuration and should remain off on a public deployment. Admin pages still require an authenticated platform-admin account.
2. Register an organization from the home page, then approve it from the platform dashboard.
3. Sign in as the organization administrator, create one or more plans, and open the public organization page.
4. From **For members**, choose an organization and apply for a plan. Use the same member email and password to join more organizations; your member sign-in can manage all memberships under that account. New member passwords must contain at least 12 characters. Member registration and organization administrator registration also include an optional gender field.
5. The member gallery at **For members** supports search by organization, plan, benefit, current offer, and upcoming event. Open an organization to preview its plans, active offers, and upcoming events. Click an event or offer card to read its full details. Organization administrators can publish offers and schedule them by simulated day; offers do not change the simulated checkout price.
6. Before payment, review the organization, plan, simulated amount, billing period, expected simulated start/end, and cancellation terms. Confirm the acknowledgement, then on the simulated bank page choose success, failure, delayed confirmation, duplicate confirmation, or abandon. These options never make a real payment.
7. Return to the platform dashboard to review members and payments. Deliver delayed confirmations there. Advance the clock by 7 or 15 days to observe reminders and renewals.
8. From the member page, view status and payment history, try an upgrade, schedule a lower-priced plan for the next period, or cancel at the end of the paid period.
9. On the organization page, search members by name or email, export the matching rows as CSV, and record an offline payment for a pending or expired membership. Offline records are unverified demo entries; they do not confirm receipt of money.
10. An organization can request a full refund for a paid simulated checkout. A platform administrator can approve or decline it, then download the reconciliation CSV. These decisions update only the demo ledger.
11. On the organization workspace, download the roster CSV template and upload a UTF-8 CSV with `full_name,email,plan_name` columns. The import is all-or-nothing and supports up to 500 rows. Copy each one-time invitation link from the results page and share it with its matching member. The member sets their password, then confirms their email before they can sign in. Links expire after 7 simulated days.
12. Read **About Socio** from the public navigation for a short product overview. Organization administrators can set applications to require review, ask for recommendation/supporting details, and add applicant instructions. Applications must be approved before payment; a member invited directly by the organization is treated as pre-approved. Schools and other organizations must verify student/alumni eligibility themselves in this demo; there is no school-directory or identity verification integration yet.
13. Organization administrators can publish dated events with optional poster images. Upcoming events appear in the member gallery and on the organization's public page; members can open each event to see its full details. Archived or past events are hidden from member discovery.
14. Organization administrators can optionally enable a one-time loyalty gift. Choose an active-membership milestone (30–365 days) and a no-charge extension (7–60 days). The simulator grants the extra days when an active, non-cancelling member reaches the milestone.

The **Reset demo** control removes organizations, organization accounts, members, plans, and simulated payments, and returns the simulated clock to day zero. It preserves platform administrator accounts and requires explicit confirmation.

## Current milestone

The prototype now covers the local demo flow for platform administrators, organization administrators, and members. It includes a product About page, organization logos uploaded by organization administrators and displayed in the gallery and public profiles, organization-controlled application review, optional recommendation/supporting details, applicant instructions, account sign-in, email confirmation through local preview or configurable SMTP, organization search/gallery, plan previews, informational scheduled offers, event publishing with optional poster images, optional one-time loyalty membership-day gifts, upgrades, scheduled downgrades, period-end cancellation, renewal reminders, simulated renewal invoices, delayed confirmations, organization member search and CSV export, manually recorded offline-payment demo entries, simulated full-refund review, reconciliation summaries and CSV export, member roster import with one-time invitations, role-specific welcome screens, an installable PWA shell with static-only caching, and a simulated clock. Membership periods are 30 simulated days for monthly plans and 365 days for annual plans. Renewal invoices are prompts for a member to confirm a simulated payment; the app does not charge automatically. Offline entries are unverified records only and do not prove that a transfer or cash payment was received. Refund decisions do not return money. Recommendation letters are collected as text in this prototype; private file upload, malware scanning, access auditing, and retention controls remain future work.

The sign-in flow opens a role-specific welcome screen. A member account can have memberships in multiple organizations and switch between them from the member workspace. Existing local SQLite databases are upgraded non-destructively. Organization administrators and members can optionally provide gender when they register; organization administrators also enter their name. Organizations can import a UTF-8 CSV roster (`full_name,email,plan_name`, up to 500 rows/2 MB). Socio validates the entire file before creating invitations. Each invitation contains a one-time bearer link; members can use an existing Socio account or create a new password, and the link expires after 7 simulated days. Email-confirmation tokens are single-use, hashed at rest, and expire after 24 hours. Organization administrators can revoke unclaimed invitations. Authenticated pages are marked non-cacheable; invitation URLs use the configured `PUBLIC_BASE_URL`, and organization registration is rate-limited. Organization review is a manual decision in this demo and does not independently validate school enrollment, alumni status, recommendation authorship, or other eligibility claims. The welcome flow, invitations, roster import, and SMTP delivery are local-demo functionality.

Real refunds, payouts and bank reconciliation, email/SMS beyond configurable SMTP confirmation, and HNB integration are not implemented. The prototype is not ready for real customers or payments.

## Remaining production work

Before a consumer launch, add and maintain versioned database migrations, use a supported production database and managed object storage before scaling beyond one app instance, configure tested database backups and recovery, review every tenant-scoped permission, add account recovery and MFA for platform administrators, and complete security, operational, privacy, and recovery reviews. If collecting recommendation letters as files, use private object storage, strict authorization, malware scanning, audit logs, and clear retention/deletion policies. Replace the simulated payment flow only after confirming HNB's merchant and settlement model. No card data should pass through this application.
