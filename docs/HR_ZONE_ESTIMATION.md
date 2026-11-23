# Automatic 60-day heart-rate training-zone estimates

**Feature branch:** `feature/automatic-60d-heart-rate-zones` (from `web-supabase-v2`).

The Planner now displays five **provisional training-zone estimates** immediately below the race-time cards (10 km, half marathon and marathon). The calculation runs in the browser, using the authenticated user's existing `activity_streams` data, with no AI service or database migration.

## Inputs / refresh

- All activities whose start time is within the preceding 60 × 24 hours.
- Time-weighted heart-rate streams (TCX/FIT/GPX and migrated workouts) from the existing Supabase table.
- Running HR is preferred when sufficient running workouts exist; otherwise cycling, then mixed-sport evidence. All exercise types can inform the observed sustained peak.
- A valid birthdate, if available, provides a weak age-predicted HRmax prior.
- Automatic calculation: at login, after a successful import/overwrite, after profile settings are saved, and when reopening the Planner after a prolonged period. The displayed time indicates when the calculation completed.

The calculation **does not overwrite** `profiles.hr_zones`, stored per-activity zone metrics, or historical training plans. Existing configured zone limits remain operative in training analysis and workout color coding. This deliberately avoids retroactively changing previous statistics from uncertain physiological estimates.

## Estimation method

- Reject HR outside 40–225 bpm, abrupt implausible spikes, and time gaps beyond 30 seconds.
- Use the elapsed duration between samples rather than assuming a constant sampling interval; accumulate a 30-second time-weighted stream.
- Obtain an observed sustained peak (rolling one-minute maximum). This is a **lower bound** on true maximal HR, not a maximal HR measurement.
- Estimate HRmax from the larger of the observed one-minute peak + a modest reserve and, when available, the Tanaka-style 208 − 0.7 × age population prior. Clamp to a plausible range and disclose uncertainty.
- The upper aerobic boundary (end of Z2) is a 77%-HRmax prior, weakly influenced by longer sessions with relatively small measured HR/pace efficiency drift. **Low drift does not prove LT1.**
- The upper threshold-zone boundary (end of Z4) is a 90%-HRmax prior, updated with high-HR sustained 20-minute running or cycling segments. **Sustained HR does not prove LT2.**
- The remaining zone boundaries interpolate between the two proxies. Z1 ends at 86% of the aerobic-boundary proxy, and Z3 ends 52% of the way between the aerobic and threshold proxies.
- All estimates are rounded to bpm, labeled as provisional, and shown alongside the number of workouts, HR hours, supporting efforts, observed peak and model confidence.

## Limitations

These are personalized **training heuristics**, not clinical exercise testing. Neither the five boundaries nor aerobic / anaerobic thresholds can be established precisely from unsupervised workouts. Values can shift with sensor accuracy, heat, fatigue, elevation, medications, and the type of exercise. In particular, running and cycling often have different threshold HR values. A validated field test or laboratory assessment is preferable when precision matters.

For regression tests:

```bash
node --test tests/hr_zone_estimator.test.js
```
