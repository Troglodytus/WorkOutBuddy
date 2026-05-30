# Workout Buddy v13: Analyze button with Ollama Python library + online option

This update changes the Analyze workflow.

## What changed

The Analyze dialog now has an **Analysis mode** selector:

1. **Deterministic only**
   - No LLM.
   - Uses local rule-based trend analysis only.

2. **Local Ollama**
   - Uses your locally installed Ollama.
   - The code first tries the official Python package:

   ```python
   from ollama import Client
   ```

   - Falls back to Ollama's local HTTP API if the package is not importable.
   - Default model: `llama3.1`.

3. **Online OpenAI-compatible**
   - Sends the same compact last-35-activity summary to an online chat-completions endpoint.
   - Requires one of these environment variables:

   ```bash
   OPENAI_API_KEY=...
   ```

   or

   ```bash
   WORKOUTBUDDY_ONLINE_LLM_API_KEY=...
   ```

   - Optional:

   ```bash
   WORKOUTBUDDY_ONLINE_LLM_BASE_URL=https://api.openai.com/v1
   WORKOUTBUDDY_ONLINE_LLM_MODEL=gpt-4.1-mini
   ```

## Prompt structure

The LLM receives:

- deterministic summary
- last 35 activities, newest first
- compact metrics only
- clear instruction to output:

```markdown
## Cue
## Analyze
## Recommendations for improving
```

## Files changed

- `analysis_engine.py`
- `web_app.py`

This ZIP also includes the other v12 files for convenience.
