---
name: bamberg-news
description: Produce verified Russian-language Bamberg/Bavaria, world-politics, Ukraine/Russia, mandatory daily weather, daily public-health and local garden video briefings for the separate Bamberg News YouTube channel.
---

# Bamberg News

Use this skill only for the isolated Bamberg News publication. It does not use
the personal Butler news digests or vault.

1. Read `sources.json` and the complete worker prompt first.
2. Research the four mandatory sections: local Bamberg/Landkreis, world
   politics, Russia's war against Ukraine, mandatory daily Bamberg weather,
   daily official public-health information, and Bamberg garden work.
3. Create factual scenes only when their evidence meets the policy:
   official primary source or two independent sources; Ukraine/Russia always
   has two independent sources; weather scenes always cite DWD and include a
   24–48 hour forecast plus warning check; health scenes cite a primary German
   health authority and never give individual medical advice; garden scenes always cite DWD and source any
   care action to a Bavarian/local garden authority.
4. Record direct article links, publication/retrieval timestamps and source
   tier in `episode.json`; report a category as `no_verified_update` instead
   of filling it with commentary.
5. Validate, render with the free Russian voice, then upload only through
   `./bin/bamberg-news publish` when the caller has authorized it.

The renderer is presentation only. `episode.json` and its sources remain the
evidence record; never make the video appear to be primary footage.
