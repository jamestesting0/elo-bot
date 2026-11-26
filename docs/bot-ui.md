# Bot UI conventions

Read this before changing anything a user sees: message text, embeds, buttons, selects, modals, DMs or
thread names. Compare the new screen with the closest existing one (profiles for statistics and recent
matches, leaderboards for ranked rows, base and history views for pagination) and reuse its helpers.
[copy-style.md](copy-style.md) covers the wording.

## Where things live

| Location | Contents |
|---|---|
| [elo_bot/config/presentation.py](../elo_bot/config/presentation.py) | `EMBED_COLORS`, `RED_COLOR`, `GREEN_COLOR`, `FOOTER_TEXT`, logo and other image URLs under `ASSETS_URL`, in-game link prefixes, `ORDINAL_REPRESENTATIONS`, `LB_ENTRIES_PER_PAGE`, leaderboard type options |
| [elo_bot/config/emojis.py](../elo_bot/config/emojis.py) | Every custom emoji and the rank, quality, town hall and league emoji tables |
| [elo_bot/discord/pagination.py](../elo_bot/discord/pagination.py) | `build_page_button`, `build_page_jump_modal`, `parse_page_number` |
| [elo_bot/discord/mentions.py](../elo_bot/discord/mentions.py) | `mention_user` |
| [elo_bot/discord/message_edits.py](../elo_bot/discord/message_edits.py), [embeds.py](../elo_bot/discord/embeds.py), [channel_edits.py](../elo_bot/discord/channel_edits.py) | Rate-limited message edits, embed edit payloads, thread-name status edits |
| `elo_bot/features/<feature>/presentation.py`, `ui.py` | Embed and component builders owned by one feature |
| `assets/embeds/*.md`, `assets/tips/*.json` | User-facing text read at runtime by `/embed` and the match tips |

Never hard-code a color, footer, emoji ID or image URL that already has a constant.

EloBot's custom icons use its Discord application emoji bank. The catalog in
`assets/emojis/application_sources.json` records each source. Startup selects the main or beta IDs from
`assets/emojis/application_ids.json`. After an approved asset addition, run
`venv/bin/python scripts/sync_application_emojis.py beta` first, then run it for `main` after verification.
The sync uploads missing names and records their IDs. It preserves existing app and server emojis.

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

- **Title:** bold, `title="**Tokens**"`. Name the view and add scope after ` · ` when needed.
  Examples include `**Player Profile**`, `**Season 14 Final · QF1 Betting**` and `**{district} Base**`.
- **Author:** player and helper views show the selected person's Discord display name and avatar in the author row.
  The display name links to their Discord profile.
- **Color:** `embed_color()` selects the default accent for the running bot. `GREEN_COLOR` marks a hosted or live match announcement and an accepted
  submission; `RED_COLOR` marks an aborted live match, a rejected submission and a removed base.
- **Footer:** Use `FOOTER_TEXT` with `footer_image()` for the signed-in bot's Discord avatar. Before login, the helper
  uses the version's logo from `ASSETS_URL`.
- **Field layout:** fields are full width. Leave `inline` at its default `False`; use `inline=True` only
  when side-by-side fields were specifically asked for.
- **Field names:** bold, `name="**Available Tokens**"`. Section headings inside profiles and the match
  instructions use `__**...**__` (`__**Statistics (All Seasons)**__`, `__**Recent Matches**__`, `__**Instruction**__`).
- **Label lines:** profile descriptions and stat blocks use `**Label:** value` per line. Statistic lists that
  show counts (season stats, base performance) use `Label: \`value\``. Follow the comparable screen.
- **Images:** `set_image(base.image_url)` for bases, `UNAVAILABLE_IMAGE` when the image is missing, and a
  clan badge for clan views. Generated images are attached as files
  and referenced with `url="attachment://bracket.png"`. Static art is served from `ASSETS_URL`.
- **Empty states:** a short sentence or placeholder value: "No recent matches.", "No defenses so far",
  "Not assigned", "Unknown builder". Omit optional statistics with no recorded samples and avoid repeating
  empty-state text; keep meaningful zeroes, coverage context and failure or recovery information.
- **Limits:** field values 1,024 characters, descriptions 4,096, titles 256, select labels and autocomplete
  names 100, 25 autocomplete choices. Check before rendering; `stats_embed` raises an error instead of
  silently truncating data the user needs.
- **Editing:** every edit of a channel message goes through `edit_channel_message`, which queues all edits in
  a channel under Discord's per-channel rate; never call `message.edit(...)` directly (only `context=` edits and
  `ctx.edit_origin`, which use the interaction route, bypass it). A component or modal callback that has not
  answered yet edits the clicked message with `ctx.edit_origin(...)`, or answers first and then edits, so a busy
  channel queue cannot outlast Discord's 3-second response window. Callbacks that wait on a lock use
  `@acknowledge_privately` (outermost), which defers privately before the lock. Ephemeral messages are only
  edited through the interaction. Send
  `embed_edit_payload(embed)` when a description must be cleared, because `Embed.to_dict` drops empty fields.

```python
embed = ipy.Embed(title="**Tokens**", color=embed_color())
embed.add_field(name="**Available Tokens**", value=f"{available:,} {TOKEN_EMOJI}")
embed.set_footer(text=FOOTER_TEXT, icon_url=footer_image())
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
  (`f"{amount:,}"`), Elo without a separator (`1335 Elo`), two decimals for averages and percentages (`f"{rate:.2f}%"`), an arrow from a count to
  its derived rate (`` `12` → `66.67%` ``), the token icon after amounts (`1,250 {TOKEN_EMOJI}`), and
  `ORDINAL_REPRESENTATIONS` for "1st base" style positions.
- **Times:** Discord timestamps, never formatted dates. `RelativeTime` for ages, countdowns, expiry and
  cooldowns; `LongDateTime` for scheduled times and season bounds (a match's **Time** field shows the long
  time with the relative time on the next line); `LongDate` for whole days. Durations read `1h 2m 3s`.
- **Links:** clans `[Name (#TAG)](share_link)`, in-game players `[name (tag)](share_link)`. Display base URLs
  as `[Base Link](url)` via `format_base_link` in `elo_bot/core/links.py`, including base cards, presets,
  search results, submission reviews and outcomes, match base views, renewal warnings and logs.
  District lists keep their descriptive `[District](url)` links. Store and submit raw URLs; message readers
  use `extract_base_link` to accept both hyperlinks and older raw-link messages. Editable URL inputs stay raw.
  Wrap profile URLs in `<...>` to suppress previews and send raw link lists with `suppress_embeds=True`.
- **Emphasis:** bold for the state or quantity that matters (`**cancelled**`, `**disabled**`, `**all**`),
  italics for reminders and notes (`{IMPORTANT_EMOJI} *Please kick the players out of the clan!*`).
  In guides, put supplementary notes and exceptions in separate italic paragraphs, including confirmation
  timeouts, base-submission reminders and the Final's district/replay note. Keep main instructions,
  section introductions and numbered rules in regular text. Escape literal asterisk markers (`\*`)
  so they do not accidentally italicize rule text.
  Messages are embeds, so markdown headings (`##`) and small text (`-#`) are not used.

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
| Refresh the current view | None | `SECONDARY` | 🔄 via `REFRESH_EMOJI` |
| Confirm a prompt; accept a request | Confirm, Accept | `SUCCESS` | `TICK_EMOJI` |
| Cancel a prompt; reject a request | Cancel, Reject | `DANGER` | `CROSS_EMOJI` |
| Form steps | Back / Next / Submit | `SECONDARY` / `PRIMARY` / `SUCCESS` | none |
| Match workflow on thread and public messages | Submit Bases, Start Match, Host, Store Result, Submit Result, Renew, Play Again | `GRAY` | the existing one (📥, ⚔️, `CLASHING_SWORD_EMOJI`) |
| Destructive or reversing | Undo, Delete, Delete Thread, Abort, Cancel Bet, Report Bug | `DANGER` | `CROSS_EMOJI` on Delete Thread |
| Tools on a result | Find Similar, Compare, Show Labels, Reveal Opponent, Spectate | `SECONDARY` | 🔍, ↔️, 🏷️, 👀 |
| External links | Clan Link, Open In-game | `URL` | `CLAN_EMOJI` |

`GRAY`, `GREY` and `SECONDARY` are the same style, and `RED` is `DANGER`; match the surrounding code.
Back/Next belongs to form steps such as predictions; it never replaces history arrows. Add a
control only when it gives the user an action they need: Matches opens useful detail, while a Refresh button
on profiles was removed because it added nothing (leaderboards keep theirs). Retire controls with
`components=[]` after completion, cancellation or expiry, and disable betting controls once betting closes.

### Selects and modals

- `StringSelectMenu` placeholders start with an emoji and say what to choose: "🔍 Select a season",
  "📊 Select the leaderboard type", "🛡️ Set the host clan". Mark the current choice with `default=True`;
  a preset district shows `✅ {district} (Preset)`. Trim option labels to 100 characters.
- Put leaderboard select menus above the row with the 🔄 refresh button. Keep the selected option when
  the leaderboard refreshes.
- Modal titles are short noun phrases ("Jump to page", "Bug Response Form"). Use `ShortText` for single
  values and `ParagraphText` for multi-line input, with a placeholder that shows the format
  ("Format: [stars].[destruction]").
- Every message, the Final prediction wizard included, is an embed with the standard footer and icon.
  interactions.py parses modal submissions. Use `ModalSelectLabel` for string and user selects in a modal.
  Convert selected users or members to string IDs before passing them to persistence or domain logic.

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

- `/player profile`, `/player hitrates`, `/player matches view`, and `/player matches search` default to the caller.
  Select `user` to view another player. Match ID suggestions follow the selected player.
- The `/player profile` description lists `**Elo:**`, `**Highest Elo:**`, `**League:**`, `**Rank:**` and
  `**Tokens:**`; its statistics show the player's and opponents' averages side by side
  (`f"**Stars:** {avg:.2f}{STAR_EMOJI} - {opp_avg:.2f}{STAR_EMOJI}"`). Final profiles use Avg. Stars and Avg. Destruction. Recent Matches lists
  `vs. {opponent} ({elo change}) {relative time}` then both scores, and Final player history shows the
  current official result once per match.
- Prediction leaderboards rank by accuracy, then by correct picks. Rows use rank or medal, name,
  `correct/eligible` and an arrow to the percentage,
  for example `` 🥇 **@Dominik** `0/1` → `0.00%` ``. Do not repeat Correct Predictions and Accuracy on every row.
- A bet chooser shows the current bet and Available Tokens; Debt appears only when present and explains the
  restriction. Review shows player, stake and Estimated Profit, plus Debt when present. Omit gross return and
  repeated wallet statistics. Cancellation review shows the proposed refund, not a completed refund.
- Current match results show the necessary scores, totals and outcome without Official result or Revision
  wrapper fields. Keep correction and refund reasons in audit storage and the private staff confirmation; a
  player's match page shows the current official result without staff notes or duplicate correction entries.
- Players use `/match base submit` and `/match base view` in their match thread. Final submissions take a link.
  Managers can open the seven-score modal with `/final match result` in the Final match thread. The private
  preview has Cancel and Confirm. Confirm rechecks role,
  ownership and the current match before saving. A played preview also has an Edit a score select (one option
  per player and district) that opens a one-score modal and redraws the preview; only its owner can edit.
  The result form prefills saved scores and shows editable `0.00` values for missing scores.

Check layouts at desktop and narrow widths: wrapping, readable numbers, action labels and disabled states.
Preserve the bot's existing styling rather than adding decorative headings, icons or controls to fill space.

## Final screens

- Bracket names (images, selects) are the player's Discord display name, refreshed before the bracket,
  a round's threads or a scoreboard is published. A nickname set with `/final add nickname:` or `/final nick`
  overrides it. Final thread names use Discord usernames like normal match threads (`🟡 QF1 · user1 vs. user8`). Quarterfinals follow seeds: QF1 1v8, QF2 2v7, QF3 3v6, QF4 4v5.
- `/embed Elo Information` has a 📖 README button opening a private guide with a section select.
  Sections live in `assets/embeds/readme/`; navigation edits the same private message.
  Rewards and Base Building keep their own buttons.
- `/embed Elo Final` posts a short description with a 📅 Details button opening the guide's Elo Final page.
  Only Seasons shows dates computed on click (Elo Final selection, queue close, season end, each as
  `<t:…:D> · <t:…:R>`). The Elo Final page mentions the Final channel. The ongoing season ends on the first Monday of Feb,
  Apr, Jun, Aug, Oct or Dec at 07:00 UTC after its `/season reset`; selection is 10 days earlier and the queue
  closes 3 days earlier. If the ongoing season has no Final (a cancelled one does not count) three days after
  its selection date, the season is extended and all dates move to the next boundary.
- `/embed sticky:True` keeps Elo Information or Elo Final at the bottom of the channel like `/sticky`. The bot
  reposts it itself so its buttons keep working; the live dashboards cannot be sticky.
- `/final start` always opens the Final of the ongoing season (the season after the last `/season reset`).
  A season has one Final; cancelling it frees the season, so it can be started again before the reset.
  Entrants are seeded by their Elo when they are added.
- Final commands take `final_season`, an autocomplete of seasons that have a Final (newest first). A season
  restarted after a cancellation shows its latest Final. The ranked `season` autocomplete is separate.
- `/final dashboard` is private. Its controls end with an emoji-only 🔄 refresh button. Managers see thread links, attention items, the next step and the available
  actions. Players see Matches, Predictions and Betting without thread links or manager controls.
  During setup, managers see entrants in Elo seeding order, one linked name per line. Dashboard action
  feedback goes above the updated embed.
- Until the bracket is created, the manager dashboard shows Add and Invite (disabled at 8 entrants).
  Each opens a modal with one user select capped at the open seats; every picked player is checked on their own
  and the outcome goes above the refreshed dashboard (`✅ Invited A, B`, then one `❌ Name: reason` line each).
  Pending invitations are listed with their expiry and can be withdrawn from a select.
- An invitation is a DM (`**Season N Final · Invitation**`, Accept and Decline) that expires after 24 hours. The
  first players to accept take the seats; everything is re-checked on Accept. A closed invitation keeps its title,
  loses its buttons and states the outcome. Accepts and declines post one line, without pings, in the staff
  channel. Creating the bracket, cancelling or ending the Final withdraws pending invitations.
- After the bracket is created, Replace Player swaps a quarterfinal entrant whose first match has not started
  (for a bye, their semifinal). The replacement keeps the slot, inherits predictions on that player and is seeded
  by their current Elo. A pairing whose round started keeps its thread: it is renamed, its opening message mentions
  the replacement, they join it (pinged once) and the leaving player is removed. Bets on the old pairing are refunded
  and a new market opens. Stored bases must be undone first. Started matches use `/final match forfeit`.
- Ending or cancelling a Final archives and locks its match threads; the reply states when they are deleted,
  7 days later, by the recovery task. `/thread delete` removes one sooner once the Final is no longer running.
- Bets show `**Won:** `x/y` → `%`` and `**Profit:** +1,250 🪙 · `+41.67%``, counting settled bets only: per Final
  on the Final profile, overall on `/tokens balance` (Available, Debt when owed, Bets, Open Bets while in play) and
  on the Betting leaderboard (ranked by profit). `/tokens history` labels are inline code (`` `Bet switched` ``).
- `/tokens balance`, `/tokens history` and `/tokens inventory` default to your wallet. Select `user` to view another
  player's wallet. The embed title names the player using their Discord handle.
- Final and token buttons ignore double clicks: a second click while the first runs, or a click on a view the
  same user's earlier click already replaced, is acknowledged without a reply (`ignore_repeated_clicks`).
- `/tokens add` and `/tokens remove` (level-1 roles) change a player's Available tokens and show as a Manager
  adjustment in `/tokens history`. Remove never takes more than the Available balance.
- `/tokens shop` and `/tokens inventory` are public by default. Use `hidden:True` for a private view.
  Using Challenge Override immediately sends one private code per copy and shows when the codes expire.
  Save the codes for `/challenge send`.
  Other players' inventory views only offer page navigation. A select lists the usable items in your own inventory.
  Purchases use an owner-only confirmation. Item emojis use the shared config when registered.
- Players can use `/match stake` once their match thread is created and until the match starts.
  The command replies privately and posts an offer in the thread that pings only the opponent.
  **Match Stake** accepts the displayed amount and **Pass** closes the offer without moving tokens.
  Matching appends *Stake matched.* to the original offer and removes its buttons.
  Show the offer and reply in the match announcement's **Notes** field, one line per player.
  Use `put 500 {TOKEN_EMOJI} at stake`, `matches the stake with 500 {TOKEN_EMOJI}`, or `went all in (500 {TOKEN_EMOJI})`.
  Edit the offer's existing line when it changes. Show the actual held amount if a short all-in reduces the offer.
  Wallet balance lists locked match stakes separately from spectator bets.
- `/final bracket create` opens one seeded draft owned by the manager. The select swaps exactly two
  slots. Reset restores Elo seeding; Confirm publishes and pins the saved bracket; Cancel discards the
  draft. Confirm uses `SUCCESS` and `TICK_EMOJI`; Cancel uses `DANGER` and `CROSS_EMOJI`.
- Starting a round lists each thread or its error, opens the round's betting markets and offers Close
  Predictions while predictions remain open. The dashboard and slash commands use the same actions.
- Predictions live on the pinned bracket post: a Predictions field (`Open · 42 submitted`) and, while
  predictions are open, a Predict button that is removed when they close. The private wizard lists the
  page's pairings in the embed with one winner select each (seed Elo in the options), then Back and
  Next; a page never exceeds five rows. Review shows the bracket image, one pick per line (`` `QF1` Name ``, 🏆
  for the champion), Back and Submit. The submitted screen keeps the image and offers Edit Picks until closure.
- `/final predictions view` lists submitted predictions (`1. **Spyke** · 🏆 Kaito · \`3/5\``, score once a
  match is played) with a "🔍 View a player's picks" select that anyone can use: it opens that player's card
  privately and leaves the list unchanged; `player:` opens one card directly. A card is the marked bracket
  image, the score as `5/7` once a match is played, and the results. Everyone can view after predictions close; LVL1
  managers can view before that, always privately. Only submitted picks appear. Paging stays with the user who ran the command.
  More than 25 predictors page with `◀️` `2/3` `▶️`.
- Betting stays in one private message through the chooser, amount modal, review, cancellation and
  receipt. Modal submission defers with `edit_origin=True`. Back edits the chooser. A closed market
  retires the panel's controls.
- Each public Final betting post includes the H2H VS poster with player nicknames, avatars and tournament
  seeds. Pool and status updates replace the attachment on that same post. Match-thread opening embeds
  and live-start announcements do not include the poster.
- Result previews last 15 minutes and survive a restart. Persistent Confirm and Cancel callbacks
  recheck the owner, role, Final and match before saving. Saved replies identify who advances; the next
  round is called ready only by the result that completes the current round. Tied scores are refused; staff
  replay a district to break the tie. Failed follow-up work keeps the saved result and shows pending updates.
- A played Final result is posted in its match thread and in the Final channel with a scoreboard image
  (`attachment://scoreboard.png`, rendered on `assets/finals/elo_final_scoreboard.png`): nicknames,
  avatars, stars and destruction per district and in total, the loser muted. The
  embed keeps the title, `A ▼ vs. B ▲` and any Recovery field; the district lines and Total
  appear only when the image fails. Forfeits have no scoreboard. Corrections edit both posts.
- Helper captures scout images for submitted Final bases and queues captures for assigned Helper bases as
  soon as they are saved. Pending captures retry automatically. Scouting opens 30 minutes before the
  attacker's scheduled time. The opponent's bases lock when scouting opens or the start is accepted,
  whichever comes first. During scouting, attackers can run `/match base scout` in the Final match thread
  and use the district dropdown to browse one image at a time in a private message. LVL1 managers can also select
  either player's attacking view. Each view follows that player's schedule and shows the other player's
  bases. Missing submissions are filled with Excellent Helper bases and scouted.
  A manual abort pauses scouting for both live players or the aborted war side. If you reschedule,
  scouting reopens 30 minutes before the new time and uses any saved scout images. Helper rollbacks
  keep the scouting window.
- `/final match forfeit` takes the forfeiting player and finds their unfinished pairing. The manager enters
  a reason and confirms that the opponent advances. Any bets on the forfeited match are refunded.
- A Final match thread has only Submit Result. Players submit bases with `/match base submit` in the
  thread; an older Submit Bases button on a Final thread points there instead of opening a form.
- Leave `clan` blank on `/match autostart` or `/match war autostart` to use an available Helper clan with
  enough base slots. Autostart prefers an eligible scheduled host clan and uses the clan that holds ready
  stored bases or already hosts a district rerun when applicable. Manual starts require a clan.
- Final hosting: `/match autostart` and `/match war autostart` run one sequential Helper job for all seven
  districts unless `sequential:False` or `district:` is given. Live `attacks_first` posts that player's
  challenge first each cycle; blank posts both together. Unsubmitted districts get Excellent library bases.
  Final hosting can start without a schedule, including after a manual abort. Open predictions and a
  conflicting live or war format prevent a start before bases are drawn or Helper is asked. `/self` hosting
  is refused for Finals.
- Schedule commands require a future time. In a match thread, **Match Schedule** and **Match War Schedule**
  under a message's Apps menu use its first future Discord timestamp. The war action schedules the
  author’s side and requires the author to be a player in the match.
- Scheduled thread posts show **Set the host clan**, **Host**, **Start Match**, and **Abort**. **Start Match**
  starts Helper hosting immediately in the selected clan and uses ready stored bases when available. Finals
  start sequentially. The same post shows **Match Started** and **Store Result** after hosting starts.
- The sequential progress message (`**QF4 · Sequential Hosting**`) has no fields: a status line with the
  current phase and a relative timestamp, followed by one row per district. A row shows when a challenge
  was sent, an attack finished, or scores were stored. Job IDs and run details stay in `/match helper status`.
- Continue opens a private district picker with the first unfinished district selected. Starting later
  leaves earlier unfinished districts available for another run.
- Managers can select any districts with **Replay** on the latest closed Helper run before the Final
  result is submitted. Replay uses the original layouts and preserves saved scores until Helper verifies
  their replacements. Live scores are replaced together for each district. War replay updates only the
  selected side. **Submit Result** confirmation waits for the replay to finish. Open a fresh preview after
  scores change.
- Cancel Final and End Final ask for confirmation. Cancellation offers optional match thread deletion;
  cancellation itself retains stored-base reservations. Confirmed Discord thread deletion releases them
  through the existing deletion listener. The public bracket (including its Predictions field) and markets reflect closure.
