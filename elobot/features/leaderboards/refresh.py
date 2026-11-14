def request_leaderboard_player_reload(bot) -> None:
    get_ext = getattr(bot, "get_ext", None)
    if not callable(get_ext):
        return
    cog = get_ext("LeaderboardCommands")
    if cog is None:
        return
    request = getattr(cog, "request_player_reload", None)
    if callable(request):
        request()
