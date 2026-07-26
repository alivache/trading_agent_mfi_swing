def should_force_close_at_eod(hour: int, minute: int, close_every_night: bool) -> bool:
    """Returneaza True cand sistemul trebuie sa inchida pozitiile la finalul zilei."""
    if close_every_night:
        return hour >= 20 or (hour == 19 and minute >= 45)
    return False


def should_enter_swing_signal(
    trend_strength: float,
    rsi_value: float,
    macd_strength: float,
    volume_ratio: float,
    ema_gap_pct: float,
    ema20_value: float,
    bullish_hmm: bool,
) -> bool:
    """Returneaza True doar daca semnalul este suficient de puternic pentru swing."""
    if not bullish_hmm:
        return False
    if trend_strength <= 0.015:
        return False
    if rsi_value < 45 or rsi_value > 70:
        return False
    if macd_strength <= 0.0:
        return False
    if volume_ratio < 1.0:
        return False
    if ema_gap_pct <= 0.01:
        return False
    if ema20_value <= 50:
        return False
    return True
