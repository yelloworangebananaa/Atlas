"""Orb state: idle | listening | thinking | acting. Module-level to avoid circular imports."""
current = "idle"


def set(value):
    global current
    current = value


def get():
    return current
