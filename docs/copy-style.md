# Writing bot messages

Write the result, choice or next action first. Keep the information needed to understand a match, choose a stake, diagnose a failure or recover unfinished work. Match existing command terminology and field labels.

For this bot, AI slop includes generic praise, repeated rules, inflated wording, internal details in player screens and UI fields/buttons that add no useful information. These are editing problems, not proof of who wrote the text.

## Examples from Final

| Remove or replace | Preferred treatment |
|---|---|
| Stars per district / Destruction per district | Avg. Stars / Avg. Destruction, with the existing score formatting |
| `#1 @Dominik 0/1 Correct Predictions · Accuracy: 0.00%` | Existing rank/name row: `` 🥇 **@Dominik** `0/1` → `0.00%` `` |
| Estimated Gross Return, includes stake, repeated balance/stakes in bet review | Player, stake, Estimated Profit; Debt only when present |
| Based on the pool when betting closes | Omit the repeated explanation from the review |
| Official result / Played · player advances / Revision 2 | Show the current score/result without redundant wrapper fields |
| Reason: Beta QA confirmed correction; retain the synthetic winner | Keep the reason in audit/private staff confirmation; omit it from player match pages |
| Profile Refresh | Remove it; keep Matches because it opens detail |
| Provisioning, node, logical round | Creating a match thread, match, round |
| Great job! Your prediction has been successfully submitted! | Prediction submitted. |

Wrap every command reference in a message, embed or error in backticks, including its subcommands and any option it names: `` Use `/final recover`. ``, `` Use `/final bracket create mode:Manual` to resume. ``. Slash option descriptions are plain text in Discord, so leave them unwrapped.

Scope belongs in the title or a useful field. Do not repeat the season and match in every line. Use token icons beside amounts rather than repeating the currency word. Keep necessary labels, debt, forfeits, tie decisions and recovery commands.

## Editing a message

Read it beside the existing command that serves the same purpose. Remove a sentence or field if deleting it leaves the user with the same understanding and choices. Then check whether the remaining words describe what has completed and what still needs action.

Prefer short, concrete verbs: saved, cancelled, closed, choose, submit. Cut phrases such as in order to, it's worth noting, seamless experience and successfully completed when the action itself says enough. Avoid grand introductions, invented labels, repeated summaries, rhetorical questions and praise for routine button clicks. Do not introduce a contrast such as This isn't about X, it's about Y when a direct sentence explains the behavior.

Concise copy must still distinguish failure from partial completion. Result saved with a pending publication instruction is useful. A generic Something went wrong after a committed result is misleading. Explain the actual problem and the command or action that recovers it.

Review the finished screen, not only its strings. Check title, description, fields, controls, receipt and errors together for repetition. Apply [the UI conventions](bot-ui.md) while editing: this project's bold titles and functional emojis take precedence over generic prose advice about reducing bold or emoji use.

Keep code changes equally focused. Reuse current parsing, locks, helpers and command forms. Avoid a new framework or abstraction for a local copy change, broad exception suppression, duplicated formatting rules and tests that only echo implementation. Verify the affected behavior and stop when the requested change is complete.
