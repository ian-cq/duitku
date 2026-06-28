"""Emitters: push canonical Transactions to a downstream system.

Currently only :mod:`duitku.emitters.firefly`. The shape is kept
narrow on purpose; if a second emitter ever lands we'll extract a
``Protocol``.
"""
