from app.config import get_routing_config

FALLBACK_PRICE = [0.0, 0.0]


def price_for(model: str) -> list[float]:
    return get_routing_config().prices_per_1m.get(model, FALLBACK_PRICE)


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    input_price, output_price = price_for(model)
    return (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000
