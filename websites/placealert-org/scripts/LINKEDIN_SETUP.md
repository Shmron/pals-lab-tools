# LinkedIn → website news auto-sync

This makes the **Media / Latest News** section on the site update automatically
from the Place Alert Labs LinkedIn company page.

**How it works**

```
You post on LinkedIn
        │
        ▼
linkedin_sync.py  (hourly cron)  ──calls──►  LinkedIn API
        │
        ▼
/var/www/website/data/news.json   (rewritten atomically)
        │
        ▼
assets/main.js fetches news.json live on every page load → visitors see it
```

The pipeline is **already installed**. It stays dormant and leaves the current
news untouched until you provide credentials. Do the one-time setup below.

---

## One-time setup

### 1. Create a LinkedIn Developer app
1. Go to <https://www.linkedin.com/developers/apps> → **Create app**.
2. Associate it with the **Place Alert Labs** company page (you must be a Page admin).
3. Note the **Client ID** and **Client Secret** (Auth tab).

### 2. Request the required product / permission
- On the app's **Products** tab, request **Community Management API**
  (this grants the `r_organization_social` scope needed to read Page posts).
- Approval can take time; LinkedIn may ask for a use-case description.
  Use: *"Display our own organization's posts on our official website."*

### 3. Generate an access token (with org admin consent)
- Easiest: use LinkedIn's **OAuth token generator** (Auth tab → "Token generator")
  or the 3-legged OAuth flow, granting scope `r_organization_social`.
- Copy the **access token** and, if shown, the **refresh token**.
  (Access tokens last ~60 days; the refresh token lets this script renew it.)

### 4. Find your numeric Organization ID
- Open your Page admin view; the URL contains `.../company/<ID>/admin/`, or
- Superadmin → Settings shows the Organization ID. It is the number in
  `urn:li:organization:<ID>`.

### 5. Put the values in the credentials file
```bash
cd /var/www/website/data
cp .linkedin.env.example .linkedin.env
chmod 600 .linkedin.env
nano .linkedin.env      # paste ACCESS_TOKEN, ORG_ID, and (ideally) CLIENT_ID/SECRET/REFRESH_TOKEN
```

### 6. Test it
```bash
python3 /var/www/website/scripts/linkedin_sync.py
```
- Success prints `Wrote N post(s) to news.json.` — reload the Media page to see them.
- Any error prints a reason and **leaves the old news.json intact** (site stays fine).

---

## Automatic hourly updates
A cron job at `/etc/cron.d/pals-linkedin-sync` runs the script every hour as
user `rutendo` and logs to `/var/www/website/scripts/linkedin_sync.log`.
No further action needed once step 5 is done.

## Notes / troubleshooting
- **401 Unauthorized** → token expired. If refresh creds are set it auto-renews;
  otherwise regenerate the token (step 3) and update `.linkedin.env`.
- **403 / permission** → the Community Management API product isn't approved yet.
- **Version errors** → set `LINKEDIN_API_VERSION` in `.linkedin.env` to the
  current `YYYYMM` value from LinkedIn's API docs.
- The script only publishes posts that have text (a caption); pure-image posts
  with no caption are skipped.
- `.linkedin.env` holds secrets and is **git-ignored** — never commit it.
