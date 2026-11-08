# nyc-subway-data

Schedule data for the NYC Subway iOS app.

Every hour a GitHub Action downloads the MTA's *supplemented* subway GTFS feed
(regular schedule plus most planned service changes for the next 7 days), turns it into
a compact `subway.json` with `tools/build_data.py` and publishes it on the
[`data`](../../tree/data) branch:

- `https://raw.githubusercontent.com/vaart/nyc-subway-data/data/subway.json`
- `https://raw.githubusercontent.com/vaart/nyc-subway-data/data/version.txt`

Source data: [MTA developer resources](https://www.mta.info/developers) (GTFS).
