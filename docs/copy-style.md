# Writing bot messages

Write the result, choice or next action first. Keep the information needed to understand a match, choose a
stake, diagnose a failure or recover unfinished work. Match existing command terminology and field labels.
[bot-ui.md](bot-ui.md) covers emojis, embeds and controls.

For this bot, AI slop includes generic praise, repeated rules, inflated wording, internal details in player
screens and UI fields or buttons that add no useful information. These are editing problems, not proof of
who wrote the text.

## Message shape

A response is one status emoji and a direct sentence: `{ERROR_EMOJI} You are not the host of this match.`
When the user must do something next, add it after the sentence or after a blank line:
`{ERROR_EMOJI} This search expired. Run a new search.` Use bold only for the state or amount that matters
(`Match-making is now **disabled**!`) and italics for reminders and notes
(`{IMPORTANT_EMOJI} *Please kick the players out of the clan!*`).

Reuse the existing text for recurring situations instead of writing a variant:

| Situation | Existing message |
|---|---|
| Failed permission check | `You do not have permission to run this command!` (components: `...to interact with this component!`) |
| Cooldown | `Command is on cooldown, you can use it again {relative time}.` |
| Another user's control | `You cannot interfere with other's components.` or `You cannot interact with other user's components.`, whichever the extension already uses |
| Stale paginated message | `The paginated message is no longer available.` |
| Clash of Clans unavailable | `Clash of Clans is currently under maintenance.` / `Clash of Clans' API server is having some unexpected issues.` |
| Discord rejected the interaction | `Looks like something is wrong with Discord, please try again later.` |
| Unexpected error | The global handler's message with the **Report Bug** button; do not write another |
| DM could not be delivered | `*This message is sent here, because you have disabled direct messages.*` |

## Terminology

- **Elo** is capitalized for the rating and the system ("Highest Elo", "starting Elo"). Older strings say
  "elo system"; use "Elo" in new copy.
- **Helper** with a capital letter is the Helper bot and its API ("Helper score job `123` has been queued.");
  **helper** in lower case is a person with the helper role ("Please contact a helper.").
- Queue copy says **match-making** ("the match-making queue"); keep it within that feature.
- Both "canceled" and "cancelled" appear; use the spelling of the feature you are editing.
- Name Discord objects the way users see them: match thread, match, round, district, base, clan.

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

## Command references

Wrap every command reference in a message, embed or error in backticks, including its subcommands and any
option it names: `` Use `/final recover`. ``, `` Use `/final bracket create mode:Manual` to resume. ``.
Where a clickable link helps (registration and preset prompts), `self.bot.mention_command("player register", scope)`
is the existing alternative. Slash-command and option descriptions are plain text in Discord, so leave them
unwrapped; they are short sentence-case phrases without a final period ("Show a player's Final result history").

Scope belongs in the title or a useful field. Do not repeat the season and match in every line. Use token
icons beside amounts rather than repeating the currency word. Keep necessary labels, debt, forfeits, tie
decisions and recovery commands.

## Editing a message

Read it beside the existing command that serves the same purpose. Remove a sentence or field if deleting it
leaves the user with the same understanding and choices. Then check whether the remaining words describe
what has completed and what still needs action.

Prefer short, concrete verbs: saved, cancelled, closed, choose, submit. Cut phrases such as in order to, it's
worth noting, seamless experience and successfully completed when the action itself says enough: new copy
reads `Base deleted.` or `Autoqueue preference updated.`. Many older messages still say "You have
successfully..."; leave them unless the task changes that message. Avoid grand introductions, invented
labels, repeated summaries, rhetorical questions and praise for routine button clicks. Do not introduce a
contrast such as This isn't about X, it's about Y when a direct sentence explains the behavior.

Concise copy must still distinguish failure from partial completion. Result saved with a pending publication
instruction is useful. A generic Something went wrong after a committed result is misleading. Explain the
actual problem and the command or action that recovers it, and say what did not change
("Results were not changed.", "No Helper storage was requested.").

Review the finished screen, not only its strings. Check title, description, fields, controls, receipt and
errors together for repetition. Apply [the UI conventions](bot-ui.md) while editing: this project's bold
titles and functional emojis take precedence over generic prose advice about reducing bold or emoji use.

Keep code changes equally focused. Reuse current parsing, locks, helpers and command forms. Avoid a new
framework or abstraction for a local copy change, broad exception suppression, duplicated formatting rules
and tests that only echo implementation. Verify the affected behavior and stop when the requested change is
complete.
