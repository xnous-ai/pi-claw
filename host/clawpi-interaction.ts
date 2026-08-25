import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const AskUserParams = Type.Object({
  question: Type.String({ description: "A concise question for the user" }),
  options: Type.Array(Type.String(), {
    description: "Two to eight clear, mutually exclusive choices",
    minItems: 2,
    maxItems: 8,
  }),
});

export default function clawpiInteraction(pi: ExtensionAPI) {
  pi.registerTool({
    name: "ask_user",
    label: "Ask user",
    description:
      "Ask the user to choose when their decision is required. Do not guess or continue until they answer.",
    parameters: AskUserParams,
    executionMode: "sequential",
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const answer = await ctx.ui.select(params.question, params.options);
      return {
        content: [
          {
            type: "text",
            text: answer ? `User selected: ${answer}` : "User cancelled the selection",
          },
        ],
        details: { question: params.question, options: params.options, answer: answer ?? null },
      };
    },
  });
}
