def format_time(seconds: int) -> str:
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    output = ""
    if hours > 0:
        output += f"{int(hours)}h "
    if minutes > 0:
        output += f"{int(minutes)}m "
    if seconds > 0:
        output += f"{int(seconds)}s"
    if not output:
        output = "0s"
    return output.strip()
