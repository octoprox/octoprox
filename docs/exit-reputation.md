---
layout: docs
title: "Exit Reputation: Route Around What a Site Blocks"
nav_id: exit-reputation
description: "Tell Octoprox which requests a site blocked: an exit IP it blocked is kept away from that site for a while, and each connector's weight for the site follows its record there. Modes, settings, sites, reporting, routing and the views."
---

# Exit Reputation

<p class="subtitle">Tell Octoprox which requests a site blocked, and it sends that site's requests around the exits and the vendors it blocks.</p>

A site that has just blocked an exit IP usually blocks its next request too, while other sites still take it, and a vendor that does well on one site can do badly on another. Your client sees the captcha or the block page; Octoprox only sees a tunnel that worked. With exit reputation your clients send **exit reports** through the API ("this request was blocked by www.google.com"), and Octoprox keeps a record per **site**, the exact host a request went to:

* **Connectors with IPs** (static lists, ISP and datacenter ports, cloud instances, pooled slots): a record per exit IP. An IP reported blocked is avoided for that site for a while, and every other site keeps using it. The connector's weight for the site follows how many of its IPs are still usable there.
* **Dynamic connectors**, whose every session exits somewhere new: a record per connector. Its weight for the site follows its block rate there, the share of its recent requests to the site that were reported blocked, and, as hard as you choose with **Strength**, how that compares with the best dynamic connector there.

Evidence fades, so an exit or a vendor that recovered comes back on its own, without anyone clearing a list. An exit IP's fades with **Remember for**: a report counts in full when it arrives, half once that many hours have passed, a quarter after twice as long, and so on. A dynamic connector's fades with **Connector memory**: it counts half after that many more of the connector's requests to the site, and at least half every hour.

## Modes

Each project picks a mode in **Project settings**, **Exit reputation** tab (`exit_reputation` in the [API]({{ site.baseurl }}/api#projects)).

| Mode | What it does |
|------|--------------|
| `off` (default) | Nothing is recorded and exit reports are refused. Routing is unchanged. |
| `record` | Reports are taken and the requests they are measured against are counted. The views show the records and what `avoid` would do, but routing is unchanged: a safe way to see the settings at work first. |
| `avoid` | Routing uses the records: an IP avoided for a site is not routed to for it, and each connector's weight for a site is multiplied by its reputation there (see [Routing](#routing)). |

## Settings

| Setting | Default | Range | What it decides |
|---------|---------|-------|-----------------|
| **Avoid for (minutes)** (`exit_reputation_avoid_minutes`) | 60 | 1 to 10080 | How long a first avoid of an exit IP for a site lasts. Each repeat doubles it, up to 7 days |
| **Remember for (hours)** (`exit_reputation_half_life_hours`) | 24 | 1 to 168 | How many hours an exit IP's evidence takes to count half: its reports and its earlier avoids |
| **Connector memory (requests)** (`exit_reputation_connector_half_life_requests`) | 100 | 20 to 10000 | How many more requests a dynamic connector's evidence for a site and place takes to count half, an hour at most: its blocked reports and the requests they are measured against |
| **Bad reports to avoid** (`exit_reputation_bad_reports`) | 1 | 1 to 10 | How many fresh bad reports avoid an exit IP for a site |
| **Minimum bad share (%)** (`exit_reputation_bad_share`) | 50 | 1 to 99 | The least share of an IP's recent reports for the site that must be bad before it is avoided |
| **Strength** (`exit_reputation_strength`) | 1 | 1 to 20 | How hard a dynamic connector is weighed against the best one at a place. 1 is the gentle trim exit reputation always made (see [Strength](#strength-and-lowest-factor)) |
| **Lowest factor (%)** (`exit_reputation_floor`) | 10 | 1 to 100 | The least share of its weight a dynamic connector keeps for a site, so some requests still reach it and show when it recovers: 10 keeps at least ×0.1 |
| **Keep data for (days)** (`exit_reputation_retention_days`) | 30 | 0 to 365 | How many days the project's exit reputation data is kept after it was last used: each site's records (an exit IP's reports and avoids, a dynamic connector's factor for each place) after the day they were last seen, and the history behind a place's [Over time](#every-place-for-a-site) chart after it was recorded. 0 keeps it forever; a record that goes starts afresh with the next report |

The tab also shows **Move refused sessions** (`location_session_move`, the same setting as on the Location tab), which decides what a `sticky` session does when its row's IP is avoided (see [Sessions](#sessions)).

### When an exit IP is avoided

* An IP is avoided for a site when its bad reports there, faded by Remember for, reach the **bad reports to avoid** (with half a report of slack, so one that came in a little earlier still counts), and make up at least the **minimum bad share** of its recent reports there.
* The avoid lasts **Avoid for**, doubled for each earlier avoid of the IP for the site that is still remembered (those fade like reports, and are rounded to whole ones), and never more than 7 days.
* While it runs, more bad reports add weight but do not lengthen it. Good reports end it early once the bad share falls under the minimum; they never start or lengthen one.

With the defaults (60 minutes, 24 hours, 1 report, 50%):

| What happened | Avoided for |
|---------------|-------------|
| One bad report | 1 hour |
| Reported bad again 3 hours later, after the first avoid ended | 2 hours: the first avoid still counts 0.92, which rounds to 1 |
| 8 bad reports at once | 1 hour: one avoid, however many reports start it |
| Three avoids in one day | 1 hour, then 2, then 4 |
| One bad report, then two good ones a minute later | A minute: the good reports end it, since a third of the recent reports are bad |

### A connector's reputation for a site

* **A dynamic connector**: its own factor is one less its block rate for the site, its success rate there, in steps of 0.05 / **Strength** (0.05 at the default 1, 0.005 at 10), and never 0: at least one step. The [Lowest factor](#strength-and-lowest-factor) is applied by routing, not here, so the record keeps how low the connector really fell. What its weight is multiplied by also depends on the best connector at the place once Strength is above 1. The block rate is its bad reports for the site per request it carried there, over its recent requests: 30 bad reports among 100 requests give 0.7. A request here is what Octoprox counts everywhere, a connection: an HTTPS tunnel is one request however many requests the client sends through it ([Metrics]({{ site.baseurl }}/metrics#how-counting-works)), so report at most one block per connection. A record starts with the connector's first report for the site, good or bad, and requests are counted from then on. Good reports change no block rate, since every request not reported bad already counts as a good one.
* **Per requested place.** A dynamic connector can do well on a site for requests from Germany and badly for requests from the US, so its record is also kept per place the request asked for, at every level of it: a request with `-cc-us-st-ny-city-new_york` counts for New York, for NY and for the US. Each level's record starts with the first report naming it, so a city no report has named yet counts its requests at its state and its country alone. A level with fewer than 20 recent requests gives no factor, and routing uses the next coarser one: a quiet city takes its state's factor, or its country's. With none even at the country, the weight is unchanged, never a figure from every country. A request that names no place has its own record.
* **A quiet place** keeps its factor as it was last decided, until new requests or reports there decide it again, or its record expires (the project's **Keep data for** days after the day it was last seen, 30 by default; see [Settings](#settings)). Once it is decided again, the hours it was quiet count too: its evidence has faded, so with fewer than 20 recent requests left it takes the coarser level's factor until it has 20 again.
* **A connector with IPs**: the share of its eligible IPs, those that could serve the request, not avoided for the site: 8 of 20 avoided gives 0.6. With none left it takes none of the site's requests. Its eligible rows already depend on the place asked for, so it needs no per-place record. Its record for the site still counts its reports and the requests it carried there, for the views alone. Those requests come from the per-host counts of [Hosts]({{ site.baseurl }}/metrics#hosts-where-the-traffic-goes): none are counted while `metrics.hosts.enabled` is off, and none for a host past the `metrics.hosts.max_hosts` ceiling.

### How fast a dynamic connector reacts

A dynamic connector's evidence for a site and place counts half after **Connector memory** more requests through it there (100 by default), and at least half every hour. Its factor is decided once a minute, when request counts are flushed, so a report takes effect within about a minute. The factor moves only when one less the block rate is a full step (0.05 / Strength) away from it, so a rate that wavers near a step does not move it back and forth.

With the default Connector memory, a connector that a site starts blocking on every request falls to an own factor of 0.5 there (×0.5 at Strength 1):

| Its requests to the site from the place | In about |
|-----------------------------------------|----------|
| 100 a minute or more | A minute |
| 10 a minute | 8 minutes |
| 1 a minute | 40 minutes |

Once the blocks stop, every 100 clean requests halve what is left of them, and so does every hour. A lower Connector memory reacts sooner but moves more on a few unlucky requests; a higher one is steadier but slower.

### Strength and Lowest factor

At the default **Strength** of 1, a dynamic connector's weight for a site is multiplied by its own factor, never under the **Lowest factor**: a connector blocked 12% of the time keeps 88% of its weight, wherever the others stand. That is the trim exit reputation always made, so nothing changes until you raise Strength.

Above 1, each dynamic connector is compared with the best one at the place: its success rate is divided by the best's, and that ratio is raised to Strength minus one. In numbers:

```
factor = success × (success / best) ^ (Strength - 1), never under Lowest factor
```

* **success** is the connector's own factor at the place, one less its block rate, from the finest level with enough requests, as above.
* **best** is the highest own factor among the dynamic connectors that could take the request and have one. On a tie, the connector with the lowest id is the best.
* **The best** keeps its own factor, since its ratio is 1. A connector with no factor yet keeps its whole weight, and a connector with IPs keeps its share of usable IPs: Strength and Lowest factor change neither.

Two dynamic connectors at equal weight, Vendor A blocked 12% of the time on a site (success 0.88) and Vendor B 1.3% (success 0.987):

| Strength | Vendor A | Vendor B | Vendor A's share |
|----------|----------|----------|------------------|
| 1 | ×0.88 | ×0.987 | 47% |
| 5 | ×0.56 | ×0.987 | 36% |
| 10 | ×0.31 | ×0.987 | 24% |
| 20 | ×0.1, held at the Lowest factor | ×0.987 | 9% |

A small gap stays small: with Vendor A blocked 2% and Vendor B 1.3%, Vendor A's share is 50%, 49%, 48% and 46% at Strength 1, 5, 10 and 20. These figures leave out the rounding to steps, which moves a share by a point at most.

Points to weigh:

* **Noise grows with Strength.** A few unlucky requests move a connector's success rate a little, and Strength multiplies that. Above about 5, raise **Connector memory** to 1,000 or more, so the rate rests on enough requests to be steady. The settings form says so when Strength is above 5 and Connector memory is under 1,000.
* **The Lowest factor keeps a lowered connector measured.** A connector that takes no requests at a site gets no new evidence there and could never show that it recovered. At the Lowest factor it still takes some, and its record follows them.
* **Strength works per place.** The best is found again for every request, among the connectors that could take it, so a vendor that is best in Germany and weak in the US is weighed by each.

While you change Strength or Lowest factor on an existing project, the form previews what it changes for one site, as `avoid` would route: up to 5 of its most reported places, each with the connectors whose share moves by a point or more, such as "United States: Vendor A 47% → 24%, Vendor B 53% → 76%".

## Sites

A site is the exact host a request went to: `www.google.com`, `maps.google.com` and `google.com` are three sites, since each can judge an exit on its own. A report counts for the one host it names, so name the host the request went to, or give the request's URL and its host is taken. Letter case, a port and a trailing dot make no difference (`WWW.Google.com.:443` is `www.google.com`). A host given as an IP address is its own site, and a name in another script counts as its ASCII form (`www.bücher.de` is `www.xn--bcher-kva.de`; name a site with a `ß` in the ASCII form the request went to). A report naming a public suffix of the [Public Suffix List](https://publicsuffix.org/), such as `com` or `co.uk`, or no host name at all, such as `*.google.com`, is refused.

## Reporting exits

A client tells Octoprox what a site did with a request by sending exit reports to `POST /api/v1/projects/{project_id}/exit-reports`, up to 1000 in one request. Any signed-in role may send them, a viewer included: give a scraper its own [service account]({{ site.baseurl }}/sso#service-accounts) with the viewer role, so the token that reports can change no setting.

| Field | Required | Meaning |
|-------|----------|---------|
| `username` | yes | The proxy username the request used. It tells the project, the place (its `-cc-`, `-st-` and `-city-`) and the request's `-tag-` |
| `site` | yes | The host the request went to, or its URL; the report counts for that exact host, its [site](#sites), alone |
| `verdict` | yes | `bad` (a block page, a captcha, a ban, a `403` or `429` from the site) or `good` (the site answered as it should) |
| `reason` | no | Why, in up to 64 characters, such as `captcha`. An IP's record keeps the latest one |
| `connector_id` | no | The request's `X-Octoprox-Connector-Id` [exit header]({{ site.baseurl }}/connecting#exit-headers) |
| `ip` | no | The request's `X-Octoprox-Exit-Ip` exit header |

**Naming the exit.** Octoprox has to know what the request went through. There are two ways:

1. **A tag.** Add `-tag-<id>` to the request's proxy username: it changes nothing about the routing. A gateway notes what a tagged request went through, and keeps the note 2 minutes; report with the same username within that time. The note is written within about 0.1 seconds of the request, so send the report after the request's response, not alongside the request.
2. **The exit headers**, when the project sends them (**Exit headers** on its Location tab) and the client can read them: a dynamic connector's `connector_id` alone, or a connector with IPs' `connector_id` with its `ip`, which must be the last seen exit IP of one of that connector's rows. A report that carries `connector_id` is named by its headers, and its tag is the fallback: when the headers name no exit of the project's rows, the exit the tag's note names counts instead.

A report that names its exit neither way is refused.

**What to do in a client:**

* Add `-tag-<unique id>` to every request you might report, and report it with that username as soon as the answer is read, within 2 minutes. This works for every connector and every strategy.
* Always send the tag yourself: a `-sessid-` is never read as a tag, and a report with no `-tag-` in its username and no `connector_id` is refused.
* Under the `sticky` strategy a whole session may share one tag value, such as the session's own (`myuser-sessid-abc-tag-abc`), which saves the gateway a write per request. The note is then written again at most once a minute, so report within a minute, and a block reported after the session moved to another exit is counted against the new one. Under the other strategies, use a tag per request.
* The exit headers are optional. A client that reads them (every plain `http://` request; for `https://`, only clients that read the answer to their `CONNECT`) can name the exit without a tag, and with one, keeps the tag as the fallback. Send them as the response carried them: the api takes them as sent, and a mistake in them shows only later, when the workers fall back on the tag or drop the report (see below).
* A dynamic connector's block rate counts connections. A tag per request gets a connection per request in common clients. A client that shares a tag across a session, or names the exit by the headers of its `CONNECT`, keeps sending requests through one tunnel: turn connection reuse off ([how]({{ site.baseurl }}/metrics#disable-client-side-connection-pooling)) or close the connection after a block, or several blocks count against one request.
* Good reports are optional. For a dynamic connector they change no block rate, though a first one naming a place starts its record there, as a bad one would; for a connector with IPs they keep a mostly working IP from being avoided for one bad report, through the minimum bad share.

The answer is `202`, with what became of each report, in the order sent. A refused report does not hold back the others:

```json
{
  "accepted": 1,
  "rejected": 1,
  "results": [
    {"index": 0, "status": "accepted", "ip": null, "site": "www.google.com", "connector_id": "6f1c...", "place": "US/NY", "error": null},
    {"index": 1, "status": "rejected", "ip": null, "site": "www.example.com", "connector_id": null, "place": null,
     "error": "no recent request with this tag (a note is kept 1 to 2 minutes after the tag's last request)"}
  ]
}
```

What an accepted report's `ip`, `connector_id` and `place` say depends on what named its exit:

* **Its tag**: what it counts for. A connector with IPs gets the report on its IP (`place` empty), a dynamic connector on itself at the place the username named (`ip` null).
* **Its exit headers**: what was sent, the `ip` in canonical form (`::ffff:203.0.113.7` is `203.0.113.7`), with the place the username named. The workers then count a dynamic connector's report on the connector at that place, without the IP, and a connector with IPs' report on its IP, at no place. When the headers name no exit of the project's rows, they count it where the tag's note says instead, if the report had a tag a gateway noted.

**What the api refuses.** The api refuses at once only what it can see in the report itself. Exit headers that can name no exit, such as an `ip` without `connector_id`, leave the report to its tag, and it is refused only when that names none either; the `error` then gives both reasons, as `no recent request with this tag (...); exit headers: ip needs connector_id`. A refused report's `error` says why:

| `error` | Why |
|---------|-----|
| `site is empty`, `not a site: co.uk is a public suffix`, `not a site: *.google.com is not a host name` | The site names no host a request can go to, and no IP |
| `username is not this project's` | The username before its parameters is not the project's proxy username |
| `username: ...` | Its `-cc-`, `-st-` or `-city-` cannot be read, as a gateway would refuse it |
| `exit headers: ip needs connector_id` | An `ip` was sent without `connector_id` |
| `exit headers: not an IP: ...`, `exit headers: ip is not an IP` | The `ip` sent is not an IP address |
| `exit headers: connector_id is not a connector id` | The `connector_id` sent holds a character no connector id has: anything but printable ASCII |
| `name the exit with a -tag- in the username, or with the exit headers` | No tag, and no `connector_id` |
| `no recent request with this tag (...)` | No request with the tag came through lately, or it was refused, or the mode was `off` then. A report sent before the gateway wrote the note gets it too |

**What the workers drop.** Whether the exit headers fit the project's rows is for the workers to check, so a report with wrong headers is still accepted. When they do not fit and the report had a tag a gateway noted, the workers count it where the note says. Otherwise they drop it, and record nothing for it, when:

* its project was deleted, or its mode turned `off`, after the report was taken (`project`);
* the connector it names is gone, or is another project's (`connector`). A report its tag named on a row with IPs still counts on that IP;
* its headers name a connector with IPs without the `ip` (`ip_needed`);
* its `ip` is not the last seen exit IP of any of that connector's rows, for example because the row's exit moved since (`ip_not_seen`).

The workers count the reports they drop by reason in `octoprox_worker_exit_reports_dropped_total` ([Metrics]({{ site.baseurl }}/metrics#prometheus)), and log them once per batch at warning, as `Exit reports dropped` with the counts by reason. The same counter counts as `given_up` the reports of a batch the workers gave up after repeated failures, such as Postgres refusing it. On **Administration → System**, each worker's **Exit reports** row shows the reports it took since it started, those given up as *lost*, and on a line under it the reports it dropped, by reason. When reports seem to change nothing, look there first.

The request as a whole answers `409` while the mode is `off`, `404` for an unknown project, `422` for a body that breaks the format (no report or more than 1000, a report without `username`, `site` or `verdict`, a `reason` over 64 characters or with a control character, a `connector_id` over 36 characters or an `ip` over 45), and `503` when Redis cannot be reached: send the reports again. Right after a project is created or its mode is changed, an api can answer `404` or `409` for a moment, normally a few milliseconds, until it has read the change. Reports are data, like metrics: they are not in the audit log. A report on an exit IP is applied within about a second of the `202` while the workers keep up; a report on a connector counts at the next minute's flush of request counts ([how fast](#how-fast-a-dynamic-connector-reacts)).

```python
import uuid

import requests

API = "https://octoprox.example.com/api/v1/projects/<project_id>/exit-reports"
TOKEN = "octo_..."  # a viewer service account's token

username = f"acme-cc-us-tag-{uuid.uuid4().hex[:12]}"
proxy = f"http://{username}:<password>@proxy.example.com:8080"
response = requests.get("https://www.example.com/search?q=shoes", proxies={"https": proxy}, timeout=30)
if response.status_code in (403, 429) or "captcha" in response.text.lower():
    requests.post(
        API,
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"reports": [{"username": username, "site": "www.example.com", "verdict": "bad", "reason": "captcha"}]},
        timeout=10,
    )
```

## Routing

With the mode `avoid`, a request's site changes where it goes. Octoprox reads the site from the host the request goes to: the `CONNECT` target, or a plain request's URL.

1. **Rows whose IP is avoided for the site are dropped** before anything else, by the IP their last [check]({{ site.baseurl }}/ip-attribution#health-checks) saw, so rows that share an IP share its avoids. Requests to every other site keep using them. A request that names an avoided row with [`-prxy-`]({{ site.baseurl }}/routing-strategies#pinning-a-proxy) fails with `502 Bad Gateway (Proxy <id> is avoided for <site>)`.
2. **Each connector's weight is multiplied by its reputation for the site**: a dynamic connector's factor at the finest level of the request's place with enough evidence, compared with the best dynamic connector's there at the project's [Strength](#strength-and-lowest-factor) and never under its Lowest factor, and a connector with IPs' share of usable IPs. The [routing strategy]({{ site.baseurl }}/routing-strategies) then picks by these effective weights as it always does by weight; `sticky` places only new sessions by them, and a session already bound keeps its row while the row is eligible. `round_robin` draws a request whose weights reputation lowered at random by them, rather than in its fixed order, which the project's other requests keep ([why]({{ site.baseurl }}/routing-strategies#exit-reputation-and-effective-weights)).

Weights stay relative, so a connector lowered less than the others gains share. For requests to www.google.com from the US:

| Connector | Weight | Reputation | Effective weight | Share of the site's requests |
|-----------|--------|------------|------------------|------------------------------|
| Vendor A, dynamic | 3 | 35% blocked: ×0.65 | 1.95 | 37% (43% by weight) |
| Vendor B, dynamic | 2 | 10% blocked: ×0.9 | 1.8 | 34% (29%) |
| Static list of 500 IPs | 2 | 120 avoided: ×0.76 | 1.52 | 29% (29%) |

Changes reach every gateway within moments: whatever starts or ends an avoid, or moves a factor, has the controller read the records again (at most once per `feed_compile_interval`, a second by default). An avoid ends on its own at its time; nothing has to lift it.

### Sessions

Under `sticky`, a session whose row's IP becomes avoided for a site follows **Move refused sessions** (`location_session_move`):

* **On**, its next request to the site moves it to another row, which it keeps from then on, for every site.
* **Off** (the default), its requests to that site are refused with `502 Bad Gateway (Exit <ip> of this session is avoided for <site>)` until the avoid ends, and its requests to other sites keep the row. A refused request keeps the session in use, so it does not go idle and move while the client keeps trying. A client that wants another exit sooner starts a new session.

The other strategies hold no session to a row, so a session's next request simply goes through another one. Dynamic connectors avoid no IPs, so their sessions are never refused for this.

### When every exit is avoided

When every row that could serve a request has its IP avoided for the request's site, and no dynamic connector can take it, the request is refused:

```
HTTP/1.1 502 Bad Gateway (Every eligible exit is avoided for www.google.com)
Proxy-Status: octoprox; error=exits_avoided; details="www.google.com"
```

The details name the site. The [Failures]({{ site.baseurl }}/metrics#failures-why-requests-failed) page counts it as **Exits avoided** (`exits_avoided`), among the requests refused before any proxy was used. The answers for rows resting after a rate limit (`429`) and for connectors over their [traffic limit]({{ site.baseurl }}/traffic-limits) come first when they hold for every row that could serve the request.

Points that are easy to miss:

* **A row no check has placed yet** has no exit IP, so it is never avoided.
* **A pooled slot whose vendor session moved** is judged by the IP of its last sighting until the next check sees the new one.
* **No preflight is needed**: reports name their exit themselves, and nothing is echoed for exit reputation.
* **Switching the mode to `record` or `off`** gives every connector its configured weight back and releases every avoid at once, keeping the records; switching back to `avoid` routes by them again.
* **Routing acts on at most 100 sites per exit IP**, and across the whole install on at most 100,000 IP and site avoids, the ones that end last, and 20,000 dynamic connector factors, the coarsest places first: an IP that served a wide crawl can be reported on thousands of sites, and every gateway carries what routing acts on. The records keep everything, and the [views](#views) show it.

**How long records are kept.** A record is deleted the project's **Keep data for** days after the day it was last seen, by a report or a request counted, 30 by default (see [Settings](#settings)), never while an IP's avoid is still ahead; 0 keeps records forever. A record that goes takes its evidence with it, and the next report on that IP or connector for the site starts it afresh. At the default Remember for, 30 days leave an exit IP's evidence worth about a billionth of a fresh report, and a connector's evidence counts half every hour at least. Set **Keep data for** to ten times Remember for or more (10 days or more for Remember for of 24 hours): an exit IP's record deleted sooner forgets evidence that still weighs, so later reports on the IP are decided without it.

## Views

Every role sees what the project's exit reputation holds, as it stands when the page is read:

* **Traffic split** (Overview and Connectors): pick a site under **Requests to**, and a place under **from**, to see each connector's **Weight**, its **Reputation** for the site (the factor, coloured by how far it lowers the weight), its **Effective** weight and the **Expected** share of the site's requests it gets, with the configured split faint above the one for the site and the reason for the connector lowered most under the rows. **Observed**, under **All traffic**, is the connector's share of every request in the window, whatever its site or place: the pickers do not narrow it. Both pickers are searchable and list everything: every site with reports, the most reported first, and every place requests to the site named, by name ("United States", "New York, NY", "London, United Kingdom"). A site opens on its busiest place, and switching sites keeps the place while the new site lists it. A tooltip on a dynamic connector says when its factor came from a coarser place than the one picked ("from United States: NY has too little evidence yet"), and a tag beside its factor names that place. With Strength above 1, the tooltip also says how the factor was reached: "Blocked 12% here vs 1.3% at Vendor B, the best here. At strength 10 that gives ×0.31." or "The best here, blocked 1.3%." At any Strength, a connector held at the floor says so: "Held at the lowest factor, ×0.1." A ×1 beside a hatched **few** is reported on but not judged yet, and a faint ×1 with a dot has no reports, so neither reads as a measured ×1. While the mode is not `avoid`, the numbers are marked as not applied: they are what `avoid` would do. **Every place for this site →** opens the site on the Reputation tab.
* **Exit locations, Reputation tab**: [every place for a site](#every-place-for-a-site), and **Exit IPs by site**, one row per exit IP and site of the connectors with IPs, with its status (**Avoided until** a time, or `-`), its good and bad reports, and the latest one with its reason. Filter by IP (a whole address or its start), by site (any part of it) and by status, and order by last seen or by reports. A row opens its IP: the sites it has a record for (100 at most, those avoiding it now first; filter the tab by the IP for every one), with the counts that decide, the recent avoids a next one doubles by, and the connectors that handed it out.
* **Exit locations, Exit IPs tab** gains two columns while the mode is not `off`: **Reports**, good / bad over every site, and **Avoided**, how many sites avoid the IP now.
* **Proxies**: while the mode is not `off`, the inspector of a row with an exit IP has an **Exit reputation** section, each site of its IP with its status and ends.

With the mode `record`, the views show what `avoid` would do, and routing does not use it. With the mode `off`, nothing new is recorded, and the records kept from before stay until they expire. The same reads are in the [API]({{ site.baseurl }}/api#exit-reputation-records).

### Every place for a site

The Reputation tab opens with one site across every place requests to it named: the most reported site with a place to list, or any other from its searchable **Site** picker, which lists every site with reports. The [Needs attention]({{ site.baseurl }}/metrics#needs-attention) line for a site blocking at least half the requests (by default) on every connector that serves it opens the tab on that site, as does **Every place for this site →** in the traffic split.

Each row is a place: **No place named** first when a dynamic connector has a record for requests naming none, then each country, the most reported first, with its states and cities under it. A country where only a connector with IPs has an IP avoided for the site has a row too. Countries start closed: an open place shows its first 5 places and **Show N more** for the rest, **Expand all** opens them all, and a country's chip says how many of its cities are lowered. A row shows the place's **Reports** (through dynamic connectors, at that exact place, since the first; a country's include its cities'), **Split here**, the expected split of its requests by weight times reputation, a cell per connector, and the **Lowest** value among the connectors that take its requests. The tab reads again every minute, and the rows keep their order meanwhile. When no place has data for the site, the card says so and points to **Exit IPs by site**, where its connectors with IPs are judged by their exit IPs.

A cell is what the connector's weight is multiplied by for the place's requests, and it looks like what stands behind it:

| Cell | What it means |
|------|---------------|
| ×0.6, tinted | Its own value at the place: a dynamic connector's factor there (after Strength and Lowest factor), or a connector with IPs' share of its rows there not avoided for the site. The tint deepens at ×0.85, ×0.5 and ×0.25; an own ×1 has none |
| ×0.7, dashed, with a level tag such as `NY` | A dynamic connector's factor from the coarser level the tag names: the place has no factor of its own yet, with fewer than 20 recent requests or no report naming it |
| ×1, hatched | Reported on, but no level of the place has 20 recent requests yet: the weight is unchanged |
| ×1, faint, with a dot | No reports: the weight is unchanged |
| `-` | The connector takes none of these requests: it is turned off, over its traffic limit, its domain filter, a reservation or its country list keeps it off, or no healthy row of it serves the place |

A dynamic connector gets a factor of its own at a place once the place has 20 recent requests through it, counted from the first report naming it ([per requested place](#a-connectors-reputation-for-a-site)). Until then the place takes the nearest coarser level's, so a busy country is judged on its own long before its quiet cities, which show dashed under it.

**Lowered** means a value under 1, everywhere on the tab. The presets narrow the table: **All**, **Lowered** (places where some connector's own value is under 1) and **Not judged** (places where a connector that takes the requests is reported on but not judged yet), each with its count. **Jump to place** finds any place by name and opens the levels above it. Above the table:

* A sentence says how many of the connectors serving the site are lowered somewhere, and the hardest hit: "On www.google.com, 2 of 3 connectors are lowered somewhere. Hardest hit: Vendor A in New York, NY (×0.3)." On a tie it names the finer place, a city before its state or country, and it skips a connector with IPs at a state or a city where it has only one or two rows, since one avoided row there reads ×0.5 or ×0.
* A tile per connector: a dynamic connector's says where it is lowered, by level ("Lowered in 2 of 9 countries, 6 of 48 cities"), counting only the places where its value is its own, so one bad city is not counted again at its state and its country. A connector with IPs' tile gives its value for requests naming no place: its usable rows of its eligible ones. A bar sorts the connector's places by where it stands, with the count of each.

A cell's and a place's tooltips carry the same Strength line as the traffic split's.

**Counts as decided.** The counts behind a dynamic connector's cell are the place's own, as routing last decided its factor, with how long ago: "5 of 20 requests reported blocked, counted 3 h ago". They are not faded since, as [a quiet place](#a-connectors-reputation-for-a-site) keeps its factor until new requests or reports there decide it again.

**A place.** Click or tap a place to open it beside the page, or over it on a narrow screen. **Split here** sets the configured split of its requests against the effective one and says how much of them move. Then come the connectors, under **Lowered here** (its own value at the place is under 1), **Lowered via** a coarser place, one per level ("Lowered via United States": the place has too little evidence of its own, and the value it inherits from that level is under 1), **Full weight** and **Takes none here**, each with its value, how its share moves ("50% → 24%, -26 pts"), the reason in words, and for a dynamic connector its factor at each level of the place, finest first: "New York, NY ×0.3, then New York, United States ×0.4, then United States ×0.6". Routing uses the first that has one. Weights stay relative, so a lowered connector can still gain share when the others lose more. While the mode is not `avoid`, the card and the place say what `avoid` would do.

**Over time.** A place shows how each connector taking its requests did there, between **Split here** and the connectors, over the last hour, 6 hours, 24 hours, 7 days or 30 days; the range follows the page's. For a dynamic connector, **Own** draws its own factor, the one that counts at the place (a level tag says when it came from a coarser level), so you see how a provider does through the day. **Routed** draws what its weight was multiplied by, at the Strength, Lowest factor and mode in force at each moment, with the Lowest factor as a dashed line and a marker where any of them changed ("Strength 10"), both left out where only connectors with IPs take the requests; while the mode is not `avoid`, it is what `avoid` would have done. A gap is a time the connector was not judged. For a connector with IPs, both views draw the share of its eligible rows whose exit IP the site was not avoiding, so the line drops when an avoid begins and climbs back when it ends; Strength and Lowest factor do not touch it. Up to 24 hours the chart steps at each move; 7 days shows an hourly mean, and 30 days a mean over each 3 hours. Tap or hover a point for the time and each connector's values: a dynamic connector's own and routed value, and the requests and blocked reports since the previous move, or, at 7 days and 30 days, in the hour or the 3 hours the mean covers; a connector with IPs' "17 of 20 IPs usable". Routed uses the connectors as they are set up now: the best is found among those that take the place's requests today, by their filters, reservations and country lists, at every moment. A connector with IPs is drawn with its rows and their exit IPs as they are now, so a fixed list is exact, while for cloud instances or pooled slots whose IPs change, older stretches are approximate. The history starts when Octoprox was upgraded to the version that keeps it, with nothing before. It is kept for the days set as **Keep data for** on the project's **Exit reputation** settings tab, 30 by default, like the site's records (see [Settings](#settings)).

On a phone the table keeps the place and the connectors: **Reports**, **Split here** and **Lowest** give way, the split goes under the place's name, and the table scrolls sideways under the place column.
