# Bot UI conventions

Read this before changing anything a user sees: message text, embeds, buttons, selects, modals, DMs or
thread names. Compare the new screen with the closest existing one (profiles for statistics and recent
matches, leaderboards for ranked rows, base and history views for pagination) and reuse its helpers.
[copy-style.md](copy-style.md) covers the wording.

## Where things live

| Location | Contents |
|---|---|
| [elo_bot/config/presentation.py](../elo_bot/config/presentation.py) | `COLOR`, `RED_COLOR`, `GREEN_COLOR`, `FOOTER_TEXT`, `FOOTER_IMAGE`, image URLs under `ASSETS_URL`, in-game link prefixes, `ORDINAL_REPRESENTATIONS`, `LB_ENTRIES_PER_PAGE`, leaderboard type options |
| [elo_bot/config/emojis.py](../elo_bot/config/emojis.py) | Every custom emoji and the rank, quality, town hall and league emoji tables |
| [elo_bot/discord/pagination.py](../elo_bot/discord/pagination.py) | `build_page_button`, `build_page_jump_modal`, `parse_page_number` |
| [elo_bot/discord/mentions.py](../elo_bot/discord/mentions.py) | `mention_user` |
| [elo_bot/discord/message_edits.py](../elo_bot/discord/message_edits.py), [embeds.py](../elo_bot/discord/embeds.py), [channel_edits.py](../elo_bot/discord/channel_edits.py) | Rate-limited message edits, embed edit payloads, thread-name status edits |
| `elo_bot/features/<feature>/presentation.py`, `ui.py` | Embed and component builders owned by one feature |
| `assets/embeds/*.md`, `assets/tips/*.json` | User-facing text read at runtime by `/embed` and the match tips |

Never hard-code a color, footer, emoji ID or image URL that already has a constant.

## Feedback messages and embeds

The response to an action is plain message content that starts with a status emoji, not an embed:
`await ctx.send(f"{ERROR_EMOJI} You are not the host of this match.", ephemeral=True)`.
Embeds are for displays: profiles, leaderboards, match announcements, bases, statistics and betting markets.

| Emoji | Meaning |
|---|---|
| `SUCCESS_EMOJI` | The action finished and its required state is committed |
| `ERROR_EMOJI` | Rejection, failure or an expired control; nothing changed unless the text says otherwise |
| `IMPORTANT_EMOJI` | Needs attention: cancellations, reminders, partly finished work and what remains |
| `LOADING_EMOJI` | Queued or running work, usually a Helper or extraction job with its ID in backticks |
| `TICK_EMOJI`, `CROSS_EMOJI` | Icons on Confirm/Accept and Cancel/Reject/Delete Thread buttons |
| `TOKEN_EMOJI` | After every token amount and on the betting button |
| `STAR_EMOJI`, `WIN_EMOJI`, `LOSS_EMOJI` | Scores and Elo gains and losses |
| `RANK_EMOJIS`, `RANK_REVERSED_EMOJIS` | Top three rows of a leaderboard (reversed set for Elo Reversed) |
| `DISTRICT_EMOJI`, `CLAN_EMOJI`, `STAFF_EMOJI`, `GIVEAWAY_EMOJI`, `CLASHING_SWORD_EMOJI` | District lists, clan links, staff actions, giveaways, play actions |
| `BASE_QUALITY_EMOJIS`, `TOWNHALL_EMOJIS`, `LEAGUE_EMOJIS` | Beside the quality, town hall or league they name |

Interpolate the shared `PartialEmoji` constants. Keep custom emoji outside backticks: Discord does not
render them inside code spans, so values inside backticks use the Unicode ⭐ (`` `2.10⭐ 81.50%` ``) while
text outside uses `STAR_EMOJI`. Neutral views and confirmation prompts carry no success icon.

### Embed anatomy

- **Title:** bold, `title="**Tokens**"`. Name the subject and add scope after ` · `:
  `**Season 14 Final · QF1 Betting**`, `**{user}'s Profile**`, `**{district} Base**`, `**{label} Stats**`.
- **Color:** `COLOR` by default. `GREEN_COLOR` marks a hosted or live match announcement and an accepted
  submission; `RED_COLOR` marks an aborted live match, a rejected submission and a removed base.
- **Footer:** `FOOTER_TEXT` with `FOOTER_IMAGE` on every embed (`set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)`
  or `footer=ipy.EmbedFooter(...)`).
- **Field names:** bold, `name="**Available Tokens**"`. Section headings inside profiles and the match
  instructions use `__**...**__` (`__**Statistics (All Seasons)**__`, `__**Recent Matches**__`, `__**Instruction**__`).
- **Label lines:** profile descriptions and stat blocks use `**Label:** value` per line. Statistic lists that
  show counts (season stats, base performance) use `Label: \`value\``. Follow the comparable screen.
- **Images:** `set_image(base.image_url)` for bases, `UNAVAILABLE_IMAGE` when the image is missing, the
  user's avatar as a profile thumbnail, a clan badge for clan views. Generated images are attached as files
  and referenced with `url="attachment://bracket.png"`. Static art is served from `ASSETS_URL`.
- **Empty states:** a short sentence or placeholder value: "No recent matches.", "No defenses so far",
  "Not assigned", "Unknown builder". Omit optional statistics with no recorded samples and avoid repeating
  empty-state text; keep meaningful zeroes, coverage context and failure or recovery information.
- **Limits:** field values 1,024 characters, descriptions 4,096, titles 256, select labels and autocomplete
  names 100, 25 autocomplete choices. Check before rendering; `stats_embed` raises an error instead of
  silently truncating data the user needs.
- **Editing:** edit through `edit_channel_message` (it stays under Discord's per-channel edit rate) and send
  `embed_edit_payload(embed)` when a description must be cleared, because `Embed.to_dict` drops empty fields.

```python
embed = ipy.Embed(title="**Tokens**", color=COLOR)
embed.add_field(name="**Available Tokens**", value=f"{available:,} {TOKEN_EMOJI}")
embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
```

## Text formatting

- **Users:** `mention_user(member, user_id, mention_type)` renders a bold, non-pinging link to the profile,
  with "❌Invalid Player", "❓Unknown Builder" or "❌Invalid Helper" when the member cannot be fetched. Use
  `member.mention` or `<@id>` only when the person must be notified: DM fallbacks, the players pinged on
  their match messages, auto-win and penalty notices. Send bulk announcements with
  `allowed_mentions=ipy.AllowedMentions.none()`, or `AllowedMentions(users=[user_id])` to ping one person.
- **Channels:** `<#channel_id>`, for example `a thread will be made in <#{MATCH_CHANNEL}>`.
- **Commands:** wrap command text in backticks, `` `/player register` ``. A few registration and preset
  prompts use `self.bot.mention_command("player register", scope)` for a clickable mention; either is
  acceptable, never a bare slash command.
- **Numbers:** inline code for values in rows and stat lists, thousands separators for counts and tokens
  (`f"{amount:,}"`), two decimals for averages and percentages (`f"{rate:.2f}%"`), an arrow from a count to
  its derived rate (`` `12` → `66.67%` ``), the token icon after amounts (`1,250 {TOKEN_EMOJI}`), and
  `ORDINAL_REPRESENTATIONS` for "1st base" style positions.
- **Times:** Discord timestamps, never formatted dates. `RelativeTime` for ages, countdowns, expiry and
  cooldowns; `LongDateTime` for scheduled times and season bounds (a match's **Time** field shows the long
  time with the relative time on the next line); `LongDate` for whole days. Durations read `1h 2m 3s`.
- **Links:** clans `[Name (#TAG)](share_link)`, in-game players `[name (tag)](share_link)`, bases
  `[District](link)`. Wrap profile URLs in `<...>` to suppress previews and send raw link lists with
  `suppress_embeds=True`.
- **Emphasis:** bold for the state or quantity that matters (`**cancelled**`, `**disabled**`, `**all**`),
  italics for reminders and notes (`{IMPORTANT_EMOJI} *Please kick the players out of the clan!*`).
  Markdown headings (`##`) and small text (`-#`) appear only in Components V2 views.

## Ranked rows

Leaderboards show `LB_ENTRIES_PER_PAGE` (10) rows. Each row is rank, name, value:
`🥇 **[Dominik](<https://discordapp.com/users/...>)** \`1543\``. The first three ranks use `RANK_EMOJIS`
(`RANK_REVERSED_EMOJIS` for Elo Reversed); later ranks use `**#4**`. Derived metrics follow the count with an
arrow (`` `30` → `2.10⭐ 81.50%` ``), predictions show `` `correct/eligible` → `pct%` ``, and tokens show
`` `1,250` `` with the token icon plus `· Debt: …` when there is debt. Do not repeat column names on every row.

## Components

### Buttons

| Purpose | Label | Style | Emoji |
|---|---|---|---|
| Page back / forward | `◀️️` / `▶️` (keep these exact strings) | `PRIMARY` | none |
| Page number and jump | `2/7` via `build_page_button` | `SECONDARY` | none |
| Confirm a prompt; accept a request | Confirm, Accept | `SUCCESS` | `TICK_EMOJI` |
| Cancel a prompt; reject a request | Cancel, Reject | `DANGER` | `CROSS_EMOJI` |
| Form steps | Back / Next / Submit | `SECONDARY` / `PRIMARY` / `SUCCESS` | none |
| Match workflow on thread and public messages | Submit Bases, Start Match, Host, Store Result, Submit Result, Renew, Play Again | `GRAY` | the existing one (📥, ⚔️, `CLASHING_SWORD_EMOJI`) |
| Destructive or reversing | Undo, Delete, Delete Thread, Abort, Cancel Bet, Report Bug | `DANGER` | `CROSS_EMOJI` on Delete Thread |
| Tools on a result | Find Similar, Compare, Show Labels, Reveal Opponent, Spectate | `SECONDARY` | 🔍, ↔️, 🏷️, 👀 |
| External links | Clan Link, Open In-game | `URL` | `CLAN_EMOJI` |

`GRAY`, `GREY` and `SECONDARY` are the same style, and `RED` is `DANGER`; match the surrounding code.
Back/Next belongs to form steps such as pairing and predictions; it never replaces history arrows. Add a
control only when it gives the user an action they need: Matches opens useful detail, while a Refresh button
on profiles was removed because it added nothing (leaderboards keep theirs). Retire controls with
`components=[]` after completion, cancellation or expiry, and disable betting controls once betting closes.

### Selects and modals

- `StringSelectMenu` placeholders start with an emoji and say what to choose: "🔍 Select a season",
  "📊 Select the leaderboard type", "🛡️ Set the host clan". Mark the current choice with `default=True`;
  a preset district shows `✅ {district} (Preset)`. Trim option labels to 100 characters.
- Modal titles are short noun phrases ("Jump to page", "Bug Response Form"). Use `ShortText` for single
  values and `ParagraphText` for multi-line input, with a placeholder that shows the format
  ("Format: [stars].[destruction]").
- The Final prediction flow is the one Components V2 view: a `ContainerComponent` with
  `accent_color=COLOR`, a `## Season 14 Final · Prediction` heading, `TextDisplayComponent` lines and a
  `-# {FOOTER_TEXT}` footer. Modals are parsed by `ComponentsV2ModalContext`, which also accepts selects
  inside modals (the base submission user select). Use Components V2 only where a flow already does.

### Custom IDs, state and ownership

- Custom IDs are `name|arg|arg` and their callbacks match a regex such as `^stats_select\|\d+$`. Read state
  back from the custom ID and the message (embed title and fields) so controls keep working after a restart.
- A control bound to the user who ran the command checks `ctx.message.interaction.user` against
  `ctx.author` and answers other users ephemerally with "You cannot interact with another user's components."
- A missing or stale target says so: "The paginated message is no longer available.", "This page no longer
  exists.", "This search expired. Run a new search."

### Pagination

Paged results use the primary `◀️️` and `▶️` buttons, disabled at the first and last page, around the
secondary page-number button where the view supports page jumps (`build_page_button`,
`build_page_jump_modal`, `parse_page_number`). A one-page history keeps disabled arrows and its enabled
page-number button. Navigation edits the originating message with `edit_origin`; a modal jump edits that
same message, validates the range and rechecks ownership. Keep the selected season or scope consistent.
Test the first, middle and last pages, invalid jumps and another user's click.

## Visibility and delivery

- Errors, confirmations, previews and personal tool output are ephemeral; slow commands start with
  `await ctx.defer(ephemeral=True)`.
- Display commands take a `hidden` option. Profiles and match history default to public
  (`hidden: bool = False`); queue and base listings default to private (`hidden: bool = True`). Follow the
  default of the command family you extend.
- Announcements, match thread messages and published results are public. Match publications add a random
  tip or fact from `assets/tips/` as message content.
- DMs try `user.send` first. On `Forbidden`, post in `COMMAND_CHANNEL` with the user's mention and
  `*This message is sent here, because you have disabled direct messages.*`
- Match thread names start with a status emoji, changed only through `schedule_channel_name_edit`:
  🟡 created or waiting (and after a war abort), 🔵 both sides scheduled without both hosts,
  🟢 hosts set or the match is live, 🔴 result submitted.
- Owner notifications and image uploads to the owner DM use `silent=True`.

## Errors and recovery

`elo_bot/app/errors.py` answers permission check failures, cooldowns (with a relative time), Clash of Clans
outages and Discord's "Unknown interaction"; every other exception gets the generic message with a
**Report Bug** button that forwards the traceback to the owner. Do not catch exceptions just to hide them.
Expected domain failures raise a specific error (for example `FinalError`) that the command catches and
shows as `f"{ERROR_EMOJI} {error}"`.

Success feedback describes committed state. If a result is saved but publication fails, keep the saved
outcome and show the specific pending or error instruction. A queued Helper request is not completed
hosting: show it with `LOADING_EMOJI` and its job ID until the job reports back.

## Screen-specific choices

- The `/player profile` description lists `**Elo:**`, `**Highest Elo:**`, `**League:**`, `**Rank:**` and
  `**Tokens:**`; its statistics show the player's and opponents' averages side by side
  (`f"**Stars:** {avg:.2f}{STAR_EMOJI} - {opp_avg:.2f}{STAR_EMOJI}"`). Final profiles use Avg. Stars and Avg. Destruction. Recent Matches lists
  `vs. {opponent} ({elo change}) {relative time}` then both scores, and Final player history shows the
  current official result once per match.
- Prediction leaderboard rows use rank or medal, name, `correct/eligible` and an arrow to the percentage,
  for example `` 🥇 **@Dominik** `0/1` → `0.00%` ``. Do not repeat Correct Predictions and Accuracy on every row.
- A bet chooser shows the current bet and Available Tokens; Debt appears only when present and explains the
  restriction. Review shows player, stake and Estimated Profit, plus Debt when present. Omit gross return and
  repeated wallet statistics. Cancellation review shows the proposed refund, not a completed refund.
- Current match results show the necessary scores, totals and outcome without Official result or Revision
  wrapper fields. Keep correction and refund reasons in audit storage and the private staff confirmation; a
  player's match page shows the current official result without staff notes or duplicate correction entries.
- Base entry uses `/match base submit`, which accepts links and attachments. Score entry uses the
  seven-score modal followed by an ephemeral result preview with Cancel and Confirm; Confirm rechecks role,
  ownership and the current match before saving.

Check layouts at desktop and narrow widths: wrapping, readable numbers, action labels and disabled states.
Preserve the bot's existing styling rather than adding decorative headings, icons or controls to fill space.
