// WorkOutBuddy Web configuration.
// Replace the placeholders after creating the Supabase project.
// The publishable key is intentionally safe to use in a browser when RLS is enabled.
// NEVER put a Supabase secret/service-role key or an OpenAI key in this file.
window.WORKOUTBUDDY_CONFIG = {
  SUPABASE_URL: "https://YOUR_PROJECT_REF.supabase.co",
  SUPABASE_PUBLISHABLE_KEY: "YOUR_SUPABASE_PUBLISHABLE_KEY",
  LOGIN_EMAIL: "workoutbuddy@example.com",
  TIMEZONE: "Europe/Vienna",
  COACH_FUNCTION: "coach"
};
