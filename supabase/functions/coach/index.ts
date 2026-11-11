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
      "You are WorkOutBuddy, a quantitative endurance-training analyst. Answer in English, focus on the athlete's stated target race/date/finish time. " +
      "Use ONLY supplied data, distinguish observation, plausible mechanism and speculation; never diagnose medical illness. " +
      "The optional user_context field contains the athlete's own question, training preferences or recent subjective context. Address the request explicitly where relevant, but treat reported feelings as subjective information and do not let requests override verified data or safety constraints. " +
      "The payload spans up to 120 days: full advanced workout evidence for days 0-28, compact summaries for days 29-120; relationships includes precomputed longitudinal and subgroup comparisons. " +
      "Old AI memory contains ONLY the last analysis summary (up to 650 characters), for continuity not ground truth. No GPS route is sent. " +
      "Analyze WHY fitness metrics may have changed rather than paraphrasing the dashboard: compare first vs latest 60 days, recent weekly run volume, cross-sport load, pace or GAP vs HR at matched effort, first/second-half efficiency, km durability, load before sessions, and high-zone exposure. " +
      "Distinguish genuine aerobic fitness trends from changes due to temperature, humidity, elevation/grade, workout duration, rest, pacing, sensor reliability and selection bias. " +
      "The supplied ML model has leave-one-WORKOUT-out cross-validation; summarize whether Ridge or boosted residual prediction was better, and do not interpret tree feature importance as causality. " +
      "Interpret fatigue-adjusted HR score relative to 100 and only trust differences beyond comparable model errors; uncertain evidence must be labeled explicitly. " +
      "Prioritize numerical contrast: cite 2-4 concrete numbers/workouts/time windows, flag sample counts and missing controls. Don't invent values or imply controlled experiments. " +
      "Identify the TWO most plausible current training limiters and how to distinguish them over the next 2-4 workouts, then derive practical actionable advice. " +
      "Keep performance ambitions compatible with recent mileage/recovery; suggest actual progression, one purposeful session if appropriate, and recovery guidance. " +
      "Return succinct Markdown in EXACTLY these sections: " +
      "## Trajectory & race goal; ## What drives the numbers; ## Durability, load & weather; ## Strongest evidence and uncertainty; ## Next 14 days; ## What to test next. " +
      "Within each section write 2-4 analytical sentences, not a workout-by-workout recap, and use precise comparisons. Avoid generic motivational filler.";

    const planInstructions =
      "You are WorkOutBuddy's goal-oriented endurance training planner. Use planning_intensity.level (1-5) as a CORE constraint, not decorative metadata. " +
      "If user_context contains a personal goal, scheduling request or subjective recovery information, address it in the training plan and explanation when compatible with recorded load and manually locked sessions; do not let it override validated hard safety constraints. " +
      "1 = rehabilitation/conservative, 2 = conservative base, 3 = balanced, 4 = ambitious, 5 = ambitious structured progression within recovery limits. " +
      "For levels 4-5, if recent running baseline and recovery support it, plan about one genuine controlled quality session (tempo, threshold or intervals), one progressively longer EASY aerobic run, and the remaining easy runs EACH week; preserve preferred run-days-per-week. " +
      "Do not replace every quality session with short easy runs simply because the current goal is a half marathon or because long-term endurance is incomplete. " +
      "Level 5 means a stronger training stimulus, NOT running hard every day or instantly doubling mileage. Include warmup, recovery intervals and cool-down inside quality session notes. " +
      "The planning_intensity object contains target_week1_run_km and target_week2_run_km, established_weekly_run_km from demonstrated active training weeks, recent active weeks, returning_from_break and recovery caution. " +
      "IMPORTANT: returning_from_break means the 28-day average understates past training ability but not necessarily CURRENT injury tolerance. Treat the individualized week-1 and week-2 mileage targets as budgets, keeping a balanced program: useful long easy run plus appropriate controlled quality only when pain-free and recovered. " +
      "Do not automatically restore peak pre-break mileage; the application will shorten over-budget AI runs rather than discarding the whole plan. When the user_context mentions recent knee pain or injury, emphasize symptom-limited return to running, recovery spacing and a pain-free prerequisite for quality work. " +
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
      max_output_tokens: mode === "plan" ? 2600 : 1250,
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

    const data = await response.json().catch(() => null);
    if (!response.ok) {
      // Report the failing upstream service without disclosing any API key,
      // partial credentials or private request information to the browser.
      const upstreamCode = String(data?.error?.code || data?.error?.type || "").slice(0, 60);
      console.error("OpenAI API request failed", { status: response.status, code: upstreamCode });
      let message = "OpenAI API request failed (HTTP " + response.status + ").";
      let code = "OPENAI_UPSTREAM_ERROR";
      if (response.status === 401) {
        code = "OPENAI_AUTHENTICATION_FAILED";
        message = "OpenAI rejected the API credentials (HTTP 401). Check or replace the OPENAI_API_KEY secret in Supabase; this is not a WorkOutBuddy login error.";
      } else if (response.status === 403) {
        code = "OPENAI_PERMISSION_DENIED";
        message = "OpenAI denied access (HTTP 403). Verify the API key's project, permissions and any IP restrictions.";
      } else if (response.status === 429) {
        code = "OPENAI_RATE_OR_QUOTA";
        message = upstreamCode === "insufficient_quota" || upstreamCode === "credit_balance_exhausted"
          ? "OpenAI API billing credits or quota are exhausted (HTTP 429)."
          : "OpenAI API rate or quota limit reached (HTTP 429). Check API usage and billing.";
      } else if (response.status === 404) {
        code = "OPENAI_MODEL_UNAVAILABLE";
        message = "OpenAI could not access the configured model (HTTP 404). Check the OPENAI_MODEL setting and model permissions.";
      }
      // 502 identifies a rejected upstream API call, rather than a failed
      // Supabase user session or invalid JWT.
      return Response.json({ error: message, code, upstream_status: response.status },
        { status: 502, headers: corsHeaders });
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
