# How much space get.map needs (Mac mini, 512 GB)

## Measured

These are the sizes on disk (tables + indexes, Postgres 16, after `vacuum`) for 10,000 Waze fare quotes,
with every route a **new** road shape, 1 jam each, 25 traffic lights and 1 camera per shape, and 1% of raw
answers kept:

| Table | Bytes per fare quote |
|---|---|
| route_shapes (encoded shape + lights/cameras list) | 1,302 |
| search_events | 456 |
| traffic_observations | 264 |
| routes | 262 |
| places | 132 |
| raw_responses (1% sampled) | 77 |
| **Total** | **~2,500 (2.5 KB)** |

A place search with 5 results takes ~2.1 KB when every result is a new place. Popular places are stored
once, so typical searches are ~0.5-1 KB.

For comparison, the first design (raw answers and JSON shapes on every row) took ~30-50 KB per fare quote.
The savings come from:

- **Road shapes stored once.** Each is thinned to 10 m and encoded (~380 points become ~90, ~1 KB).
  The same trip quoted again stores nothing new, and its lights and cameras are looked up once per shape,
  not once per quote.
- **Raw answers sampled.** Failed requests are always kept, plus 1% of the rest, for 7 days.
- **Weather once** per ~5 km cell and 15 minutes. **Jams once** per 15 minutes. **Alerts once**, with
  first and last seen times.
- **Weekly partitions.** Old weeks are dropped instantly, with no slow deletes and no bloat.

## What a day costs

Assume each booking makes one fare quote plus ~3 place searches, about 5.5 KB in all:

| Bookings / quotes a day | About | 300 GB lasts |
|---|---|---|
| 50,000 | 0.3 GB/day | ~3 years (the 400-day retention applies first: ~110 GB) |
| 200,000 | 1.1 GB/day | ~9 months |
| 500,000 | 2.8 GB/day | ~3.5 months |
| 1,440,000 (1,000 a minute, all day) | 7.9 GB/day | ~5 weeks |

1,000 a minute is the **peak** figure from the route servers' READMEs, not a daily average. Look at
`fare_ai_responses` for your real daily count:

```sql
select date_trunc('day', created_at) d, count(*) from public.fare_ai_responses
where provider in ('waze','gmap','apple') group by 1 order by 1 desc limit 14;
```

## The limits (getmap.env)

| Setting | Default | |
|---|---|---|
| `MAX_DB_GB` | 300 | Database ceiling. Over it, the oldest weeks are dropped. |
| `MIN_FREE_DISK_GB` | 80 | Disk floor. Under it, the same. Under 1.25 × it, raw answers stop being kept. |
| `RETAIN_RAW_DAYS` | 7 | Raw answers |
| `RETAIN_EVENTS_DAYS` | 400 | Searches and routes (a year plus a month, for year-on-year) |
| `RETAIN_TRAFFIC_DAYS` | 800 | Jams (two years of rush hours) |
| `RETAIN_SHAPES_DAYS` | 180 | A shape nobody has quoted for 6 months |
| `RETAIN_ALERTS_DAYS` / `RETAIN_WEATHER_DAYS` | 800 | |
| `STORE_RAW` / `RAW_SAMPLE_RATE` | sample / 0.01 | `none` saves ~3% more. `all` costs ~10× more. |
| `SHAPE_TOLERANCE_M` | 10 | 20 roughly halves shapes. Lights and cameras are matched before thinning either way. |

Places, traffic lights and speed cameras are kept forever. They are small: Malaysia has tens of thousands
of each.

Why 300 GB of 512: macOS and apps use ~60 GB. An SSD should keep ~15% free to stay fast and last. Postgres
needs headroom for WAL, vacuum and index rebuilds.
