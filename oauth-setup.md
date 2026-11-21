# Google OAuth client (one time)

The app signs in with your own Google Cloud project, so nobody else's quota or credentials are involved.

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create a project, for example `Ubuntu Files Cloud Backup`.
2. Enable the **Google Drive API** for it.
3. Open **Google Auth Platform** and set it up: app name, your email as support and contact email, audience **External**.
4. Under **Audience**, add your own Google account as a **test user**.
5. Under **Clients**, create a client of type **Desktop app** and download the JSON.
6. Save it as `~/.config/gdrive-folder-backup/oauth-client.json` and run `chmod 600` on it. Do not commit or share it.

The app asks only for `drive.file` plus your email address (to show which account is connected).

## Seven-day expiry in Testing mode

A project left in **Testing** makes Google expire the login every seven days, so you press **Sign in with Google** again once a week. To stop that, set the project to **In production** on the Audience page. Google asks for an app home page and privacy policy link first. `drive.file` is not a sensitive scope, so personal use does not need a verification review. Publishing is your decision.

You can also set `GDRIVE_BACKUP_CLIENT_ID` and `GDRIVE_BACKUP_CLIENT_SECRET` instead of the JSON file.
