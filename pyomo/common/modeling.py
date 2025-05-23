#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright (c) 2008-2025
#  National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________

from .dependencies import random

# [Aug 24] Importing for backwards compatibility; may deprecate this import later
from .flags import FlagType, NOTSET

# Backward compatibility with the previous name for this flag
NoArgumentGiven = NOTSET


def randint(a, b):
    """Our implementation of random.randint.

    The Python random.randint is not consistent between python versions
    and produces a series that is different in 3.x than 2.x.  So that we
    can support deterministic testing (i.e., setting the random.seed and
    expecting the same sequence), we will implement a simple, but stable
    version of randint()."""
    return int((b - a + 1) * random.random())


def unique_component_name(instance, name):
    # test if this name already exists in model. If not, we're good.
    # Else, we add random numbers until it doesn't
    if instance.component(name) is None and not hasattr(instance, name):
        return name
    name += '_%d' % (randint(0, 9),)
    while True:
        if instance.component(name) is None and not hasattr(instance, name):
            return name
        else:
            name += str(randint(0, 9))
