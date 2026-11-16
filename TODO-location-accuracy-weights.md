# Location Accuracy Weights - Tracked Follow-up

Deferred on 2026-11-16: exit reputation shipped without it. This is another dial, usable on its own or combined
with reputation (weight x reputation x location).

## The gap

Nothing in routing reads exit location accuracy. A new request picks a connector by its weight times its exit
reputation factor, which comes from client reports only. A preflight refusal never lowers a weight:

- A refused sticky session with "Move refused sessions" on stays on its dynamic connector and gets a new vendor
  session from the same provider. It changes connector only after a vendor 4xx, an unreachable gateway or a row gone.
- A refused session-less request on a dynamic connector picks again by weight, with the refused connector still in
  the draw, up to `preflight_max_attempts` (3).

So every request starts from the same weights, whichever provider usually lands in the requested place.

## The idea

A factor per (connector, requested place level) from recent location accuracy multiplies the connector's weight.
Requests then start on the provider that most often exits where asked, and weight-based retries favour it too.

## Notes for when it is picked up

- **The data exists:** the observation ingest keeps each connector's location accuracy per level, which the Exit
  locations page shows (`exit_accuracy` in `api/db/geo_repository.py`).
- **Exit reputation's machinery fits:**
  - levels with the finest one that has enough evidence (`api/reputation/weight.py`);
  - a memory counted in requests;
  - decisions once a minute at the metrics flush;
  - a feed section;
  - the side-by-side effective weight in the Traffic split card.
- **To decide:**
  - its own project setting or mode;
  - what counts against a connector (contradicted only, or unknown too);
  - whether it applies to dynamic connectors only, since fixed rows are already chosen by where they were seen.
- **Before building**, state the cost per connector kind: request path, writes, feed size, and the effect on spidey.
