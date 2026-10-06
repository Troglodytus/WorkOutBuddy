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

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  if (req.method !== "POST") return new Response("Method not allowed", { status: 405, headers: corsHeaders });

  // Keep the function user-authenticated. Supabase also verifies JWTs by default
  // for deployed functions; this explicit check prevents accidental anonymous use.
  if (!req.headers.get("Authorization")) {
    return new Response(JSON.stringify({ error: "Authentication required" }), {
      status: 401, headers: { ...corsHeaders, "Content-Type": "application/json" }
    });
  }

  const apiKey = Deno.env.get("OPENAI_API_KEY");
  if (!apiKey) {
    return new Response(JSON.stringify({ error: "OPENAI_API_KEY is not configured" }), {
      status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" }
    });
  }

  try {
    const payload = await req.json();
    const model = Deno.env.get("OPENAI_MODEL") || "gpt-6-luna";

    const instructions =
      "You are WorkOutBuddy, an evidence-focused endurance training analyst. " +
      "Use only the supplied training data. Do not diagnose medical conditions. " +
      "Do not summarize every workout individually. Distinguish real longitudinal signals from heat, hills, route, intensity and recovery confounders. " +
      "Prioritize running fitness while treating cycling, hiking and strength as supporting load. " +
      "Return concise Markdown with exactly these sections: " +
      "## Fitness trend, ## Main limiter, ## Evidence, ## Next 14 days, ## Metrics to watch. " +
      "When the evidence is weak or missing, say so explicitly. Keep recommendations progressive and avoid abrupt volume jumps.";

    const input =
      "Analyze this structured WorkOutBuddy payload. The deterministic analysis is primary evidence; the activity rows are supporting context.\n\n" +
      JSON.stringify(payload);

    const response = await fetch("https://api.openai.com/v1/responses", {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + apiKey,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model,
        instructions,
        input,
        max_output_tokens: 1800,
      }),
    });

    const data = await response.json();
    if (!response.ok) {
      const message = data?.error?.message || "OpenAI request failed";
      return new Response(JSON.stringify({ error: message }), {
        status: response.status, headers: { ...corsHeaders, "Content-Type": "application/json" }
      });
    }

    return new Response(JSON.stringify({ text: extractText(data), model }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" }
    });
  } catch (error) {
    return new Response(JSON.stringify({ error: String(error?.message || error) }), {
      status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" }
    });
  }
});
