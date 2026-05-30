# Workout Buddy v14: Local Qwen-only Analyze

This update removes the online/OpenAI API path completely.

## What changed

- Default Ollama model is now `qwen3:8b`.
- The Analyze dialog now only offers:
  - `Deterministic only`
  - `Local Ollama`
- The online/OpenAI-compatible option was removed from the UI.
- `analysis_engine.py` no longer contains OpenAI/API-key code.
- Local Ollama uses the official Python package first:
  ```python
  from ollama import Client
  ```
  and falls back to Ollama's local HTTP API if the package is unavailable.

## Where to change the LLM prompt

Open:

```text
analysis_engine.py
```

The important functions are:

### 1. `build_llm_prompt(det, df)`

This builds the main user message sent to the LLM.

Edit this if you want to change:
- the task
- the required output sections
- the interpretation instructions
- how strict/cautious the LLM should be

### 2. `_compact_activity_rows(df)`

This controls which activity columns are included in the JSON payload.

Add/remove column names in `keep_cols` if you want to change which workout data is sent.

### 3. `call_ollama_chat(...)`

This contains the system message:

```python
system = (...)
```

Edit that for the general role/personality of the LLM.

## What is sent to the LLM?

The LLM receives:

- a deterministic summary
- compact rows for the last 35 activities
- selected metrics:
  - sport type
  - distance/time/elevation
  - HR/pace
  - Apple VO2max
  - own VO2max
  - combined fitness score
  - HR/power drift
  - weather/temperature
  - effective power
  - weight/BMI

The prompt asks for exactly:

```markdown
## Cue
## Analyze
## Recommendations for improving
```

## Recommended local setup

```powershell
ollama pull qwen3:8b
ollama run qwen3:8b
```

Then in WorkoutBuddy:

```text
Analysis mode: Local Ollama
Model: qwen3:8b
```
