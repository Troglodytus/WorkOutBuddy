# Workout Buddy v12: planner type recalculation, own VO2max, fitness score, Analyze button

## A) Planner type change now recalculates the selected day

The planner editor now auto-recalculates when you change only the workout type.
Example: if Saturday was planned as Strength and you switch it to Hike, it now rebuilds:

- title
- family
- duration
- distance
- HR zone
- pace/target text
- notes

Supported new type logic includes Hike/Walk. After saving, the normal 14-day replan still runs, so following days are recalculated around that manual choice.

## B) Own VO2max estimate per run

`database.py` now creates/fills:

- `own_vo2max_estimate`
- `estimated_vo2max`

This is separate from manually entered Apple VO2max. It uses distance, duration, elevation gain, average HR, age, and mild temperature correction. It is not a lab value; it is a trend metric.

## C) Combined fitness score

`database.py` now derives:

- `cardio_efficiency_index`
- `temp_adjusted_efficiency`
- `combined_fitness_score`
- `fitness_score_ma7`
- `fitness_score_ma14`
- `fitness_score_ma35`
- `vo2max_35d_trend`

These appear in the statistics/pivot controls and can be plotted over time.

## D) Analyze button

The Activities landing page has a new **Analyze** button. It analyzes the last 35 trainings deterministically and summarizes:

- sport distribution
- total volume
- Apple VO2max trend
- own VO2max trend
- combined fitness score trend
- HR drift
- power/HR efficiency
- possible explanations for negative trends

## Local Ollama LLM foundation

The Analyze dialog includes an optional checkbox **Use local Ollama LLM**.

To use it:

1. Install Ollama.
2. Download a model, for example:

```bash
ollama pull llama3.1
```

3. Start Ollama normally. It exposes:

```text
http://localhost:11434/api/generate
```

4. In Workout Buddy, click **Analyze**, tick **Use local Ollama LLM**, and set the model name, e.g. `llama3.1`.

No cloud API is needed. The code sends a compact JSON summary of your recent workouts to the local Ollama server.

## Files changed

- `web_app.py`
- `planner.py`
- `database.py`
- `analysis_engine.py` new
- `recommendation.py` included from previous version
- `weather.py` and `duplicate_cleanup.py` included to preserve prior fixes
