"""BlitzTrader: an aggressive short-hold swing engine driven by an agent with the Robinhood MCP.

The Python here is deterministic and stdlib-only. It never talks to the broker: an agent session
feeds it broker facts on the command line, and it answers with one JSON object saying what to do.
"""

__version__ = "0.1.0"
