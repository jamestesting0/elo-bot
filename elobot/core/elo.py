import math

from elobot.config.game import LOSS_FACTOR, WIN_FACTOR, C, K


def calculate_elo(elo: int, opponent_elo: int, stars: list[int], destruction: list[int], opponent_stars: list[int],
                  opponent_destruction: list[int], is_challenge: bool) -> int:
    q_p1 = 10 ** (elo / C)
    q_p2 = 10 ** (opponent_elo / C)
    expected_outcome = q_p1 / (q_p1 + q_p2)
    p_p1 = calculate_points(stars, destruction)
    p_p2 = calculate_points(opponent_stars, opponent_destruction)
    sign = calculate_sign(stars, destruction, opponent_stars, opponent_destruction)
    try:
        actual_outcome = p_p1 / (p_p1 + p_p2)
    except ZeroDivisionError:
        actual_outcome = 0.5

    min_loss = 5
    max_loss = 40
    min_gain = 10
    max_gain = 45
    if is_challenge:
        min_loss = 0
        max_loss = 30
        min_gain = 0
        max_gain = 35

    final_elo = round(sign * 25 + elo + K * (actual_outcome - expected_outcome))
    if sign > 0:
        return min(elo + max_gain, max(elo + min_gain, final_elo))
    elif sign < 0:
        return max(elo - max_loss, min(elo - min_loss, final_elo))
    else:
        return min(elo + 10, max(elo - 10, final_elo))


def calculate_points(stars: list[int], destruction: list[int]) -> int:
    points = 0
    for i in range(0, 3):
        if stars[i] == 0:
            points += math.floor(destruction[0] / 10)
        elif stars[i] == 1:
            points += 5 + math.floor(destruction[0] / 9)
        elif stars[i] == 2:
            points += 16 + math.floor((destruction[0] - 50) / 3)
        else:
            points += 40

    return points


def calculate_sign(stars: list[int], destruction: list[int], opponent_stars: list[int],
                   opponent_destruction: list[int]) -> int:
    p1_score = sum(stars) * 1000 + sum(destruction)
    p2_score = sum(opponent_stars) * 1000 + sum(opponent_destruction)

    if p1_score == p2_score:
        return 0
    elif p1_score > p2_score:
        return 1 * WIN_FACTOR
    else:
        return -1 * LOSS_FACTOR
