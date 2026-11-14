# Bot UI conventions

Compare a new screen with the equivalent existing command before adding presentation code. Use player profiles for statistics/recent matches, leaderboards for ranking rows, and base/history views for pagination. Reuse shared helpers and constants.

## Embeds and emojis

Wrap embed titles and field names in `**...**`. Existing profile section headings sometimes use `__**...**__`; follow the comparable screen. Use `COLOR`, `FOOTER_TEXT` and `FOOTER_IMAGE` from [elo_bot/config/presentation.py](../elo_bot/config/presentation.py).

```python
embed = ipy.Embed(title="**Tokens**", color=COLOR)
embed.add_field(name="**Available Tokens**", value=f"{available:,} {TOKEN_EMOJI}")
embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
```

The emoji constants live in [elo_bot/config/emojis.py](../elo_bot/config/emojis.py).

| Constant | Use |
|---|---|
| `SUCCESS_EMOJI` | A completed action after required state is committed |
| `ERROR_EMOJI` | Rejection, failure or expired confirmation |
| `IMPORTANT_EMOJI` | Unfinished work that needs attention/recovery |
| `TOKEN_EMOJI` | Currency amounts and the betting button; ID `1555865080711876660` |
| `STAR_EMOJI`, `WIN_EMOJI`, `LOSS_EMOJI` | Existing score and match-outcome styling |

Interpolate the shared `PartialEmoji` constants in Discord copy. Put token icons beside formatted amounts in balances, stakes, profits, refunds, pools, history and leaderboards. Keep words such as Available Tokens and Stake in tokens where they name a field or input. Keep custom emoji markup outside backticks so Discord can render it; the numeric leaderboard value may remain in backticks. SDK serialization and visible rendering need separate verification.

Success feedback describes committed state. If a result is saved but publication fails, retain the saved outcome and specific pending/error instruction. A queued Helper request is not completed hosting. Neutral views and confirmation prompts do not need a success icon.

## Pagination and controls

Paged results use primary `◀️️` and `▶️` buttons, disabled at the first/last page. Preserve those exact existing labels. Where the view supports page jumps, use the shared secondary `1/3` button, [build_page_button](../elo_bot/discord/pagination.py), `build_page_jump_modal` and `parse_page_number`. A one-page history keeps disabled arrows and its enabled page-number button, matching the current helper.

Navigation edits the originating message. A modal jump edits that same message, validates the range and rechecks ownership. Keep the selected season/scope consistent. Test first, middle and last pages, invalid jumps and another user's click.

Back/Next belongs to form steps such as pairing or predictions. It is not a replacement for history arrows. Add a control only when it provides an action the user needs: Matches opens useful detail; a profile Refresh button was removed because it added no value. Retire confirmation controls after completion, cancellation or expiry. Disable betting controls after closure.

## Screen-specific choices

- Profiles use Avg. Stars and Avg. Destruction. Recent Matches follows `/player profile`: opponent, outcome and both total scores, with existing stars/win/loss icons. Final player history shows the current official result once per match.
- Prediction leaderboard rows use rank/medal, mention, `correct/eligible` and an arrow to the percentage, for example `` 🥇 **@Dominik** `0/1` → `0.00%` ``. Do not repeat Correct Predictions and Accuracy on every row.
- A bet chooser shows the current bet and Available Tokens; conditional Debt explains restrictions. Review shows player/stake and Estimated Profit, plus Debt when present. Omit gross return and repeated wallet statistics. Cancellation review shows the proposed refund, not a completed refund.
- Current match results show necessary scores/totals and relevant outcome. Remove the Official result and Revision wrapper fields. Keep correction/refund reasons in audit storage and private staff confirmation. A separate audit history may identify replaced results; a player's current match page does not need staff notes.
- Base entry uses `/match base submit`, which fits links and attachments. Score entry uses the seven-score modal followed by an ephemeral result preview with Cancel/Confirm. Confirm rechecks role, ownership and current match before saving.

Layout checks should inspect wrapping, readable numbers, action labels and disabled states at both desktop and narrow widths. Preserve the bot's existing styling rather than adding decorative headings, icons or controls to fill space.
