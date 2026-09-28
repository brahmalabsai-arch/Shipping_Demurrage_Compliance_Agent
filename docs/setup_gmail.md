# Gmail setup — real review emails (one-time, ~15 minutes)

Until this is done the system uses the local **outbox** backend: every review email is written
as an `.eml` file under `outbox/` (git-ignored) and nothing leaves the machine. Follow these
steps to send real email through the Gmail API instead (architecture §7).

- **Sending account:** `brahmalabs.ai@gmail.com` (the account you authorize below).
- **Reviewer (recipient):** `REVIEWER_EMAIL` in `.env` — currently `4x2kms@gmail.com`.
- **Permission requested:** `gmail.send` only — the app can send mail as the account, never
  read, modify or delete it.

Google renames console menus from time to time; if a label below differs, look for the
equivalent under **APIs & Services** or **Google Auth Platform**.

## 1. Create a Google Cloud project and enable the Gmail API

1. Open <https://console.cloud.google.com/> signed in as `brahmalabs.ai@gmail.com`.
2. Project picker → **New project** → name it e.g. `customs-copilot` → **Create**, then select it.
3. **APIs & Services → Library** → search **Gmail API** → **Enable**.

## 2. Configure the OAuth consent screen

1. **Google Auth Platform** (or **APIs & Services → OAuth consent screen**) → **Get started**.
2. App name `Customs Copilot`; user support email `brahmalabs.ai@gmail.com` → Next.
3. Audience: **External** → Next. Contact email: `brahmalabs.ai@gmail.com` → Create.
4. **Audience → Test users → Add users** → add `brahmalabs.ai@gmail.com`. Leave the app in
   **Testing** (no Google verification needed).
5. **Data access → Add or remove scopes** → add
   `https://www.googleapis.com/auth/gmail.send` → Update → Save.

## 3. Create the OAuth client and save it

1. **Clients** (or **APIs & Services → Credentials**) → **Create client** → application type
   **Desktop app** → name `customs-copilot-local` → **Create**.
2. **Download JSON** and save it as `secrets/credentials.json` in the project root
   (`secrets/` is git-ignored — never commit it).

## 4. Authorize once (opens your browser)

```powershell
.\.venv\Scripts\python.exe -m integrations.gmail_client authorize
```

Sign in as `brahmalabs.ai@gmail.com`. Google warns that the app is unverified — expected for an
app in Testing: **Continue**, then allow **Send email on your behalf**. The refresh token is
saved to `secrets/token.json` and refreshed automatically from then on.

## 5. Switch the backend and send a test

In `.env`:

```
EMAIL_BACKEND=gmail
```

Then:

```powershell
.\.venv\Scripts\python.exe -m integrations.gmail_client send-test   # mails REVIEWER_EMAIL
```

## 6. End-to-end check (Phase 6 exit criterion)

1. Dashboard → **Customs Bulletin → Edit**: change a rule's text (e.g. a duty rate) → Publish.
2. **Scans & changes → Run scheduled scan now** (or
   `.\.venv\Scripts\python.exe -m scheduler.watch_scheduler --once`).
3. Expect: a pending proposal on **Compliance → Pending review**, a review email at
   `REVIEWER_EMAIL`, and `bulletin_change.detected` + `notification.review_email_sent` rows in
   `audit.events`. Running the scan again without another edit sends nothing.

Note: the first scan after setup only records a baseline if no scan has run before — edit the
bulletin *after* a first scan to see a change detected.

## Troubleshooting

- **`Gmail is not authorized yet` / `token is invalid or revoked`** — run step 4 again.
  Apps in *Testing* get refresh tokens that Google expires after **7 days**, so expect to
  re-authorize weekly (publishing the app removes this, but needs Google verification for
  the Gmail scope — unnecessary for this demo).
- **`OAuth client file not found`** — the JSON must be at `GMAIL_CREDENTIALS_PATH`
  (default `secrets/credentials.json`).
- **`access_denied` during consent** — the signing-in account is not a test user (step 2.4).
- **Revoke access** any time at <https://myaccount.google.com/permissions>, then delete
  `secrets/token.json`.
- A failed send never loses the detected change: the proposal stays pending on the Compliance
  page and the failure is recorded as `regulatory_watch.email_failed` in `audit.events`.
