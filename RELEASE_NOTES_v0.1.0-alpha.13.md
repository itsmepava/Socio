# Socio v0.1.0-alpha.13

## What's new

- Added Railway deployment configuration for the Flask app, startup database initialization, and a health check.
- Added configurable image storage folder so organization logos and event posters can use Railway's persistent volume.
- Added step-by-step Railway team-preview setup, including persistent SQLite storage, HTTPS settings, SMTP configuration, and initial platform-admin creation.
- Kept Gunicorn out of Windows local installs while including it for hosted Linux deployments.

## Notes

- Railway's current free trial is time-limited and usage-limited. This package uses a single service and a small persistent volume for team review.
- The hosted demo starts with a fresh database. Create the first platform admin through Railway SSH, then create/approve organizations in the app.
- Payments remain simulated. No automated test suite was run for this deployment-preparation update.
