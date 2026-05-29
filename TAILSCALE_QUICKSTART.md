# Tailscale quickstart for WorkOutBuddy Web

1. Install Tailscale on the Windows PC/server.
2. Log in with your account.
3. Install Tailscale on the iPhone from the App Store.
4. Log in with the same account and enable the VPN profile.
5. In the Tailscale admin/device list, find the Windows PC name or MagicDNS name.
6. Run WorkOutBuddy Web on the PC:

```powershell
.\.venv\Scripts\python.exe main.py
```

7. On the iPhone open:

```text
http://<PC-MagicDNS-name>:8080
```

Example:

```text
http://daniel-pc.tailnet-name.ts.net:8080
```

For Strava OAuth from the iPhone, set this same base URL in `.env`:

```env
WORKOUTBUDDY_PUBLIC_BASE_URL=http://daniel-pc.tailnet-name.ts.net:8080
```

Then set the Strava callback domain to:

```text
daniel-pc.tailnet-name.ts.net
```
