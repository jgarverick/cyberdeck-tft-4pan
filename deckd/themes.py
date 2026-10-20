"""Color palettes deckd sends to the CYD. They match the neon-deck and cmp-deck desktop themes."""

THEMES = {
    "neon": {
        "name": "neon", "title": "NEON//DECK",
        "bg": "#0a0a12", "panel": "#141420", "line": "#2a2a3a",
        "fg": "#e6e6f0", "dim": "#5a5a78",
        "accent": "#ff2a6d", "accent2": "#05d9e8", "hi": "#f5b700",
        "ok": "#00ff9f", "bad": "#f5b700",
        "tabs": ["MIX", "DECK", "SYS", "DOCK"],
    },
    "cmp": {
        "name": "cmp", "title": "CLOWN MIGRATION PLAN",
        "bg": "#0b0a0f", "panel": "#16131c", "line": "#2e2638",
        "fg": "#ece6f4", "dim": "#6e6578",
        "accent": "#9142EE",   # Visionary
        "accent2": "#D00CF3",  # Countermeasure
        "hi": "#EE9C23",       # Buffer
        "ok": "#13C458",       # Guardian
        "bad": "#FF0000",      # Event
        "tabs": ["MIXER", "DECK", "BURN RATE", "SHIPPING"],
    },
}
