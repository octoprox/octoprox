---
layout: docs
title: "Changelog"
nav_id: changelog
description: "What changed in each Octoprox release, newest first, with links to the pages that explain each change."
---

# Changelog

<p class="subtitle">What changed in each release, newest first. Releases up to 6.1.4 are summarised one minor version at a time.</p>

## What's New and the update check

The web UI shows these notes under **Help > What's New**, up to the version it runs, and points out the ones you have not opened yet. Admins also see when a newer release is out: once a day a worker fetches [this list]({{ site.baseurl }}/releases.json) from octoprox.com, and the request carries nothing about your install. Set `OCTOPROX_UPDATE_CHECK=false` on the controller and the api to turn the check off (see [Configuration]({{ site.baseurl }}/configuration)).

{% for release in site.data.releases %}
## {{ release.version }}: {{ release.title }} {#v{{ release.version | replace: '.', '-' }}}

<p class="release-meta">{{ release.date | date: "%-d %B %Y" }}{% if release.from %} · covers {{ release.from }} to {{ release.version }}{% endif %}</p>

{% for change in release.changes %}- {{ change.text }}{% if change.docs %} [Read more]({{ site.baseurl }}/{{ change.docs }}){% endif %}
{% endfor %}
{% endfor %}
