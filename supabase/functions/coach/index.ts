import { createClient } from "npm:@supabase/supabase-js@2.95.0";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function extractText(data: any): string {
  if (typeof data?.output_text === "string" && data.output_text.trim()) return data.output_text.trim();
  const parts: string[] = [];
  for (const item of data?.output || []) {
    for (const content of item?.content || []) {
      if (content?.type === "output_text" && typeof content.text === "string") parts.push(content.text);
    }
  }
  return parts.join("\n").trim();
}

const planSchema = {
  type: "object",
  properties: {
    summary: { type: "string" },
    load_adjustment_reason: { type: "string" },
    plan: {
      type: "array",
      minItems: 14,
      maxItems: 14,
      items: {
        type: "object",
        properties: {
          date: { type: "string" },
          start_time: { type: "string" },
          title: { type: "string" },
          sport_type: { type: "string" },
          family: { type: "string" },
          duration_min: { type: "number" },
          distance_km: { type: "number" },
          zone: { type: "string" },
          pace: { type: "string" },
          wattage: { type: "string" },
          notes: { type: "string" },
          no_workout: { type: "boolean" },
        },
        required: ["date","start_time","title","sport_type","family","duration_min","distance_km","zone","pace","wattage","notes","no_workout"],
        additionalProperties: false,
      },
    },
  },
  required: ["summary","load_adjustment_reason","plan"],
  additionalProperties: false,
};

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  if (req.method !== "POST") return new Response("Method not allowed", { status: 405, headers: corsHeaders });

  const authHeader = req.headers.get("Authorization") || "";
  if (!authHeader.startsWith("Bearer ")) {
    return Response.json({ error: "Authentication required" }, { status: 401, headers: corsHeaders });
  }

  const publishableKeys = JSON.parse(Deno.env.get("SUPABASE_PUBLISHABLE_KEYS") || "{}");
  const publishableKey = publishableKeys.default || Deno.env.get("SUPABASE_ANON_KEY") || "";
  const supabase = createClient(
    Deno.env.get("SUPABASE_URL") || "",
    publishableKey,
    { global: { headers: { Authorization: authHeader } } },
  );

  const token = authHeader.replace("Bearer ", "");
  const { data: { user }, error: authError } = await supabase.auth.getUser(token);
  if (authError || !user) {
    return Response.json({ error: "Invalid or expired session" }, { status: 401, headers: corsHeaders });
  }

  const apiKey = Deno.env.get("OPENAI_API_KEY");
  if (!apiKey) {
    return Response.json({ error: "OPENAI_API_KEY is not configured" }, { status: 500, headers: corsHeaders });
  }

  try {
    const payload = await req.json();
    const model = Deno.env.get("OPENAI_MODEL") || "gpt-6-luna";
    const mode = payload?.mode === "plan" ? "plan" : "analysis";

    const analysisInstructions =
      "You are WorkOutBuddy, an evidence-focused endurance training analyst. " +
      "Use only the supplied training data. Do not diagnose medical conditions. " +
      "The activity payload covers up to 180 days. Workouts from the most recent 28 days have detail_level=full with advanced metrics; older workouts are intentionally compact to reduce token cost while preserving long-term volume/HR/VO2/weather context. " +
      "The profile.goals object contains the explicit primary race goal, goal date and target_minutes when configured; treat these as user constraints, not predictions. " +
      "The race_predictions object is a conservative deterministic current-capability estimate and should be compared against the target time. " +
      "Pay particular attention to first-half versus second-half HR, speed/HR efficiency, grade-adjusted efficiency, " +
      "raw and GAP efficiency drift, km-based drift, best efforts, power/HR efficiency, normalized power, cadence/step estimates, " +
      "impact-load heuristic, zone distribution, load, elevation, temperature, relative humidity and longitudinal VO2max evidence. " +
      "Treat temperature and humidity as important cardiovascular confounders: elevated HR or poorer efficiency in hot/humid conditions should not be interpreted identically to the same response in cool conditions. " +
      "For recent runs, km_efficiency contains per-kilometer HR, GAP pace, prior cumulative load/Z4-Z5/time, personal expected HR and fatigue_adjusted_efficiency. " +
      "Interpret fatigue_adjusted_efficiency around 100: above 100 means HR was lower than this athlete's personal 180-day expectation for that GAP speed and prior fatigue; below 100 means higher than expected. When fatigue_reference_uses_weather=1, that expectation already includes workout temperature and humidity. " +
      "Treat heuristic fields as supportive rather than measured physiology. " +
      "The history array contains at most one compact summary of the last completed AI analysis. Use it for continuity, not as ground truth, and do not restate it unnecessarily. " +
      "The ml_model object contains whole-workout cross-validated Ridge and Gradient Boosting errors, the selected HR predictor and non-causal feature importance. Use these as reliability evidence and never claim a model is superior when cross-validation does not support it. " +
      "Do not summarize every workout individually. Distinguish longitudinal signals from hills, route, intensity and recovery confounders. " +
      "Prioritize running fitness while treating cycling, hiking and strength as supporting load. " +
      "Return concise Markdown with exactly these sections: " +
      "## Fitness trend, ## Main limiter, ## Evidence, ## Next 14 days, ## Metrics to watch. " +
      "When evidence is weak or missing, say so explicitly. Keep recommendations progressive and avoid abrupt volume jumps.";

    const planInstructions =
      "You are WorkOutBuddy's goal-oriented endurance training planner. Use planning_intensity.level (1-5) as a CORE constraint, not decorative metadata. " +
      "1 = rehabilitation/conservative, 2 = conservative base, 3 = balanced, 4 = ambitious, 5 = ambitious structured progression within recovery limits. " +
      "For levels 4-5, if recent running baseline and recovery support it, plan about one genuine controlled quality session (tempo, threshold or intervals), one progressively longer EASY aerobic run, and the remaining easy runs EACH week; preserve preferred run-days-per-week. " +
      "Do not replace every quality session with short easy runs simply because the current goal is a half marathon or because long-term endurance is incomplete. " +
      "Level 5 means a stronger training stimulus, NOT running hard every day or instantly doubling mileage. Include warmup, recovery intervals and cool-down inside quality session notes. " +
      "The planning_intensity object contains historic weekly volume, long-run ability, recommended week-1/week-2 progression and a recovery caution flag. " +
      "Use the last 30 days of actual workouts, intensity distribution, fatigue/drift, weather, cross-training load and the current 14-day plan to make the next steps realistic. " +
      "If recent fatigue, illness, injury indications or other evidence require dropping all quality work or reducing training substantially, explain precisely why in load_adjustment_reason. " +
      "Otherwise keep load_adjustment_reason empty. The application validates your result and may reject an unjustifiably easy or excessive plan. " +
      "Target roughly the suggested volume progression in planning_intensity when feasible, avoid abrupt increases and never exceed two truly hard endurance sessions in any rolling seven days. " +
      "Keep manual/locked days unchanged and use exactly the 14 dates in current_plan. Return all required plan fields for every date. " +
      "The profile.goals object contains the primary race, date and target_minutes. Distinguish the athlete's desired finish time from conservative current capability. " +
      "History is only one prior analysis summary. Treat ML errors/importance as predictive evidence, not medical certainty. " +
      "State a concise rationale in summary including the selected aggressiveness level and the key quality/long-run structure. Do not diagnose medical conditions.";

    const input =
      (mode === "plan"
        ? "Review and revise this structured WorkOutBuddy plan payload. No raw GPS route is included.\n\n"
        : "Analyze this structured WorkOutBuddy payload. The deterministic analysis is primary evidence; activity rows and prior evaluations are supporting context. No raw GPS route is included.\n\n") +
      JSON.stringify(payload);

    const body: any = {
      model,
      instructions: mode === "plan" ? planInstructions : analysisInstructions,
      input,
      reasoning: { effort: "low" },
      max_output_tokens: mode === "plan" ? 2600 : 1100,
      store: false,
    };
    if (mode === "plan") {
      body.text = {
        format: {
          type: "json_schema",
          name: "workoutbuddy_training_plan",
          strict: true,
          schema: planSchema,
        },
      };
    }

    const response = await fetch("https://api.openai.com/v1/responses", {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + apiKey,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });

    const data = await response.json();
    if (!response.ok) {
      const message = data?.error?.message || "OpenAI request failed";
      return Response.json({ error: message }, { status: response.status, headers: corsHeaders });
    }

    const text = extractText(data);
    if (mode === "plan") {
      try {
        const parsed = JSON.parse(text);
        return Response.json({ summary: parsed.summary, load_adjustment_reason: parsed.load_adjustment_reason, plan: parsed.plan, model }, { headers: corsHeaders });
      } catch {
        return Response.json({ error: "AI returned invalid structured plan JSON" }, { status: 502, headers: corsHeaders });
      }
    }

    return Response.json({ text, model }, { headers: corsHeaders });
  } catch (error) {
    return Response.json(
      { error: String((error as any)?.message || error) },
      { status: 500, headers: corsHeaders },
    );
  }
});
