"""glink-harness host: the process that drives a pi kernel and serves the workbench.

The host never imports pi: it starts the official ``pi`` binary as a child
process and consumes its documented headless protocol. Everything under
``kernel/`` (added in a later task) turns that protocol into the product event
stream defined by :mod:`glink_harness.events`.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
