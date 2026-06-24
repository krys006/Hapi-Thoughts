SUPABASE MIGRATION NOTES
Feature: PostgreSQL Migration from Local DB to Supabase + Cloud Media Storage
Date implemented: June 2026
Branch: backend-dev

---

WHAT WAS CHANGED

1. Database migrated from local PostgreSQL to Supabase
   Project: kayhqqrnfxgfdxwmizzz
   Region: ap-southeast-1 (Singapore)
   Connection via: Supabase connection pooler (port 5432, direct mode)
   backend/.env updated:
   - DATABASE_URL pointing to Supabase pooler URL

2. Media storage migrated from local filesystem to Supabase Storage
   Bucket name: media
   Bucket visibility: Public
   S3-compatible API used via django-storages + boto3
   backend/.env additions:
   - USE_S3
   - AWS_ACCESS_KEY_ID (Supabase S3 access key, NOT project ref)
   - AWS_SECRET_ACCESS_KEY (Supabase S3 secret key)
   - AWS_STORAGE_BUCKET_NAME
   - AWS_S3_ENDPOINT_URL
   - AWS_S3_REGION_NAME
   - MEDIA_URL

3. hapivet/settings.py — additions
   Replaced hardcoded MEDIA_URL and MEDIA_ROOT block with conditional:
   - If USE_S3=True: uses S3Boto3Storage backend, reads all AWS_* vars from .env
   - If USE_S3 is unset or False: falls back to local /media/ and MEDIA_ROOT
   Added to INSTALLED_APPS: "storages"
   Full S3 settings block:
   - DEFAULT_FILE_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"
   - AWS_S3_FILE_OVERWRITE = False
   - AWS_DEFAULT_ACL = "public-read"
   - MEDIA_URL read from .env with hardcoded default fallback

4. New packages installed
   django-storages — abstracts file storage backends
   boto3 — AWS/S3-compatible client used by django-storages
   Add both to requirements.txt if not already present:
   django-storages
   boto3

---

DATA MIGRATION

Existing local data was exported and loaded into Supabase.

Export command (run against local DB):
   python manage.py dumpdata --natural-foreign --natural-primary \
     --exclude contenttypes --exclude auth.permission \
     -o data_backup.json

Import process:
   Standard loaddata failed due to FK ordering and pooler transaction issues.
   Used a custom shell script to load models in dependency order
   with FK checks temporarily disabled via SET session_replication_role = replica.

Load order used:
   sites.site → accounts.user → account.emailaddress →
   socialaccount.socialapp → socialaccount.socialaccount →
   socialaccount.socialtoken → pets.petowner → pets.pet →
   pets.contactlink → pets.petdeletionrequest →
   appointments.clinicsettings → appointments.blockeddate →
   appointments.appointment → billing.service →
   billing.billingreceipt → billing.billingitem →
   medical.medicalrecord → medical.prescriptionitem →
   medical.vaccination → medical.testresultfile →
   notifications.notification → notifications.notificationpreference →
   health.healtharticle

After import, all table sequences were reset to avoid PK collisions on new inserts:
   Used setval(pg_get_serial_sequence(table, 'id'), MAX(id)) for all tables.
   This is required any time data is loaded with explicit PKs into Postgres.

Existing media files were uploaded to Supabase Storage using upload_media.py
   (script located in backend root, safe to delete after migration)
   One file skipped: pets/สตว.jpg — Supabase S3 rejects non-ASCII filenames.

---

KNOWN ISSUES AND NOTES

1. Supabase S3 access keys are separate from the service role key
   Generate them under: Supabase dashboard → Storage → S3 Access Keys
   The project ref (kayhqqrnfxgfdxwmizzz) is NOT a valid S3 access key ID.

2. SET session_replication_role = replica disables FK checks at session level
   This only works on a direct connection, not the pooler in transaction mode.
   The Django shell uses a persistent connection so the setting holds for the
   duration of the shell session.

3. loaddata is unreliable against Supabase pooler for large fixtures
   It wraps the entire file in one transaction and fails atomically.
   The custom shell import script is the recommended approach for this project.

4. Sequence reset is required after any bulk import with explicit PKs
   If skipped, new user registrations (including Google sign-in) will fail
   with duplicate key violations on accounts_user, account_emailaddress, etc.

5. Non-ASCII filenames are rejected by Supabase S3
   Any file uploaded through the app with a non-ASCII filename will fail silently
   or error. Consider adding filename sanitization on upload (e.g. slugify).

---

PRODUCTION NOTES (when deploying to Render)

1. Add all AWS_* and USE_S3 vars to Render environment variables dashboard
2. Set USE_S3=True in production environment
3. MEDIA_URL should point to the public Supabase Storage URL (already set in .env)
4. Do not set MEDIA_ROOT in production — it is unused when USE_S3=True
5. Run sequence reset after any future bulk data imports to production DB

---

FILES MODIFIED
- hapivet/settings.py (media storage block)
- backend/.env (DATABASE_URL, USE_S3, AWS_* vars, MEDIA_URL)
- requirements.txt (django-storages, boto3)

FILES CREATED
- upload_media.py (one-time migration script, can be deleted)
