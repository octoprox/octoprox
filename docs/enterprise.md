---
layout: docs
title: "Octoprox Enterprise"
nav_id: enterprise
description: "The licensed edition of Octoprox: every sign-in and API token in the audit log, the whole install's activity on one page and an export for your SIEM, under an offline license that never locks anyone out."
---

# Octoprox Enterprise

<p class="subtitle">The licensed edition of Octoprox. It records who got in as well as what changed: every sign-in and API token in the audit log, the whole install's activity on one page, and an export for your SIEM.</p>

<p class="cta"><a href="{{ site.baseurl }}/enterprise#get-in-touch" data-contact>Get in touch</a> <noscript class="contact-address">enterprise at octoprox dot com</noscript></p>

## What it is

Octoprox Enterprise is the public release with the Enterprise package added, published as its own image, `ghcr.io/octoprox/octoprox-enterprise`, and tagged with the version of the release it is built on. Each Enterprise release follows the public release of the same version.

Run it in place of the public image, with the same settings and roles on the same database: everything else in Octoprox works as it does on the free edition. A license key, entered on the *License* card under **Administration → System**, turns on what Enterprise adds. Only the api processes (roles `api` and `all`) load the Enterprise package, so on a role cluster the gateways, controllers and workers can stay on the public image.

## Available today

Every edition records each change a person makes in the [audit log]({{ site.baseurl }}/audit-log), with who made it, when and the fields it changed. With a license, Octoprox Enterprise adds:

- **Sign-ins.** Every sign-in by password, invite link or sign-in provider, failed ones included, with its method, the address it came from and the browser. A failed sign-in records why it was refused.
- **API tokens.** Each personal access token issued, used for the first time and revoked, and each sign-in provider account unlinked from a user.
- **The Activity page.** Every entry of the install on one page under **Administration → Activity**, the changes recorded before the license included, filtered by entity type, action, the user who acted, project and time.
- **Export for a SIEM.** Every entry under the page's filters as JSON lines, and an `audit` line per entry in the server log for a log pipeline to collect.

The [audit log's Enterprise section]({{ site.baseurl }}/audit-log#octoprox-enterprise) has the details of each.

## Planned

Later releases are planned to hand access control to your directory. None of these is available yet; [Single Sign-On]({{ site.baseurl }}/sso#octoprox-enterprise) describes each as designed:

- [Domain rules]({{ site.baseurl }}/sso#domain-rules): everyone whose verified email is at your domain signs in and gets a user on first login, with a default role.
- [Your own identity provider]({{ site.baseurl }}/sso#your-own-identity-provider): any OpenID Connect or SAML 2.0 provider, such as Okta, Entra ID, Auth0, Keycloak or Ping.
- [Group sync]({{ site.baseurl }}/sso#group-sync): roles from directory groups, re-evaluated on every sign-in.
- [Enforcement]({{ site.baseurl }}/sso#enforcement): the identity provider required for everyone except break-glass admins.
- [Provisioning]({{ site.baseurl }}/sso#provisioning): SCIM 2.0, so a leaver loses access the moment the directory says so.

## Free and Enterprise

| Feature | Free | Enterprise |
|---|---|---|
| Proxy routing, providers, IP attribution, tunnel devices, metrics and the dashboard | Yes | Yes, the same release |
| Sign-in with a password or a Google, Microsoft or GitHub account, and personal access tokens | Yes | Yes |
| Every change a person makes, with who, when and the fields it changed | Yes | Yes |
| Sign-ins, failed ones included, with method, address and browser | No | Yes |
| API tokens issued, first used and revoked | No | Yes |
| The whole install's audit log on the Activity page, with filters | No | Yes |
| Export as JSON lines, and an audit line per entry in the server log | No | Yes |
| Domain rules, your own identity provider, group sync, enforcement and provisioning | No | Planned |

## How licensing works

- **An offline key.** A license is one key, starting with `oxl1.`, pasted into the *License* card under **Administration → System**. Octoprox checks its signature on the install, against a public key the image carries: nothing phones home, and a key it cannot verify is refused with the reason.
- **What a key names.** The organisation, the number of seats, the expiry date and the features it turns on. The License card shows them with the seats in use, and the login page names the organisation while the license is active or in its grace period.
- **Seats** count the active users, invited and token-only accounts included. More active users than seats blocks nobody: the License card shows it, and admins see a banner until users are disabled or the license covers more.
- **Expiry and grace.** A license is active up to and including its expiry date, in UTC. A grace period of 30 days follows in which everything goes on as before, with a banner for admins on every page until a new key is entered.
- **Nobody is ever locked out.** The license decides only what the audit log records and whether the Activity page can be read. Sign-in, passwords and tokens work the same on every edition, whatever the state of the license.
- **After the grace period**, sign-ins, token events and audit log lines are no longer recorded until a new key is entered. Everything else keeps working: changes are still recorded, as on the free edition, and the Activity page and its export stay readable, so an install can still answer an auditor for what it recorded.
- **Renewing**, or adding seats, is a new key pasted over the old one on the License card. A key already past its grace period is refused when entered.

[Getting a license]({{ site.baseurl }}/sso#getting-a-license) has the rest, such as each state the License card shows.

## Get in touch

To get Octoprox Enterprise, or to ask about it, get in touch by email. A license comes with access to pull the Enterprise image, `ghcr.io/octoprox/octoprox-enterprise`, from GitHub's container registry for the GitHub accounts you name, and the key arrives with the steps to run the image and enter the key.

<p class="cta"><a href="{{ site.baseurl }}/enterprise#get-in-touch" data-contact>Get in touch</a> <noscript class="contact-address">enterprise at octoprox dot com</noscript></p>
