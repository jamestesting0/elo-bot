import time

import interactions as ipy

from elo_bot.config.emojis import CLASHING_SWORD_EMOJI, CROSS_EMOJI, IMPORTANT_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI
from elo_bot.config.presentation import FOOTER_TEXT
from elo_bot.discord.embeds import embed_color, footer_image
from elo_bot.core.finals import FINAL_DISTRICTS
from elo_bot.core.match_results import is_valid_match_result_entry
from elo_bot.core.parsing import get_destruction, get_stars
from elo_bot.db import Match
from elo_bot.db.sequential_models import LIVE_SIDE, HelperSequentialRun
from elo_bot.discord.pagination import build_page_button
from elo_bot.features.hosting.sequential.policy import HELPER_DISTRICT_NAMES, STATUS_EVENT_COUNT
from elo_bot.features.hosting.sequential.runs import district_states, is_tracked, played_district_codes, result_sides, run_result
from elo_bot.features.hosting.sequential.scores import outcome_entry, outcome_side
from elo_bot.integrations.helper_api import HelperSequentialJob, HelperSequentialResult

CANCEL_PREFIX = "helper_seq_cancel"
CONTINUE_PREFIX = "helper_seq_continue"
REPLAY_PREFIX = "helper_seq_replay"
CONTINUE_DISTRICT = "seq_continue_district"
CONTINUE_CONFIRM = "seq_continue_confirm"
LIST_LEFT_PREFIX = "helper_runs_left"
LIST_RIGHT_PREFIX = "helper_runs_right"
LIST_PAGE_PREFIX = "helper_runs_page"
LIST_JUMP_PREFIX = "helper_runs_jump"

STAGE_LABELS = {
    "PREPARING": "Preparing layouts",
    "POSTING": "Sending challenges",
    "WAITING": "Waiting for attacks",
    "CLOSING": "Closing challenges",
    "SCORING": "Reading scores",
    "FINISHING": "Finishing the run",
}


def relative_time(timestamp: float | None) -> str:
    return f"<t:{int(timestamp)}:R>" if timestamp else "never"


def side_label(side: int) -> str:
    return "Live" if side == LIVE_SIDE else f"Player {side + 1}"


def run_status_line(run: HelperSequentialRun, result: HelperSequentialResult | None) -> str:
    match run.state:
        case "CREATING":
            return f"{LOADING_EMOJI} Sending the match to Helper."
        case "UNCERTAIN":
            if run.replay_baseline is not None:
                return (f"{IMPORTANT_EMOJI} Helper could not confirm the replay. "
                        "Managers can use `/match helper recover`.")
            return (f"{IMPORTANT_EMOJI} Helper's response could not be confirmed, so the job may be running. "
                    "Staff: run `/match helper recover`.")
        case "ACTIVE":
            checked = relative_time(run.last_polled_at)
            if run.poll_failures:
                return f"{IMPORTANT_EMOJI} **Helper status unavailable** · last attempt {checked}"
            if run.job_status in (None, "queued"):
                activity = "Waiting for a Helper device"
            else:
                stage = result.stage.partition(":")[0] if result is not None else ""
                activity = STAGE_LABELS.get(stage, "Helper is hosting the match")
            side = "" if run.side == LIVE_SIDE else f" for {side_label(run.side)}"
            return f"{LOADING_EMOJI} **{activity}**{side} · {checked}"
        case "TERMINAL":
            return f"{IMPORTANT_EMOJI} {run.close_reason}"


def stored_scores(match: Match | None, side: int, district: str) -> tuple[bool, str]:
    if match is None:
        return False, ""
    index = FINAL_DISTRICTS.index(district)
    scores = []
    for player in result_sides(side):
        score = match.participants[player].result[index]
        scores.append((player, score if score and is_valid_match_result_entry(score) else None))
    if not any(score for _, score in scores):
        return False, ""
    details = "".join(
        f" · P{player + 1} `{f'{get_stars(score)}⭐ {get_destruction(score)}%' if score else '-'}`"
        for player, score in scores
    )
    return all(score for _, score in scores), details


def district_line(state: str, district: str, scores: str, *, stored: bool, open_run: bool) -> str:
    if stored:
        return f"{SUCCESS_EMOJI} **{district}** · Scores stored{scores}"
    match state:
        case "played":
            scoring_state = "Scores pending" if open_run else "Scores missing"
            emoji = LOADING_EMOJI if open_run else IMPORTANT_EMOJI
            return f"{emoji} **{district}** · Attacked · {scoring_state}{scores}"
        case "in progress":
            return f"{CLASHING_SWORD_EMOJI} **{district}** · Challenge sent{scores}"
        case _:
            return f"➖ {district}{scores}"


def district_lines(
        run: HelperSequentialRun, match: Match | None, result: HelperSequentialResult | None, *, open_run: bool,
) -> str:
    states = {HELPER_DISTRICT_NAMES.get(code): state for code, state in district_states(result).items()} if result else {}
    lines = []
    for district in run.districts:
        stored, scores = stored_scores(match, run.side, district)
        if run.replay_baseline is not None and district not in run.replay_replacements:
            if district in run.replay_conflicts or (not open_run and stored):
                lines.append(f"{IMPORTANT_EMOJI} **{district}** · Scores kept{scores}")
                continue
            stored = False
        lines.append(district_line(states.get(district, "waiting"), district, scores, stored=stored, open_run=open_run))
    return "\n".join(lines)


def progress_embed(run: HelperSequentialRun, match: Match | None) -> ipy.Embed:
    heading = "Sequential Replay" if run.replay_baseline is not None else "Sequential Hosting"
    title = f"**{run.final_node_id} · {heading}**" if run.final_node_id else f"**{heading}**"
    result = run_result(run)
    status = run_status_line(run, result)
    districts = district_lines(run, match, result, open_run=run.open)
    return ipy.Embed(
        title=title,
        description=f"{status}\n\n{districts}",
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=footer_image()),
        color=embed_color(),
    )


def progress_components(
        run: HelperSequentialRun, *, can_continue: bool, can_submit: bool, match_token: str, can_replay: bool = False,
) -> list[ipy.Button]:
    if run.open:
        return [ipy.Button(style=ipy.ButtonStyle.DANGER, label="Cancel", emoji=CROSS_EMOJI,
                           custom_id=f"{CANCEL_PREFIX}|{run.id}")]
    buttons = []
    if can_continue:
        buttons.append(ipy.Button(style=ipy.ButtonStyle.GRAY, label="Continue", emoji=ipy.PartialEmoji(name="▶️"),
                                  custom_id=f"{CONTINUE_PREFIX}|{run.id}"))
    if can_replay:
        buttons.append(ipy.Button(style=ipy.ButtonStyle.GRAY, label="Replay", emoji="🔁",
                                  custom_id=f"{REPLAY_PREFIX}|{run.id}"))
    if can_submit:
        buttons.append(ipy.Button(style=ipy.ButtonStyle.GRAY, label="Submit Result",
                                  emoji=ipy.PartialEmoji(name="📥"), custom_id=f"submit_button|{match_token}"))
    return buttons


def continuation_picker(districts: list[str], start: str) -> tuple[str, list[ipy.ActionRow]]:
    text = f"Continue from **{start}**."
    if start != districts[0]:
        text += " Earlier unfinished districts stay pending."
    options = [ipy.StringSelectOption(label=district, value=district, default=district == start)
               for district in districts]
    return text, [
        ipy.ActionRow(ipy.StringSelectMenu(*options, custom_id=CONTINUE_DISTRICT)),
        ipy.ActionRow(ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="Continue", custom_id=CONTINUE_CONFIRM)),
    ]


def replay_picker(scores: dict[str, list[str | None]], selected: list[str]) -> tuple[str, list[ipy.ActionRow]]:
    options = [ipy.StringSelectOption(
        label=district, value=district, default=district in selected,
        description=" · ".join(score or "–" for score in values) if any(values) else "No saved scores",
    ) for district, values in scores.items()]
    return "Choose districts to replay.", [
        ipy.ActionRow(ipy.StringSelectMenu(*options, min_values=0, max_values=len(options),
                                         placeholder="⚔️ Select districts", custom_id=CONTINUE_DISTRICT)),
        ipy.ActionRow(ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="Start Replay", disabled=not selected,
                                 custom_id=CONTINUE_CONFIRM)),
    ]


def helper_summary(job_status: str | None, result: HelperSequentialResult | None, error: str | None) -> str:
    lines = [f"Status: **{job_status or 'unknown'}**"]
    if result is not None:
        lines.append(f"Stage: `{result.stage}` · districts {len(played_district_codes(result))}/"
                     f"{result.completion.districts_total} · scores **{result.score_status}**")
        if result.label:
            lines.append(f"Label: {result.label}")
        if result.failure_cause:
            lines.append(f"Failure cause: `{result.failure_cause}`")
    if error:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def verified_versus_stored(run: HelperSequentialRun, result: HelperSequentialResult | None,
                           match: Match | None) -> str:
    if result is None:
        return "No Helper result yet."
    lines = []
    for outcome in result.score_outcomes:
        district = HELPER_DISTRICT_NAMES.get(outcome.district, outcome.district)
        side = outcome_side(run, outcome)
        entry = outcome_entry(outcome)
        stored = None
        if match is not None and district in FINAL_DISTRICTS:
            stored = match.participants[side].result[FINAL_DISTRICTS.index(district)] or None
        if outcome.status != "verified":
            flag = f"⚠️ unresolved ({outcome.cause or 'no cause'})"
        elif entry is None:
            flag = "⚠️ verified but not storable; enter it manually"
        elif stored != entry:
            flag = f"⚠️ stored `{stored or '-'}`"
        else:
            flag = "✅ stored"
        lines.append(f"**{district}** P{side + 1}: Helper `{entry or '-'}` · {flag}")
    return "\n".join(lines) or "No scores recorded yet."


def status_embed(
        run: HelperSequentialRun,
        match: Match | None,
        checks: list[str],
        live: HelperSequentialJob | str,
        progress_link: str | None,
        clan_name: str,
) -> ipy.Embed:
    now = time.time()
    saved = run_result(run)
    tracking = (f"✅ Tracked, last check {relative_time(run.last_polled_at)}" if is_tracked(run, now)
                else f"⚠️ Not tracked, last check {relative_time(run.last_polled_at)}"
                + (". Run `/match helper recover`." if run.open and run.state == "ACTIVE" else "."))
    if run.poll_failures:
        tracking += f"\nFailed checks in a row: {run.poll_failures} · last error: {run.last_poll_error}"
    district_result, open_run = saved, run.open
    if isinstance(live, HelperSequentialJob):
        district_result = live.result or saved
        open_run = live.status in ("queued", "running")
        live_text = helper_summary(live.status, live.result, live.error)
        live_snapshot = live.result.model_dump(mode="json") if live.result else None
        if (live.status, live.error, live_snapshot) != (run.job_status, run.error, run.snapshot):
            live_text = f"⚠️ Differs from the saved state.\n{live_text}"
    else:
        live_text = live
    events = "\n".join(
        f"{relative_time(event.at)} `{event.kind}` {event.detail}"[:120]
        + (f" by <@{event.actor_id}>" if event.actor_id else "")
        for event in run.events[-STATUS_EVENT_COUNT:]
    )
    overview = (f"Run `{run.id}` · {side_label(run.side)} · **{clan_name}**\n"
                f"State **{run.state}** · {'open' if run.open else 'closed'} · requested by <@{run.requested_by}> "
                f"{relative_time(run.created_at)}")
    if run.previous_run_id:
        overview += f"\nContinues run `{run.previous_run_id}`"
    if progress_link:
        overview += f"\n[Progress message]({progress_link})"
    fields = [
        ipy.EmbedField(name="**Run**", value=overview),
        ipy.EmbedField(name="**Tracking**", value=tracking),
        ipy.EmbedField(name="**Helper (saved)**", value=helper_summary(run.job_status, saved, run.error)),
        ipy.EmbedField(name="**Helper (live)**", value=live_text),
        ipy.EmbedField(name="**Districts**", value=district_lines(run, match, district_result, open_run=open_run)),
        ipy.EmbedField(name="**Scores**", value=verified_versus_stored(run, saved, match)[:1024]),
        ipy.EmbedField(name="**Checks**", value="\n".join(checks)),
        ipy.EmbedField(name="**Recent Events**", value=events[:1024] or "None"),
    ]
    return ipy.Embed(title="**Helper Run Status**", fields=fields,
                     footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=footer_image()), color=embed_color())


def list_line(run: HelperSequentialRun, now: float, clan_name: str) -> str:
    flag = ""
    if run.open and (run.state != "ACTIVE" or not is_tracked(run, now)):
        flag = "⚠️ "
    result = run_result(run)
    progress = f"{len(played_district_codes(result))}/{result.completion.districts_total}" if result else "-"
    return (f"{flag}**{run.final_node_id}** <#{run.thread_id}> · {side_label(run.side)} · **{clan_name}** · "
            f"{run.state} · Helper {run.job_status or '-'} · {progress} · checked {relative_time(run.last_polled_at)}")


def list_embed(runs: list[HelperSequentialRun], clan_names: dict[str, str]) -> ipy.Embed:
    now = time.time()
    description = "\n".join(list_line(run, now, clan_names[run.host_clan_tag]) for run in runs) or "No matching Helper runs."
    return ipy.Embed(title="**Helper Runs**", description=description,
                     footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=footer_image()), color=embed_color())


def list_components(page: int, total_pages: int, state: str, clan: str | None) -> ipy.ActionRow:
    scope = f"{state}|{clan or '-'}"
    return ipy.ActionRow(
        ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="◀️️", custom_id=f"{LIST_LEFT_PREFIX}|{max(page - 1, 0)}|{scope}",
                   disabled=page == 0),
        build_page_button(page + 1, total_pages, f"{LIST_PAGE_PREFIX}|{scope}"),
        ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="▶️",
                   custom_id=f"{LIST_RIGHT_PREFIX}|{min(page + 1, total_pages - 1)}|{scope}",
                   disabled=page == total_pages - 1),
    )


def check_line(passed: bool, text: str) -> str:
    return f"{'✅' if passed else '⚠️'} {text}"

