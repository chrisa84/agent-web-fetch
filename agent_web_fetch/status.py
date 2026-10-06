"""Request discipline visibility without contacting any backend."""


def render_status(state: dict) -> str:
    wait = state["jina_cooldown_seconds"]
    lines = [f"Jina cooldown: {wait}s remaining" if wait else "Jina cooldown: inactive"]
    for domain in state["domains"]:
        details = []
        if domain["hold_seconds"]:
            details.append(f"hold {domain['hold_seconds']}s remaining")
        if domain["breaker_seconds"]:
            details.append(f"circuit-broken {domain['breaker_seconds']}s remaining")
        if domain["jina_delay_seconds"] > 0 or domain["browser_delay_seconds"] > 1:
            details.append(f"backing off (Jina delay {domain['jina_delay_seconds']:g}s, "
                           f"browser delay {domain['browser_delay_seconds']:g}s; "
                           f"next request in {domain['hold_seconds']}s)")
        lines.append(domain["host"] + ": " + "; ".join(details))
    if not state["domains"]:
        lines.append("Domains: no active holds, breakers or backoff")
    return "\n".join(lines) + "\n"


def summary(state: dict) -> str:
    domains = state["domains"]
    return (f"{sum(d['hold_seconds'] > 0 for d in domains)} domain holds, "
            f"{sum(d['breaker_seconds'] > 0 for d in domains)} circuit breakers, "
            f"{sum(d['jina_delay_seconds'] > 0 or d['browser_delay_seconds'] > 1 for d in domains)} backing off; "
            f"Jina cooldown {state['jina_cooldown_seconds']}s")
