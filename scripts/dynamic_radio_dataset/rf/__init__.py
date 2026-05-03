"""RF/RSS processing package.

This package intentionally stays separate from CARLA collection and plan
generation. Importing it must not import Sionna, Mitsuba, or Dr.Jit at module
load time; those dependencies are reached only inside subprocess jobs.
"""

