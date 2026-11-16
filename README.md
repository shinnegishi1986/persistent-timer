# Persistent Timers & Journal

A Streamlit application for persistent timers, reusable timer templates, parent–child timer organization, journals, custom alarm sounds, and quit-history analytics.

The interface includes English and Japanese labels. Timer schedules, user data, uploaded files, and settings are stored in SQLite.

## Features

### Persistent timers

Create timers using:

- Hours
- Days
- Weeks
- Calendar months
- Calendar years
- A specific date and time in Japan Standard Time

The specified-date option provides a calendar date picker and separate hour/minute inputs.

Timers use stored start and end timestamps rather than a browser-only countdown. Closing the browser does not pause or reset a timer.

When you open the same hosted application on another device and log in to the same account, your saved timers show their current progress.

### Timer display

Each timer card includes:

- Title and description
- Current status
- Start and scheduled end timestamps
- Elapsed percentage
- Elapsed time
- Remaining time
- Scheduled duration
- Animated progress bar
- Animated elapsed-versus-remaining donut chart
- Assigned alarm sound
- Sharing and deletion controls

Timer animations run in the browser. Database changes are periodically refreshed, so changes from another device may not appear immediately.

Timer statistics follow the selected Streamlit light or dark theme. Each timer can have independently selected progress-bar and donut colors, or use the same color for both.

### Templates and parent–child timers

Every saved timer can be reused as a template.

- New creates a timer from empty inputs.
- Reuse creates a new timer using a saved timer’s settings.
- Edit updates the selected existing timer.

Reusing a timer does not duplicate its children.

Timers can be organized into parent–child relationships. For example:

```text
One-week diet plan
├── Workout session
├── Meal preparation
└── Evening walk
```

Parent and child timers run independently. Starting, finishing, or quitting a parent does not automatically change its children.

### Custom alarm sounds

Each user has a private sound library with:

- Audio uploads
- Titles and descriptions
- Audio preview
- Metadata editing
- Optional audio-file replacement
- Confirmed deletion

Supported upload extensions:

```text
.mp3 .wav .ogg .m4a .aac .flac .webm
```

The maximum audio-file size is 10 MiB. Playback support depends on the browser and audio format.

Audio files are stored as SQLite BLOBs. A timer can use a custom sound or the default generated chime.

Deleting a custom sound removes its timer assignments. Affected timers fall back to the default chime.

### Sound preferences

The “Enable sound” preference is stored per user in the database.

Changing it on one device updates the preference used by other devices connected to the same application and account.

This setting does not grant browser audio permission. Automatic playback may still be blocked, and the app must remain open and connected to play completion sounds.

### Journals

Write journal articles linked to:

- Past timers
- Current timers
- Future timers
- No timer

Articles support image and file attachments. Attachments are stored directly in SQLite as BLOBs.

The journal displays 20 posts per page and supports filtering by linked timer.

Attachment limits:

| Limit | Size |
|---|---:|
| Individual file | 10 MiB |
| Combined attachments per article | 30 MiB |

Deleting an article also deletes its attachment BLOBs.

### Quit tracking and dashboard

Manually stop a running timer to record:

- Quit timestamp
- Elapsed duration
- Original scheduled duration
- Optional reason
- A snapshot of the timer’s title and schedule

Quit timers display frozen progress and do not trigger their scheduled completion alarm.

The dashboard includes:

- Quit counts by weekday
- Quit counts by hour, from 00 to 23 JST
- Mean elapsed duration before quitting
- Sample-standard-deviation error bars
- Individual quit history
- Date and title filters
- CSV export of filtered history

Standard deviation uses `ddof=1`. Groups with only one event do not display an SD error bar.

These charts describe recorded quit events, not the probability of quitting. Groups may include timers with different intended durations.

### Sharing and embedding

Timer owners can create public, read-only links.

A person with a valid link does not need an account to view the shared timer’s:

- Title and description
- Start and scheduled end
- Elapsed and remaining time
- Progress charts
- Quit status

Public pages do not provide editing, deletion, journal access, attachment downloads, or custom-audio playback.

Owners can:

- Generate a share link
- Copy iframe embed HTML
- Replace an existing link
- Revoke public access

Replacing a link invalidates the previous one. Sharing is disabled until a link is explicitly created.

The app automatically retrieves the deployed URL for link generation. `TIMER_PUBLIC_BASE_URL` can override that URL when necessary.

### Accounts and administration

Authentication features include:

- Username/password login
- Invitation-code registration
- Persistent browser login
- Password changes
- Logout
- Administrator-issued invitation codes
- Administrator-created user accounts

The administrator can configure invitation usage limits and expiration.

New accounts created by the administrator must change their temporary password before continuing.

## Requirements

- A Python version supported by your installed Streamlit release
- A current Streamlit release exposing `st.context.url`
- SQLite
- A persistent writable data directory for durable storage

The application uses:

```text
streamlit
streamlit-cookies-controller
python-dateutil
pandas
altair
Pillow
```

A suitable starting `requirements.txt` is:

```text
streamlit
streamlit-cookies-controller
python-dateutil>=2.9
pandas
altair
Pillow
```

After testing, pin dependency versions for reproducible deployments.

## Project structure

```text
.
├── app.py
├── requirements.txt
├── README.md
├── timer_theme_component/
│   └── index.html
├── data/
│   └── timer_app.sqlite3
├── scripts/
│   └── ...
└── script/
    └── ...
```

The `timer_theme_component` directory is required for theme-aware animated timer statistics.

The `scripts` and `script` directories may contain the incremental patch utilities used to build the application. Once the application has been patched, those utilities are not required for normal operation.

Do not reapply older patch scripts indiscriminately: some later scripts replace functions introduced by earlier scripts.

## Local installation

Create and activate a virtual environment:

```bash
python -m venv .venv
```

macOS / Linux:

```bash
source .venv/bin/activate
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

For local HTTP development, disable the Secure cookie requirement.

macOS / Linux:

```bash
export TIMER_COOKIE_SECURE=0
streamlit run app.py
```

Windows PowerShell:

```powershell
$env:TIMER_COOKIE_SECURE = "0"
streamlit run app.py
```

Open the URL printed by Streamlit.

Do not disable Secure cookies for a public production deployment.

## Initial administrator account

For a newly initialized database, the default administrator credentials are:

```text
Username: timer-admin
Password: tadmin123@
```

The application requires a password change before continuing.

Set a different bootstrap password before first startup:

```bash
export TIMER_ADMIN_INITIAL_PASSWORD='replace-with-a-strong-password'
streamlit run app.py
```

This environment variable affects initial account creation only. It does not reset the password of an administrator already stored in the database.

## Configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `TIMER_DATA_DIR` | `./data` | Directory containing the SQLite database |
| `TIMER_ADMIN_INITIAL_PASSWORD` | `tadmin123@` | Bootstrap password for a newly created administrator |
| `TIMER_COOKIE_SECURE` | `1` | Require HTTPS for persistent-login cookies |
| `TIMER_COOKIE_NAME` | `persistent_timer_login_v1` | Persistent-login cookie name |
| `TIMER_PUBLIC_BASE_URL` | Automatically detected | Optional canonical URL for public timer links |

Example production configuration:

```bash
export TIMER_DATA_DIR=/persistent-data/timer-app
export TIMER_COOKIE_SECURE=1
export TIMER_PUBLIC_BASE_URL=https://timers.example.com/
streamlit run app.py
```

If deploying under a URL subpath, include it in the canonical URL:

```text
https://example.com/timer-app/
```

## Basic usage

### Create a timer

1. Log in.
2. Open **Create & edit**.
3. Select the New tab.
4. Enter a title and optional description.
5. Select an optional parent.
6. Set the start date and time.
7. Choose a duration or an exact end date/time.
8. Select an alarm sound.
9. Choose chart colors.
10. Save the timer.

Successful creation clears the New inputs for the next record.

### Edit a timer

1. Open the Edit tab.
2. Select the saved timer.
3. Change its settings.
4. Press Update timer.

Editing updates the existing database record. Changing an alarm sound does not intentionally create a new timer.

### Reuse a timer

1. Open the Reuse template tab.
2. Select a saved timer.
3. Adjust its schedule and other settings.
4. Create the timer.

Reuse intentionally creates a separate timer with its own ID.

### Add custom audio

1. Open Alarm sounds in the sidebar.
2. Select New sound.
3. Upload an audio file.
4. Preview it.
5. Enter its title and description.
6. Save.
7. Assign it to a timer in the timer editor.

### Write a journal article

1. Open Journal.
2. Expand the article editor.
3. Enter a title and body.
4. Optionally link a timer.
5. Optionally upload images or files.
6. Publish.

Successful publication clears the article inputs.

### Quit a timer

1. Open a currently running timer.
2. Expand its Stop / quit control.
3. Optionally enter a reason.
4. Stop and record the quit.

Only running timers can be quit. Naturally completed timers are not counted as quit events.

### Share a timer

1. Open Share / Embed on a timer.
2. Confirm public read-only access.
3. Create a link.
4. Copy the link or iframe HTML.

The full generated URL is retained in the owner’s current browser session. After that session is lost, issue a replacement link to obtain a new URL.

The database stores only the share token’s hash, so the original token cannot be recovered from it.

## Time handling

All user-facing scheduling and analytics use `Asia/Tokyo`.

Timestamps are stored with timezone information and converted to UTC for calculations.

### Exact end times

A selected date and time represents an exact endpoint.

For example:

```text
Date: 2026-11-20
Hour: 18
Minute: 30
```

The timer ends at:

```text
2026-11-20 18:30 JST
```

### Months and years

Month and year durations use calendar arithmetic rather than fixed numbers of seconds.

### Legacy date-only values

Older date-only template values remain supported. Existing timer end timestamps are not automatically rewritten by the UI patches.

## Persistence and multiple devices

For cross-device access, every device must connect to the same hosted application and database.

```text
Laptop ──┐
         ├── Hosted Streamlit application ── SQLite database
Phone ───┘
```

Separate local installations with separate SQLite files do not synchronize automatically.

### Stored in the database

- Users and password hashes
- Invitation records
- Timer schedules and hierarchy
- Custom audio BLOBs
- Timer-to-sound assignments
- Journal articles and attachment BLOBs
- Quit history
- Sound-enabled preferences
- Chart-color preferences
- Persistent-login token hashes
- Public-share token hashes

### Browser-session-specific state

Examples include:

- UI selections
- Prepared export snapshots
- Alarm notification deduplication
- Pending notification lists
- Newly generated share-token URLs

Persistent login restores authentication, not all prior UI state.

## CSV import and export

CSV exports use UTF-8 with BOM for Japanese spreadsheet compatibility.

### Timers

Timer CSV includes:

- IDs
- Parent relationships
- Titles and descriptions
- Start and end timestamps
- Duration settings

### Journals

Journal CSV includes article data and attachments encoded as JSON/base64.

### Import behavior

Imports create new records rather than updating existing records.

For a combined timer/journal restoration:

1. Import the timer CSV.
2. Import the journal CSV during the same login session.

The application uses an in-session mapping to reconnect imported journal articles to newly imported timers.

Limits:

| Import limit | Value |
|---|---:|
| CSV file size | 50 MiB |
| Records per import | 1,000 |

### CSV limitations

The existing timer and journal CSV formats do not provide a complete application backup.

They do not include all account data, custom sounds, timer sound assignments, chart colors, sharing configuration, persistent sessions, or sound preferences.

Quit history has a separate dashboard export.

User text is preserved in exports. Spreadsheet programs may interpret some values as formulas, so review untrusted content before opening exported files in a spreadsheet.

## Deletion behavior

Deletion requires confirmation.

| Deleted item | Result |
|---|---|
| Timer | Removes the timer, its sound assignment, visual preferences, public link, and quit history |
| Parent timer | Child timers remain and become parentless |
| Timer linked to journals | Journal articles remain but become unlinked |
| Custom sound | Removes the audio BLOB and its assignments; affected timers use the default chime |
| Journal article | Removes the article and all attached image/file BLOBs |

Deletion cannot be undone through the application.

Existing duplicate timer records are not automatically merged. Separate timer IDs remain separate even if their titles match.

## Audio notification limitations

Database persistence is not the same as a background alarm service.

Audio notifications require:

- The app to be open
- An active connection
- The sound preference to be enabled
- A browser that permits playback

Additional behavior:

- Device sleep and browser background throttling may delay alerts.
- Closing the app prevents it from playing an alarm on that device.
- A timer already finished when sound becomes enabled does not produce a catch-up alarm.
- Multiple connected devices may each play an alarm.
- When several timers finish in the same notification check, the current implementation plays the latest completed timer’s sound.
- Public shared/embed pages do not play completion alarms.

Use the test player to verify audio on each browser.

## Persistent login

The application stores a random bearer token in a browser cookie and validates its hash against SQLite.

- Refreshing the page can restore login.
- Browser restarts can retain login while the cookie remains valid.
- Logout revokes the current browser’s token.
- Changing a password invalidates existing persistent sessions.
- Each device has its own persistent session.

Login is not guaranteed to persist indefinitely. Clearing cookies, private browsing, browser expiration policies, password changes, or database loss can require logging in again.

The current cookie component uses JavaScript and cannot create HttpOnly cookies. For stronger production authentication, use a server-side identity provider or authentication gateway capable of issuing HttpOnly cookies.

## Deployment

### Required files

Deploy at least:

```text
app.py
requirements.txt
timer_theme_component/index.html
```

### Streamlit Community Cloud

1. Commit the required application files.
2. Ensure `requirements.txt` includes `streamlit-cookies-controller`.
3. Deploy the correct repository, branch, and entrypoint.
4. Review the dependency installation logs.
5. Verify login, timer rendering, audio preview, and public links.

Installing a package on your own computer does not install it in the deployed environment. Keep the deployment dependency file updated.

### Storage considerations

The application currently uses a local SQLite database.

For durable operation, deploy with a persistent writable volume and preserve the database across restarts and redeployments. Do not assume a deployment’s local filesystem is a permanent backup.

Use a single application instance with this SQLite design. Multiple independent instances with separate local databases will not share state.

For a deployment requiring durable managed storage or multiple application instances, adapt the database layer to an appropriate shared database.

## Backup and recovery

The SQLite database contains the application’s private data, uploaded files, credentials, and settings.

Back it up regularly and protect backups accordingly.

For a consistent backup of a running database, use SQLite’s backup API rather than copying only the main database file while writes are occurring.

Example:

```python
import sqlite3

source = sqlite3.connect("data/timer_app.sqlite3")
destination = sqlite3.connect("timer_app_backup.sqlite3")

try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
```

To restore:

1. Stop the application.
2. Preserve the current database and associated SQLite files.
3. Restore the consistent backup to the configured database location.
4. Restart the application.
5. Verify accounts, timers, journals, and attachments.

Treat database backups as sensitive. Restoring an older backup also restores older authentication and sharing state.

## Security notes

Before exposing the application publicly:

- Change the bootstrap administrator password.
- Use HTTPS.
- Keep Secure cookies enabled.
- Do not commit databases, backups, passwords, or secrets.
- Share timer links only with intended viewers.
- Remember that a shared timer includes its description.
- Revoke links that should no longer be accessible.
- Restrict access to the persistent data directory.
- Review uploaded files before downloading or opening them.
- Maintain tested dependency versions.

Suggested `.gitignore` entries:

```gitignore
.venv/
__pycache__/
*.pyc

data/
*.sqlite3
*.sqlite3-wal
*.sqlite3-shm

*.backup-*
.streamlit/secrets.toml
.env
```

Do not ignore `timer_theme_component/index.html`; it is required for deployment.

## Troubleshooting

### Missing `streamlit-cookies-controller`

Install it locally:

```bash
pip install streamlit-cookies-controller
```

For deployment, add it to the active dependency file, commit, and push.

The patch utility can help update a requirements-based deployment:

```bash
python scripts/compatible-for-streamlit-cloud.py app.py
```

### Login does not survive refresh

Check:

- Cookies are permitted.
- The app is using HTTPS when Secure cookies are enabled.
- Local HTTP development uses `TIMER_COOKIE_SECURE=0`.
- The SQLite database is preserved.
- The cookie component dependency is installed.
- The password has not changed since the session was created.

### Automatic share URL detection fails

Upgrade Streamlit to a version exposing `st.context.url`, or configure:

```bash
export TIMER_PUBLIC_BASE_URL=https://your-app.streamlit.app/
```

### Timer statistics are blank

Verify that this file was deployed:

```text
timer_theme_component/index.html
```

Also review application logs and refresh the browser after deploying component changes.

### Timer changes are not immediately visible

The animated display runs locally, while database changes are refreshed periodically.

Refresh the page to load an update immediately.

### Two timers have the same title

A shared title does not necessarily mean the UI rendered the same record twice.

Reusing a template and importing CSV both create new timer records. Check the displayed timer IDs and delete unwanted records individually.

### Embedded timer does not display

Verify that:

- The public link works directly without login.
- The link has not been revoked.
- The timer still exists.
- The blog permits iframe HTML.
- Hosting and browser framing restrictions permit the embed.
- The required component directory was deployed.

## Current scope

This application provides persistent timer schedules and browser-based visualization, not guaranteed operating-system alarms.

It does not currently provide:

- Background push notifications
- Guaranteed alarms while devices are asleep
- Automatic parent–child scheduling
- Automatic recurring timer creation
- Full application restoration from timer/journal CSV alone
- Synchronization between separate SQLite installations
- Undo for deleted records