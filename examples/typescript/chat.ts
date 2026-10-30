/**
 * The OpenAI Node SDK (TypeScript) against llm-finops-gateway.
 *
 *   npm install
 *   LFG_API_KEY=lfg_... npm start              # api_key mode
 *   LFG_AUTH_MODE=headers npm start           # headers mode (development)
 *
 * Environment: LFG_URL (default http://localhost:8080/v1), LFG_API_KEY, LFG_MODEL
 * (default gpt-4.1-mini), LFG_TEAM / LFG_PROJECT / LFG_USE_CASE for the attribution headers.
 */

import OpenAI from "openai";
import type { ChatCompletionMessageParam, ChatCompletionTool } from "openai/resources/chat/completions";

const MODEL = process.env.LFG_MODEL ?? "gpt-4.1-mini";

const client = new OpenAI({
  baseURL: process.env.LFG_URL ?? "http://localhost:8080/v1",
  apiKey: process.env.LFG_API_KEY ?? "lfg_dev",
  defaultHeaders: {
    // X-Team is ignored in api_key mode (the key decides); it is required in headers mode.
    "X-Team": process.env.LFG_TEAM ?? "platform",
    "X-Project": process.env.LFG_PROJECT ?? "sdk-example",
    "X-Use-Case": process.env.LFG_USE_CASE ?? "typescript-example",
  },
});

interface FinOps {
  cost_usd: string;
  team: string;
  policy: { budget: unknown; warnings: string[] };
}

/** The gateway's extension block; the SDK passes unknown fields through untouched. */
function finops(obj: unknown): FinOps | undefined {
  return (obj as { x_finops?: FinOps }).x_finops;
}

async function plainCompletion(): Promise<void> {
  const completion = await client.chat.completions.create({
    model: MODEL,
    messages: [{ role: "user", content: "One sentence on what FinOps is." }],
    max_tokens: 60,
  });
  const x = finops(completion);
  console.log("1. completion:", completion.choices[0].message.content);
  console.log(`   cost ${x?.cost_usd} USD, team ${x?.team}, budget`, x?.policy.budget);
  if (x?.policy.warnings.length) console.log("   warnings:", x.policy.warnings);
}

async function streamedCompletion(): Promise<void> {
  process.stdout.write("2. stream: ");
  const stream = await client.chat.completions.create({
    model: MODEL,
    messages: [{ role: "user", content: "Count from one to five, words only." }],
    max_tokens: 40,
    stream: true,
  });
  let last: unknown;
  for await (const chunk of stream) {
    const delta = chunk.choices[0]?.delta?.content;
    if (delta) process.stdout.write(delta);
    last = chunk;
  }
  // The final chunk has no choices; it carries usage and x_finops.
  const x = finops(last);
  const usage = (last as { usage?: { total_tokens: number } }).usage;
  console.log(`\n   usage ${usage?.total_tokens ?? "?"} tokens, cost ${x?.cost_usd} USD`);
}

async function toolCallRoundTrip(): Promise<void> {
  const tools: ChatCompletionTool[] = [
    {
      type: "function",
      function: {
        name: "get_weather",
        description: "Current weather for a city",
        parameters: {
          type: "object",
          properties: { city: { type: "string" } },
          required: ["city"],
        },
      },
    },
  ];
  const messages: ChatCompletionMessageParam[] = [
    { role: "user", content: "What is the weather in Budapest? Use the tool." },
  ];
  const first = await client.chat.completions.create({ model: MODEL, messages, tools, max_tokens: 60 });
  const call = first.choices[0].message.tool_calls?.[0];
  if (!call || call.type !== "function") throw new Error("expected a tool call");
  console.log(`3. tool call: ${call.function.name}(${call.function.arguments})`);
  messages.push(first.choices[0].message);
  messages.push({
    role: "tool",
    tool_call_id: call.id,
    content: JSON.stringify({ temp_c: 9, sky: "cloudy" }),
  });
  const second = await client.chat.completions.create({ model: MODEL, messages, tools, max_tokens: 60 });
  console.log("   answer:", second.choices[0].message.content);
}

async function rejectedCall(): Promise<void> {
  try {
    await client.chat.completions.create({ model: "gpt-9", messages: [{ role: "user", content: "hi" }] });
  } catch (err) {
    console.log("4. rejected as expected:", (err as Error).message);
  }
}

await plainCompletion();
await streamedCompletion();
await toolCallRoundTrip();
await rejectedCall();
