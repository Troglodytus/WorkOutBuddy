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
  required: ["summary","plan"],
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
      "The activity payload covers up to 180 days and includes advanced stream-derived metrics. " +
      "The profile.goals object contains the explicit primary race goal, goal date and target_minutes when configured; treat these as user constraints, not predictions. " +
      "The race_predictions object is a conservative deterministic current-capability estimate and should be compared against the target time. " +
      "Pay particular attention to first-half versus second-half HR, speed/HR efficiency, grade-adjusted efficiency, " +
      "raw and GAP efficiency drift, km-based drift, best efforts, power/HR efficiency, normalized power, cadence/step estimates, " +
      "impact-load heuristic, zone distribution, load, elevation and longitudinal VO2max evidence. " +
      "Treat heuristic fields as supportive rather than measured physiology. " +
      "Prior AI evaluations are historical context, not ground truth: explicitly update or disagree with them when new data warrants it. " +
      "Do not summarize every workout individually. Distinguish longitudinal signals from hills, route, intensity and recovery confounders. " +
      "Prioritize running fitness while treating cycling, hiking and strength as supporting load. " +
      "Return concise Markdown with exactly these sections: " +
      "## Fitness trend, ## Main limiter, ## Evidence, ## Next 14 days, ## Metrics to watch. " +
      "When evidence is weak or missing, say so explicitly. Keep recommendations progressive and avoid abrupt volume jumps.";

    const planInstructions =
      "You are WorkOutBuddy's conservative endurance training planner. " +
      "Evaluate the supplied current 14-day deterministic plan against the last 30 days of training, advanced workout metrics, profile/goals, deterministic race_predictions and prior AI evaluations. " +
      "The profile.goals object contains the explicit primary race goal, goal date and target_minutes when configured. The plan should work toward that target while remaining realistic for current capability. " +
      "Return a complete revised 14-day plan using exactly the dates supplied in current_plan. " +
      "Manual/locked days in current_plan are constraints and must not be changed. " +
      "Use first/second-half efficiency, raw/GAP HR drift, km drift, recent load, hard-zone exposure, best efforts, VO2max trend, impact heuristic and cross-training load when relevant. " +
      "Do not chase noisy single-session metrics. Avoid abrupt volume jumps and generally avoid more than two genuinely hard endurance sessions in any rolling seven-day block. " +
      "Use rest/recovery when recent load, durability or efficiency evidence supports it. " +
      "The summary should briefly explain the important changes and why they were made. " +
      "Do not diagnose medical conditions.";

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
      max_output_tokens: mode === "plan" ? 3200 : 1400,
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
        return Response.json({ summary: parsed.summary, plan: parsed.plan, model }, { headers: corsHeaders });
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
