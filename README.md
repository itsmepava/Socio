# Membership Platform V1 — M1 foundation

Local prototype foundation for organizations, accounts, roles, plans, and basic administrative activity logging. Payments and member-facing membership flows are not implemented in this milestone.

## Run locally (PowerShell)

1. Open PowerShell in this folder.
2. Create and activate a private Python environment:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

3. Set a fresh local secret and database location for this PowerShell session:

   ```powershell
   $env:SECRET_KEY = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
   $env:DATABASE_URL = 'sqlite:///membership.db'
   ```

4. Initialize the local database and create the platform administrator interactively:

   ```powershell
   flask --app 'app:create_app' init-db
   flask --app 'app:create_app' create-admin
   ```

5. Start the application:

   ```powershell
   flask --app 'app:create_app' run
   ```

Open http://127.0.0.1:5000. No preconfigured credentials are included. `.env.example` is a placeholder reference and is not loaded automatically. Organization administrators can register, but need platform approval before signing in or managing plans. If a login email is used in multiple organizations, sign in with that organization's contact email too.

## Tests

```powershell
pytest
```

The prototype is for localhost and synthetic data only. It does not process real payments. Before any hosted deployment, move to HTTPS, enable secure cookies, configure production secrets and database, apply schema migrations, review rate-limit storage, and perform the production security work described in the approved plan.
