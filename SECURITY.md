# Security

## Reporting

Report vulnerabilities privately through GitHub's **Security → Report a
vulnerability** feature for this repository. Do not include real credentials,
stream URLs, device tokens, or private catalogue data in an issue.

## Deployment requirements

- Set a long, random `EASYPLAY_PASS`; the proxy refuses empty credentials.
- Keep `.env` and `.easyplay_device.json` private. Both are gitignored.
- Bind to localhost by default, or restrict LAN access with a firewall.
- Do not expose port 8601 to the internet. Xtream URLs carry credentials in
  their path/query, and HTTP provides no transport encryption.
- If remote access is genuinely required, use a private VPN rather than
  publishing this proxy through a public reverse proxy.
- Rotate credentials if a playlist URL, log, screenshot, or backup leaks them.

The server does not log full request paths, because those paths can contain
credentials. `/healthz` only reports aggregate counts.
