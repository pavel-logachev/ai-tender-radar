"""Prompt-driven lead research agent.

A procurement is only a signal. The agent reads it, estimates the customer's economics, finds a named
contact with a phone (in the procedure first, on the open web otherwise) and prepares a short talk track.
Judgement lives in ``prompt.md``; code here only provides tools, budgets, grounding checks, storage
and delivery.
"""
