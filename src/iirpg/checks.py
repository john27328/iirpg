"""Pure check result calculations shared by scene and combat rules."""

def degree(natural: int, total: int, dc: int) -> int:
    result = 3 if total >= dc + 10 else 2 if total >= dc else 0 if total <= dc - 10 else 1
    return max(0, min(3, result + (1 if natural == 20 else -1 if natural == 1 else 0)))
