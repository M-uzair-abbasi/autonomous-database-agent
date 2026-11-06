"""Stand-ins for neighbours' modules, so `ask` runs before they exist.

Each one is deleted the day the real module lands. Nothing outside `ask`
should import from here.
"""
