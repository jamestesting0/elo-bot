from common.finals.types import FinalError, NODE_IDS, ROUND_NODES
from elobot.db.season_models import EloFinal, EloFinalMatch


def seeded_players(final: EloFinal) -> list[str]:
    ranked = sorted(final.entrant_ids, key=lambda user: (
        final.archived_elo.get(user) is None, -(final.archived_elo.get(user) or 0), user,
    ))
    slots: list[str | None] = [None] * 8
    for user, position in final.seeds.items():
        if user not in ranked or not 1 <= position <= 8 or slots[position - 1] is not None:
            raise FinalError("Seeds must be unique positions from 1 to 8 for registered entrants.")
        slots[position - 1] = user
    remaining = iter(user for user in ranked if user not in final.seeds)
    for index, value in enumerate(slots):
        if value is None:
            slots[index] = next(remaining, None)
    return [user for user in slots if user is not None]


def automatic_pairs(final: EloFinal) -> dict[str, list[str | None]]:
    ordered = seeded_players(final)
    if not 5 <= len(ordered) <= 8:
        raise FinalError("Bracket creation requires 5 to 8 entrants.")
    slots = ordered + [None] * (8 - len(ordered))
    pairs = {node: [slots[a - 1], slots[b - 1]]
             for node, (a, b) in zip(NODE_IDS[:4], ((1, 8), (4, 5), (2, 7), (3, 6)))}
    required = set(final.byes)
    if not required <= set(ordered) or len(required) > 8 - len(ordered):
        raise FinalError("Explicit byes exceed this bracket's quota.")
    for user in ordered:
        if user not in required:
            continue
        source = next(pair for pair in pairs.values() if user in pair)
        if None in source:
            continue
        target = next((pair for pair in pairs.values() if None in pair and pair[0] not in required), None)
        if target is None:
            raise FinalError("The requested byes cannot form a complete first round.")
        index = source.index(user)
        source[index], target[0] = target[0], user
    return pairs


def build_bracket(final: EloFinal, pairs: dict[str, list[str | None]]) -> dict[str, EloFinalMatch]:
    entrants = final.entrant_ids
    if not 5 <= len(entrants) <= 8 or set(pairs) != set(NODE_IDS[:4]):
        raise FinalError("Choose all four quarterfinal pairings for 5 to 8 entrants.")
    assigned = [user for pair in pairs.values() for user in pair if user is not None]
    if sorted(assigned) != sorted(entrants) or len(set(assigned)) != len(assigned):
        raise FinalError("Every entrant must appear exactly once.")
    if any(len(pair) != 2 or all(user is None for user in pair) for pair in pairs.values()):
        raise FinalError("Each pairing needs a player and an opponent or BYE.")
    byes = [next(user for user in pair if user is not None) for pair in pairs.values() if None in pair]
    if len(byes) != 8 - len(entrants) or not set(final.byes) <= set(byes):
        raise FinalError("The bye count or explicitly assigned byes do not match.")
    nodes = {}
    for node_id in NODE_IDS[:4]:
        pair = pairs[node_id]
        bye = None in pair
        nodes[node_id] = EloFinalMatch(id=node_id, contestants=list(pair), bye=bye,
                                      winner=next(user for user in pair if user is not None) if bye else None,
                                      state="BYE" if bye else "READY")
    for node_id, feeders in (("SF1", ["QF1", "QF2"]), ("SF2", ["QF3", "QF4"]), ("F1", ["SF1", "SF2"])):
        nodes[node_id] = EloFinalMatch(id=node_id, feeders=feeders)
    advance_nodes(nodes)
    return nodes


def advance_nodes(nodes: dict[str, EloFinalMatch]) -> list[str]:
    changed = []
    for node_id in NODE_IDS[4:]:
        node = nodes[node_id]
        contestants = [nodes[feeder].winner for feeder in node.feeders]
        if contestants != node.contestants:
            had_play = node.first_actual_started_at is not None or node.result_id is not None
            node.contestants = contestants
            node.contestant_revision += 1
            node.result_id = None
            node.winner = None
            node.outcome = None
            node.match_id = None
            node.thread_id = None
            node.provisioning = False
            node.first_actual_started_at = None
            node.start_claim = None
            node.scheduled_times = [None, None]
            node.scheduled_is_war = None
            node.actual_started_at = None
            node.state = "NEEDS_MANAGER" if had_play else ("READY" if all(contestants) else "WAITING")
            changed.append(node_id)
        elif not node.winner and not node.result_id and node.state != "NEEDS_MANAGER":
            node.state = "READY" if all(contestants) else "WAITING"
    return changed


def require_round_ready(final: EloFinal, round_name: str) -> tuple[str, ...]:
    if round_name not in ROUND_NODES:
        raise FinalError("Choose Quarterfinals, Semifinals or Final.")
    previous = {"Semifinals": "Quarterfinals", "Final": "Semifinals"}.get(round_name)
    if previous and any(not final.nodes[node].winner for node in ROUND_NODES[previous]):
        raise FinalError("Resolve every preceding-round pairing before starting this round.")
    if final.repair_pending or not final.active or not final.nodes:
        raise FinalError("This Final is not ready. Use `/final recover` for pending work.")
    return ROUND_NODES[round_name]
