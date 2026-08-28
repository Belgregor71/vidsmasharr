"""Keeping other software from undoing our work.

Everything in this package writes somewhere outside our own database, which is
why it lives apart from the rest: it is the one place where a bug does not just
produce a bad report.
"""
